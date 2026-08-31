"""Tests for the merge rules.

The scenario tests say what each rule does. The property tests at the bottom
say the rules hold together: whichever side merges first, both reach the same
answer, and merging twice changes nothing.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from ril.models import Article
from ril.storage import body_digest
from ril.sync import changed_since, merge_article, merge_indexes, needs_body

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _article(article_id="abc123", *, at=T0, **overrides) -> Article:
    fields = dict(
        id=article_id,
        url=f"https://example.com/{article_id}",
        title="Title",
        saved_at=at,
        filename=f"20260101T120000Z_{article_id}_title.md",
        content_updated_at=at,
        state_updated_at=at,
    )
    fields.update(overrides)
    return Article(**fields)


def _later(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


# --- one side has never seen it ---------------------------------------------


def test_a_record_only_one_side_has_is_taken_as_is():
    mine = _article()
    assert merge_article(mine, None) == mine
    assert merge_article(None, mine) == mine


def test_merging_returns_a_copy_not_the_original():
    """A merge must not quietly mutate either side's record."""
    mine = _article()
    merged = merge_article(mine, None)
    merged.title = "Changed"
    assert mine.title == "Title"


def test_merging_nothing_is_a_mistake():
    with pytest.raises(ValueError):
        merge_article(None, None)


def test_two_different_articles_cannot_be_merged():
    with pytest.raises(ValueError, match="different articles"):
        merge_article(_article("aaa"), _article("bbb"))


# --- content: quality beats recency -----------------------------------------


def test_a_fetched_body_beats_a_paywall_stub_however_new():
    """The whole point: the laptop has the cookies, so its copy is the real one."""
    good = _article(fetch_failed=False, title="The real article", at=_later(0))
    stub = _article(fetch_failed=True, title="Subscribe to continue", at=_later(60))

    for merged in (merge_article(good, stub), merge_article(stub, good)):
        assert merged.title == "The real article"
        assert not merged.fetch_failed


def test_between_two_fetched_bodies_the_newer_wins():
    older = _article(title="First", content_updated_at=_later(0))
    newer = _article(title="Second", content_updated_at=_later(10))

    for merged in (merge_article(older, newer), merge_article(newer, older)):
        assert merged.title == "Second"


def test_between_two_stubs_the_newer_wins():
    older = _article(title="Old stub", fetch_failed=True, content_updated_at=_later(0))
    newer = _article(title="New stub", fetch_failed=True, content_updated_at=_later(10))

    for merged in (merge_article(older, newer), merge_article(newer, older)):
        assert merged.title == "New stub"


# --- read state settles on its own ------------------------------------------


def test_the_newer_read_state_wins_regardless_of_content():
    """The two halves settle separately, which is why there are two clocks."""
    read_later = _article(read=True, read_at=_later(30), state_updated_at=_later(30))
    newer_content = _article(title="Better", content_updated_at=_later(60))

    merged = merge_article(read_later, newer_content)
    assert merged.read is True
    assert merged.title == "Better"


def test_marking_unread_later_wins_even_though_read_at_is_cleared():
    """`read_at` cannot carry the ordering: unread throws it away."""
    marked_read = _article(read=True, read_at=_later(10), state_updated_at=_later(10))
    marked_unread = _article(read=False, read_at=None, state_updated_at=_later(20))

    for merged in (
        merge_article(marked_read, marked_unread),
        merge_article(marked_unread, marked_read),
    ):
        assert merged.read is False
        assert merged.read_at is None


# --- existence --------------------------------------------------------------


def test_a_delete_travels():
    live = _article()
    deleted = _article(deleted=True, existence_updated_at=_later(10))

    for merged in (merge_article(live, deleted), merge_article(deleted, live)):
        assert merged.deleted


def test_a_delete_beats_an_earlier_edit():
    edited = _article(title="Edited", content_updated_at=_later(5))
    deleted = _article(deleted=True, existence_updated_at=_later(10))

    for merged in (merge_article(edited, deleted), merge_article(deleted, edited)):
        assert merged.deleted


def test_adding_the_url_again_later_beats_the_delete():
    """Otherwise a deleted article could never be saved again.

    `save_article` moves the existence clock when it revives a row, which is
    what this stands in for.
    """
    deleted = _article(deleted=True, existence_updated_at=_later(10))
    re_added = _article(
        title="Saved again", content_updated_at=_later(20), existence_updated_at=_later(20)
    )

    for merged in (merge_article(deleted, re_added), merge_article(re_added, deleted)):
        assert not merged.deleted
        assert merged.deleted_at is None
        assert merged.title == "Saved again"


def test_two_deletes_stay_deleted():
    first = _article(deleted=True, existence_updated_at=_later(10))
    second = _article(deleted=True, existence_updated_at=_later(20))
    assert merge_article(first, second).deleted


# --- saved_at ---------------------------------------------------------------


def test_the_article_was_saved_when_it_was_first_saved_anywhere():
    early = _article(at=_later(0))
    late = _article(at=_later(90))

    for merged in (merge_article(early, late), merge_article(late, early)):
        assert merged.saved_at == _later(0)


# --- whole indexes ----------------------------------------------------------


def test_merging_indexes_covers_both_sides():
    mine = {"a": _article("a"), "shared": _article("shared", title="Mine")}
    theirs = {
        "b": _article("b"),
        "shared": _article("shared", title="Theirs", content_updated_at=_later(10)),
    }

    merged = merge_indexes(mine, theirs)

    assert set(merged) == {"a", "b", "shared"}
    assert merged["shared"].title == "Theirs"


def test_merging_indexes_is_commutative():
    mine = {"a": _article("a"), "shared": _article("shared", title="Mine")}
    theirs = {"b": _article("b"), "shared": _article("shared", title="Theirs")}
    assert merge_indexes(mine, theirs) == merge_indexes(theirs, mine)


# --- what to send and fetch -------------------------------------------------


def test_everything_is_changed_when_there_is_no_cursor():
    articles = [_article("a"), _article("b")]
    assert changed_since(articles, None) == articles


@pytest.mark.parametrize(
    "overrides",
    [
        {"content_updated_at": _later(30)},
        {"state_updated_at": _later(30)},
        {"deleted_at": _later(30)},
    ],
)
def test_a_change_to_any_clock_counts_as_changed(overrides):
    """A delete is a change too, or tombstones would never be sent."""
    assert changed_since([_article(**overrides)], _later(10)) != []


def test_an_untouched_record_is_not_resent():
    assert changed_since([_article(at=_later(0))], _later(10)) == []


def test_a_body_is_fetched_when_this_side_has_none_or_a_different_one():
    remote = _article(body_sha256="aaa")
    assert needs_body(None, remote)
    assert needs_body(_article(body_sha256=None), remote)
    assert needs_body(_article(body_sha256="bbb"), remote)


def test_an_unchanged_body_is_not_fetched_again():
    assert not needs_body(_article(body_sha256="aaa"), _article(body_sha256="aaa"))


def test_a_deleted_record_has_no_body_to_fetch():
    deleted = _article(deleted=True, existence_updated_at=_later(1), body_sha256="aaa")
    assert not needs_body(None, deleted)


def test_a_digest_depends_only_on_the_text():
    assert body_digest("hello") == body_digest("hello")
    assert body_digest("hello") != body_digest("world")


# --- the properties the whole design rests on -------------------------------


def _random_article(rng: random.Random, article_id: str = "abc123") -> Article:
    deleted = rng.random() < 0.3
    read = rng.random() < 0.5
    return _article(
        article_id,
        title=rng.choice(["A", "B", "C"]),
        author=rng.choice([None, "X", "Y"]),
        tags=rng.choice([[], ["t"], ["u", "v"]]),
        fetch_failed=rng.random() < 0.4,
        content_updated_at=_later(rng.choice([0, 5, 10])),
        state_updated_at=_later(rng.choice([0, 5, 10])),
        read=read,
        read_at=_later(rng.choice([0, 5, 10])) if read else None,
        deleted=deleted,
        existence_updated_at=_later(rng.choice([0, 5, 10])),
        body_sha256=rng.choice([None, "aaa", "bbb"]),
        at=_later(rng.choice([0, 5])),
    )


def test_merging_is_commutative_over_many_random_pairs():
    """If order mattered, the two sides would never stop disagreeing."""
    rng = random.Random(20260830)
    for _ in range(3000):
        a, b = _random_article(rng), _random_article(rng)
        assert merge_article(a, b) == merge_article(b, a), (a, b)


def test_merging_is_idempotent_over_many_random_pairs():
    """A repeated or retried sync must not keep changing the answer."""
    rng = random.Random(11)
    for _ in range(3000):
        a, b = _random_article(rng), _random_article(rng)
        once = merge_article(a, b)
        assert merge_article(once, b) == once, (a, b)
        assert merge_article(a, once) == once, (a, b)
        assert merge_article(once, once) == once, (a, b)


def test_three_sides_reach_the_same_answer_in_any_order():
    """Associativity: a laptop, a phone and the server must all converge."""
    rng = random.Random(7)
    for _ in range(2000):
        a, b, c = (_random_article(rng) for _ in range(3))
        left = merge_article(merge_article(a, b), c)
        right = merge_article(a, merge_article(b, c))
        assert left == right, (a, b, c)


# --- applying a merge to a real library -------------------------------------


def _folder_with(tmp_path, name: str):
    from ril.storage import save_article

    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    return folder, save_article


def test_a_tombstone_arriving_removes_the_file(tmp_path):
    """A delete has to take the body with it, or backups fill with orphans."""
    from ril.extractor import ExtractedArticle
    from ril.storage import save_article
    from ril.sync import merge_locally

    folder = tmp_path / "here"
    saved = save_article(
        folder, ExtractedArticle(url="https://e.com/x", title="X", body_markdown="body")
    )
    path = folder / "articles" / saved.filename
    assert path.exists()

    gone = saved.model_copy(deep=True)
    gone.mark_deleted()
    merge_locally(folder, [gone])

    assert not path.exists()
    from ril.storage import load_index

    assert load_index(folder).by_id()[saved.id].deleted


def test_removing_a_body_twice_is_not_an_error(tmp_path):
    """Sync retries, so every step of it has to tolerate being repeated."""
    from ril.extractor import ExtractedArticle
    from ril.storage import save_article
    from ril.sync import merge_locally

    folder = tmp_path / "here"
    saved = save_article(
        folder, ExtractedArticle(url="https://e.com/x", title="X", body_markdown="body")
    )
    gone = saved.model_copy(deep=True)
    gone.mark_deleted()

    merge_locally(folder, [gone])
    merge_locally(folder, [gone])  # must not raise


def test_two_libraries_converge_through_the_wire_format(tmp_path):
    """A full round trip, using the same records the endpoints exchange."""
    from ril.extractor import ExtractedArticle
    from ril.storage import load_index, read_body, save_article, write_body
    from ril.sync import apply_incoming, merge_locally

    laptop, server = tmp_path / "laptop", tmp_path / "server"
    save_article(
        laptop, ExtractedArticle(url="https://e.com/a", title="A", body_markdown="Body of A")
    )
    save_article(
        server, ExtractedArticle(url="https://e.com/b", title="B", body_markdown="Body of B")
    )

    # Laptop sends what it has; the server merges and answers.
    outgoing = load_index(laptop).articles
    answer = apply_incoming(server, outgoing, None)
    merged = merge_locally(laptop, answer.outgoing)

    # Bodies move in whichever direction the merge asked for.
    for article_id in merged.wanted_bodies:
        article = load_index(laptop).by_id()[article_id]
        write_body(laptop, article, read_body(server, article))
    for article_id in answer.wanted_bodies:
        article = load_index(server).by_id()[article_id]
        write_body(server, article, read_body(laptop, article))

    def shape(folder):
        return {a.id: (a.title, a.read, a.deleted) for a in load_index(folder).articles}

    assert shape(laptop) == shape(server)
    assert len(shape(laptop)) == 2


def test_a_server_without_sync_endpoints_is_an_error_not_an_empty_sync(tmp_path):
    """Reading a 404 as "nothing changed" would move the cursor past real work."""
    import httpx

    from ril.remote import Remote, RemoteClient, RemoteProtocolError
    from ril.sync import SyncState, sync_once

    client = RemoteClient(Remote(url="https://e.com/ril", token="t"))
    client._client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(404, json={"detail": "x"})),
        headers=dict(client._client.headers),
    )

    folder = tmp_path / "here"
    with pytest.raises(RemoteProtocolError, match="no sync endpoints"):
        sync_once(folder, client)

    # The cursor stayed where it was, so nothing has been skipped.
    assert SyncState.load(folder).local is None


def test_a_failed_exchange_leaves_the_cursor_alone(tmp_path):
    """Whatever was missed simply goes with the next sync."""
    import httpx

    from ril.remote import Remote, RemoteClient, RemoteUnavailableError
    from ril.sync import SyncState, sync_once

    def refuse(request):
        raise httpx.ConnectError("down")

    client = RemoteClient(Remote(url="https://e.com/ril", token="t"))
    client._client = httpx.Client(
        transport=httpx.MockTransport(refuse), headers=dict(client._client.headers)
    )

    folder = tmp_path / "here"
    with pytest.raises(RemoteUnavailableError):
        sync_once(folder, client)
    assert SyncState.load(folder).local is None


def test_a_dry_run_changes_nothing(tmp_path):
    from ril.sync import SyncState, sync_once

    folder = tmp_path / "here"
    report = sync_once(folder, client=None, dry_run=True)
    assert report.dry_run
    assert SyncState.load(folder).local is None
