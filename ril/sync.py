"""Merging two copies of the library.

Both sides run these rules, unchanged, on the same pair of records. That is
what makes sync safe without a coordinator: it does not matter which side
syncs first, in what order changes arrive, or how long one side was offline.
Given the same two records, both ends reach the same answer.

Two properties are relied on and tested:

- **Commutative.** `merge(a, b)` equals `merge(b, a)`. If it were not, the two
  sides would disagree and each would keep pushing its own version forever.
- **Idempotent.** Merging a result again changes nothing, so a retried or
  duplicated sync is harmless.

A record is not merged as a whole. It is three parts that change
independently and therefore settle independently: whether it still exists, its
body and metadata, and whether it has been read.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from ril.models import Article, as_utc, utcnow
from ril.remote import RemoteProtocolError
from ril.storage import (
    SYNC_STATE_NAME,
    body_digest,
    get_article_path,
    index_transaction,
    load_index,
    read_body,
    write_body,
)

# Bumped if the wire format changes in a way an older client cannot read.
SYNC_PROTOCOL = 1

# Fields that travel together as "the article's content". The body lives in a
# file, so `body_sha256` and `filename` are how a record points at it.
_CONTENT_FIELDS = (
    "url",
    "title",
    "author",
    "description",
    "published_date",
    "tags",
    "image_urls",
    "video_urls",
    "fetch_failed",
    "filename",
    "body_sha256",
    "content_updated_at",
)


def _fingerprint(values: tuple) -> str:
    """A stable digest of some fields, used only to break an exact tie.

    Both sides must break a tie the same way, or they would each keep choosing
    their own version and never agree. Ranking on a digest of the fields
    themselves does that, and makes a tie mean what it should: the two records
    are identical in everything being compared, so either will do.
    """
    return hashlib.sha256(repr(values).encode("utf-8")).hexdigest()


def _content_rank(article: Article) -> tuple:
    """How good this side's content is. Higher wins.

    A body that was actually fetched beats a paywall stub however recent the
    stub is. This is the rule that makes the whole arrangement work: the
    command line runs where the browser cookies are, so its copy of a
    paywalled article is the real one, and it must not be overwritten by the
    server's later, emptier attempt.
    """
    return (
        not article.fetch_failed,
        article.content_updated_at,
        # At equal times, a side that actually holds a body beats one that
        # holds none. Without this the two could tie on an arbitrary digest,
        # and a side with no body could win — after which neither side would
        # ask for one, because their digests would agree at "nothing".
        article.body_sha256 is not None,
        _fingerprint(tuple(getattr(article, name) for name in _CONTENT_FIELDS)),
    )


def _state_rank(article: Article) -> tuple:
    """Whether this side's read state is the newer one. Higher wins."""
    return (
        article.state_updated_at,
        _fingerprint((article.read, article.read_at)),
    )


def _existence_rank(article: Article) -> tuple:
    """Whether this side's view of the article existing is the newer one.

    Deletion is not permanent: saving a URL again clears the tombstone and
    moves this clock, so a later re-add beats an earlier delete. Only an exact
    tie falls back to deleting, and that needs two machines to have acted in
    the same microsecond.
    """
    return (article.existence_updated_at, article.deleted)


def _better(left: Article, right: Article, rank) -> Article:
    return left if rank(left) >= rank(right) else right


def merge_article(mine: Optional[Article], theirs: Optional[Article]) -> Article:
    """The one record both sides should end up holding.

    Either side may be absent, which is simply "this side has not seen it".
    """
    if mine is None and theirs is None:
        raise ValueError("merge_article needs at least one record")
    if mine is None:
        return theirs.model_copy(deep=True)
    if theirs is None:
        return mine.model_copy(deep=True)
    if mine.id != theirs.id:
        raise ValueError(f"Cannot merge different articles: {mine.id} and {theirs.id}")

    content = _better(mine, theirs, _content_rank)
    state = _better(mine, theirs, _state_rank)
    existence = _better(mine, theirs, _existence_rank)

    merged = content.model_copy(deep=True)
    for field in ("read", "read_at", "state_updated_at"):
        setattr(merged, field, getattr(state, field))
    merged.deleted = existence.deleted
    merged.existence_updated_at = existence.existence_updated_at
    # The article was saved when it was first saved anywhere.
    merged.saved_at = min(mine.saved_at, theirs.saved_at)
    return merged


def merge_indexes(mine: dict[str, Article], theirs: dict[str, Article]) -> dict[str, Article]:
    """Merge two id-keyed sets of records, including tombstones."""
    return {
        article_id: merge_article(mine.get(article_id), theirs.get(article_id))
        for article_id in mine.keys() | theirs.keys()
    }


def changed_since(articles: list[Article], moment: Optional[datetime]) -> list[Article]:
    """Records touched since `moment`. Everything, when that is None."""
    if moment is None:
        return list(articles)
    return [
        a
        for a in articles
        if max(a.content_updated_at, a.state_updated_at, a.existence_updated_at) > moment
    ]


def needs_body(local: Optional[Article], remote: Article) -> bool:
    """Whether the remote body has to be fetched to bring this side up to date.

    An unknown digest on the remote side is not a reason to ask for it: the
    other side is saying it has no body, and asking would get nothing back.
    A library from before sync existed has no digests either, which is why
    `backfill_digests` runs first and fills them in from the files on disk.
    """
    if remote.deleted or remote.body_sha256 is None:
        return False
    if local is None or local.body_sha256 is None:
        return True
    return local.body_sha256 != remote.body_sha256


def verify_digests(data_folder: Path) -> int:
    """Recompute every digest from the files on disk. Returns how many changed.

    A digest is a claim about a file. If the two ever disagree — a body that
    failed to arrive, a file removed by hand — neither side can tell, because
    they compare claims and not files. This settles it from the files
    themselves, and runs on a full reconciliation, which is the moment such a
    disagreement can actually be repaired.
    """
    corrected = 0
    with index_transaction(data_folder) as index:
        for article in index.articles:
            if article.deleted:
                continue
            try:
                body = read_body(data_folder, article)
            except (ValueError, OSError):
                continue
            actual = body_digest(body) if body.strip() else None
            if article.body_sha256 != actual:
                article.body_sha256 = actual
                corrected += 1
    return corrected


def backfill_digests(data_folder: Path) -> int:
    """Record the digest of every body already on disk. Returns how many.

    A library saved before sync existed has no digests. Left unset, every one
    of its records looks like one whose body is missing here, and the other
    side gets asked for bodies this side already holds.
    """
    filled = 0
    with index_transaction(data_folder) as index:
        for article in index.articles:
            if article.deleted or article.body_sha256 is not None:
                continue
            try:
                body = read_body(data_folder, article)
            except (ValueError, OSError):
                continue
            if body:
                article.body_sha256 = body_digest(body)
                filled += 1
    return filled


def _merge_records(index, incoming: list[Article]) -> tuple[set[str], set[str], list[Article]]:
    """Merge records into an open index.

    Returns the ids that were touched, the ids whose body this side no longer
    holds — because the merge chose content from the other side and the file
    here belongs to the version that lost — and the records that are now
    tombstones, whose files the caller must remove.
    """
    mine = index.by_id()
    touched: set[str] = set()
    wanted: set[str] = set()
    tombstoned: list[Article] = []

    for remote in incoming:
        current = mine.get(remote.id)
        merged = merge_article(current, remote)
        mine[merged.id] = merged
        touched.add(merged.id)

        if merged.deleted:
            # A delete that arrives has to take the file with it, or the body
            # stays on disk forever and shows up as an orphan in a backup.
            merged.body_sha256 = None
            tombstoned.append(merged)
            continue

        # The digest has to describe the body this side actually holds, not
        # the one the merge would like it to hold. Adopting the other side's
        # digest before its body arrives makes both sides agree that nothing
        # needs sending, and the missing body is never noticed again.
        held = None if current is None else current.body_sha256
        merged.body_sha256 = held
        if remote.body_sha256 and remote.body_sha256 != held:
            wanted.add(merged.id)

    index.articles = list(mine.values())
    return touched, wanted, tombstoned


def _remove_bodies(data_folder: Path, articles: list[Article]) -> None:
    """Delete the files of records that are now tombstones. Safe to repeat."""
    for article in articles:
        try:
            path = get_article_path(data_folder, article)
        except ValueError:
            # A filename that would escape the articles folder is refused by
            # storage; there is nothing of ours to remove.
            continue
        path.unlink(missing_ok=True)


@dataclass
class MergeOutcome:
    """What one round of merging decided."""

    outgoing: list[Article]
    wanted_bodies: set[str]
    now: datetime


def apply_incoming(
    data_folder: Path,
    incoming: list[Article],
    since: Optional[datetime],
) -> MergeOutcome:
    """Merge what the other side sent, and say what it has not seen.

    The returned moment is this machine's clock, and the caller sends it back
    as `since` next time. So the cursor is always compared against timestamps
    written here, and a change made here can never be skipped because the
    other side's clock runs fast or slow.

    Everything merged is returned too, even when the other side sent it,
    because the merge may not have decided in its favour and it needs the
    answer.
    """
    # A full reconciliation is the one moment a digest that disagrees with the
    # file behind it can be found and put right.
    if since is None:
        verify_digests(data_folder)

    now = utcnow()
    with index_transaction(data_folder) as index:
        send = {a.id for a in changed_since(index.articles, since)}
        touched, wanted, tombstoned = _merge_records(index, incoming)
        send |= touched
        outgoing = [a for a in index.articles if a.id in send]
    _remove_bodies(data_folder, tombstoned)
    return MergeOutcome(outgoing=outgoing, wanted_bodies=wanted, now=now)


def merge_locally(data_folder: Path, incoming: list[Article]) -> MergeOutcome:
    """Merge records from the other side into this library.

    The same rules as `apply_incoming`, without working out a reply.
    """
    with index_transaction(data_folder) as index:
        touched, wanted, tombstoned = _merge_records(index, incoming)
        merged = [a for a in index.articles if a.id in touched]
    _remove_bodies(data_folder, tombstoned)
    return MergeOutcome(outgoing=merged, wanted_bodies=wanted, now=utcnow())


# --- the client side --------------------------------------------------------


@dataclass
class SyncState:
    """Where the last sync left off.

    Two cursors, because the two sides keep their own clocks. `remote` came
    from the server and is compared against the server's timestamps; `local`
    is ours, and decides what we still have to send.
    """

    remote: Optional[datetime] = None
    local: Optional[datetime] = None

    @classmethod
    def load(cls, data_folder: Path) -> SyncState:
        path = data_folder / SYNC_STATE_NAME
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A lost cursor is not a failure. Both sides fall back to a full
            # reconciliation, which reaches the same place more slowly.
            return cls()
        return cls(remote=_read_moment(raw.get("remote")), local=_read_moment(raw.get("local")))

    def save(self, data_folder: Path) -> None:
        path = data_folder / SYNC_STATE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "remote": self.remote.isoformat() if self.remote else None,
                    "local": self.local.isoformat() if self.local else None,
                },
                indent=2,
            ),
            encoding="utf-8",
        )


def _read_moment(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return as_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


@dataclass
class SyncReport:
    """What one sync did, for the command line to print."""

    sent: int = 0
    received: int = 0
    bodies_sent: int = 0
    bodies_received: int = 0
    repaired: int = 0
    dry_run: bool = False

    @property
    def quiet(self) -> bool:
        return not any(
            (self.sent, self.received, self.bodies_sent, self.bodies_received, self.repaired)
        )


def sync_once(data_folder: Path, client, dry_run: bool = False) -> SyncReport:
    """One full exchange with the hosted copy.

    Nothing here has to succeed for the library to stay correct. If the
    exchange fails part way, the cursors are not moved, so the next sync
    simply covers the same ground again.
    """
    state = SyncState.load(data_folder)
    if state.remote is None:
        # A full exchange: check every digest against its file, so a body that
        # went missing here is noticed rather than assumed present.
        verify_digests(data_folder)
    else:
        # Must happen before anything is compared, or records whose digest is
        # simply unknown look like records whose body is missing.
        backfill_digests(data_folder)
    local_now = utcnow()
    index = load_index(data_folder)
    outgoing = changed_since(index.articles, state.local)

    if dry_run:
        return SyncReport(sent=len(outgoing), dry_run=True)

    payload = {
        "since": state.remote.isoformat() if state.remote else None,
        "articles": [a.model_dump(mode="json") for a in outgoing],
    }
    response = client.request("POST", "/api/sync", json=payload)
    # A 404 or 405 is a server too old to sync. Reading it as an empty answer
    # would look like a successful, quiet sync and move the cursor past
    # changes that were never sent.
    if response.status_code in (404, 405):
        raise RemoteProtocolError("That server has no sync endpoints yet. Update the hosted copy.")
    if response.status_code >= 400:
        raise RemoteProtocolError(f"The sync endpoint answered {response.status_code}.")
    try:
        answer = response.json()
        incoming = [Article.model_validate(a) for a in answer.get("articles", [])]
    except (ValueError, TypeError) as exc:
        raise RemoteProtocolError(f"Could not read the sync answer: {exc}") from exc
    outcome = merge_locally(data_folder, incoming)
    report = SyncReport(sent=len(outgoing), received=len(incoming))

    # Send before receiving. If anything goes wrong in between, the other side
    # has gained a body rather than this side having lost one.
    report.bodies_sent = _push_bodies(data_folder, client, answer.get("want_bodies", []))
    report.bodies_received = _pull_bodies(data_folder, client, outcome.wanted_bodies)

    # Only now, once everything has landed, does the sync count as done.
    SyncState(remote=_read_moment(answer.get("now")) or local_now, local=local_now).save(
        data_folder
    )
    return report


def _pull_bodies(data_folder: Path, client, wanted: set[str]) -> int:
    if not wanted:
        return 0
    fetched: dict[str, str] = {}
    for article_id in sorted(wanted):
        response = client.request("GET", f"/api/sync/body/{article_id}")
        if response.status_code == 404:
            continue
        markdown = response.json().get("markdown", "") if response.status_code < 300 else ""
        # An empty answer is never an improvement on what is here. It means the
        # other side has no file, and writing it would destroy this one.
        if markdown.strip():
            fetched[article_id] = markdown

    if not fetched:
        return 0
    written = 0
    with index_transaction(data_folder) as index:
        by_id = index.by_id()
        for article_id, markdown in fetched.items():
            article = by_id.get(article_id)
            if article is None or article.deleted:
                continue
            if article.body_sha256 == body_digest(markdown.strip()):
                continue  # already exactly this
            write_body(data_folder, article, markdown)
            written += 1
    return written


# How many bodies go in one request. Large enough that a first sync is a few
# dozen round trips rather than thousands, small enough to keep each request
# and the memory behind it modest.
_BODY_BATCH = 50


def _push_bodies(data_folder: Path, client, wanted: list[str]) -> int:
    if not wanted:
        return 0
    by_id = load_index(data_folder).by_id()

    ready: dict[str, str] = {}
    for article_id in wanted:
        article = by_id.get(article_id)
        if article is None or article.deleted:
            continue
        markdown = read_body(data_folder, article)
        if markdown.strip():
            ready[article_id] = markdown

    items = list(ready.items())
    sent = 0
    for start in range(0, len(items), _BODY_BATCH):
        batch = dict(items[start : start + _BODY_BATCH])
        response = client.request("PUT", "/api/sync/bodies", json={"bodies": batch})
        if response.status_code in (404, 405):
            # A server from before batching. Fall back to one at a time.
            for article_id, markdown in items[start:]:
                client.request("PUT", f"/api/sync/body/{article_id}", json={"markdown": markdown})
                sent += 1
            return sent
        sent += len(batch)
    return sent


# How long a library may go unsynced before a read-only command pulls first.
# Long enough that a burst of commands does not each hit the network, short
# enough that a list is not badly out of date.
STALE_AFTER = timedelta(minutes=5)


def sync_is_stale(data_folder: Path, now: Optional[datetime] = None) -> bool:
    """Whether enough time has passed to be worth syncing before reading."""
    last = SyncState.load(data_folder).local
    if last is None:
        return True
    return (now or utcnow()) - last > STALE_AFTER
