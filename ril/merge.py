"""The rules for merging two copies of one article.

Both sides run these, unchanged, on the same pair of records. That is what
makes sync safe without a coordinator: it does not matter which side syncs
first, in what order changes arrive, or how long one side was offline. Given
the same two records, both ends reach the same answer.

Three properties are relied on, and checked over thousands of random pairs:

- **Commutative.** `merge(a, b)` equals `merge(b, a)`. If it were not, the two
  sides would disagree and each would keep pushing its own version forever.
- **Idempotent.** Merging a result again changes nothing, so a retried or
  duplicated sync is harmless.
- **Associative.** Three replicas reach the same answer in any order.

A record is not merged as a whole. It is three parts that change
independently and therefore settle independently: whether it still exists, its
body and metadata, and whether it has been read.

Nothing here touches the disk or the network. That is deliberate: these rules
are the part that has to be provably well behaved, and they stay easy to
reason about only while they remain a pure function of two records.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Optional

from ril.models import Article

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
    A library older than sync has no digests either, which is why
    `verify_digests` settles them from the files before any comparison.
    """
    if remote.deleted or remote.body_sha256 is None:
        return False
    if local is None or local.body_sha256 is None:
        return True
    return local.body_sha256 != remote.body_sha256
