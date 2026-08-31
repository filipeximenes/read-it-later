"""Tests for carrying changes between two libraries.

The rules themselves are tested in `test_merge.py`. These cover what happens
around them: applying a merge to files on disk, moving bodies without ever
losing one, and the cursors that decide what a sync sends.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from ril.models import Article
from ril.sync import merge_article, needs_body

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


def test_a_preview_needs_no_server_and_writes_nothing(tmp_path):
    """It reports what would be sent without touching the library at all."""
    from ril.extractor import ExtractedArticle
    from ril.storage import save_article
    from ril.sync import SyncState, sync_preview

    folder = tmp_path / "here"
    save_article(folder, ExtractedArticle(url="https://e.com/a", title="A", body_markdown="b"))
    before = (folder / "index.json").stat().st_mtime_ns

    assert sync_preview(folder) == 1

    assert (folder / "index.json").stat().st_mtime_ns == before
    assert SyncState.load(folder).local is None


# --- never lose a body ------------------------------------------------------
#
# A real library was emptied by an early version of this code. Every test here
# stands for one of the mistakes that combined to do it.


def _library_with_bodies(folder, count=3):
    from ril.extractor import ExtractedArticle
    from ril.storage import save_article

    saved = []
    for i in range(count):
        saved.append(
            save_article(
                folder,
                ExtractedArticle(
                    url=f"https://e.com/{i}", title=f"T{i}", body_markdown=f"Real body {i}" * 20
                ),
            )
        )
    return saved


def _client_answering(handler):
    import httpx

    from ril.remote import Remote, RemoteClient

    client = RemoteClient(Remote(url="https://e.com/ril", token="t"))
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler), headers=dict(client._client.headers)
    )
    return client


def test_syncing_with_a_server_that_has_no_bodies_keeps_ours(tmp_path):
    """The exact failure that emptied a real library.

    The server had every record but not one body. This side had every body but
    no digests, because they predate sync. Each record therefore looked like
    one whose body was missing here, the server was asked for all of them, and
    its empty answers were written over the real files.
    """
    import httpx

    from ril.storage import load_index, read_body
    from ril.sync import sync_once

    folder = tmp_path / "laptop"
    saved = _library_with_bodies(folder)
    before = {a.id: read_body(folder, a) for a in saved}
    assert all(before.values())

    # Digests are cleared, exactly as a library migrated from version 1.
    from ril.storage import index_transaction

    with index_transaction(folder) as index:
        for article in index.articles:
            article.body_sha256 = None

    def server(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/sync"):
            # It knows the records, and has no body for any of them.
            articles = [a.model_dump(mode="json") for a in load_index(folder).articles]
            for a in articles:
                a["body_sha256"] = None
            return httpx.Response(
                200,
                json={
                    "now": T0.isoformat(),
                    "articles": articles,
                    "want_bodies": [a["id"] for a in articles],
                },
            )
        if request.method == "GET":
            # What the server really did: a 200 with nothing in it, because
            # reading a file it did not have returned an empty string.
            return httpx.Response(200, json={"markdown": ""})
        return httpx.Response(200, json={"body_sha256": "x"})

    sync_once(folder, _client_answering(server))

    after = {a.id: read_body(folder, load_index(folder).by_id()[a.id]) for a in saved}
    assert after == before, "sync destroyed local article bodies"


def test_an_empty_answer_is_never_written_over_a_body(tmp_path):
    """Even if a server answers 200 with nothing, the file here survives."""
    import httpx

    from ril.storage import index_transaction, load_index, read_body
    from ril.sync import sync_once

    folder = tmp_path / "laptop"
    saved = _library_with_bodies(folder, 1)[0]
    before = read_body(folder, saved)

    with index_transaction(folder) as index:
        index.articles[0].body_sha256 = None

    def server(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/sync"):
            article = load_index(folder).articles[0].model_dump(mode="json")
            article["body_sha256"] = "something-else"
            return httpx.Response(
                200,
                json={"now": T0.isoformat(), "articles": [article], "want_bodies": []},
            )
        return httpx.Response(200, json={"markdown": "   \n  "})

    sync_once(folder, _client_answering(server))
    assert read_body(folder, load_index(folder).by_id()[saved.id]) == before


def test_storage_refuses_to_write_an_empty_body(tmp_path):
    """The last guard, in case a caller ever stops filtering."""
    from ril.storage import write_body

    folder = tmp_path / "laptop"
    saved = _library_with_bodies(folder, 1)[0]
    with pytest.raises(ValueError, match="empty body"):
        write_body(folder, saved, "   \n ")


def test_digests_are_filled_in_from_the_files_on_disk(tmp_path):
    """A library older than sync has no digests; the files supply them."""
    from ril.storage import index_transaction, load_index
    from ril.sync import verify_digests

    folder = tmp_path / "laptop"
    _library_with_bodies(folder, 3)
    with index_transaction(folder) as index:
        for article in index.articles:
            article.body_sha256 = None

    assert verify_digests(folder) == 3
    assert all(a.body_sha256 for a in load_index(folder).articles)
    # Repeating it does nothing, because there is nothing left to settle.
    assert verify_digests(folder) == 0


def test_a_body_is_not_asked_for_when_the_other_side_has_none():
    remote = _article(body_sha256=None)
    assert not needs_body(None, remote)
    assert not needs_body(_article(body_sha256="aaa"), remote)


def test_a_side_with_a_body_wins_a_tie_against_a_side_without_one():
    """Recovery depends on this.

    After restoring from a backup, both sides hold the same record with the
    same clocks, but only one still has the body. If the empty side won, the
    merged record would say there is no body, and neither side would ever ask
    for one.
    """
    has_body = _article(body_sha256="aaa")
    no_body = _article(body_sha256=None)

    for merged in (merge_article(has_body, no_body), merge_article(no_body, has_body)):
        assert merged.body_sha256 == "aaa"

    # And the empty side then knows to ask for it.
    assert needs_body(no_body, merge_article(has_body, no_body))


def test_a_digest_is_never_adopted_before_its_body_arrives(tmp_path):
    """Otherwise both sides agree nothing is missing, and it never is sent.

    This happened for real: a body failed to arrive, but the receiving side
    had already taken the sender's digest, so the two compared equal forever
    and the gap could not be seen.
    """
    from ril.extractor import ExtractedArticle
    from ril.storage import index_transaction, load_index, save_article
    from ril.sync import merge_locally

    folder = tmp_path / "here"
    saved = save_article(
        folder, ExtractedArticle(url="https://e.com/x", title="X", body_markdown="mine")
    )
    # This side has no body at all, as a receiver would not.
    with index_transaction(folder) as index:
        index.articles[0].body_sha256 = None

    incoming = saved.model_copy(deep=True)
    incoming.body_sha256 = "their-digest"
    incoming.touch_content()

    outcome = merge_locally(folder, [incoming])

    stored = load_index(folder).by_id()[saved.id]
    assert stored.body_sha256 is None, "took a digest for a body it does not have"
    assert saved.id in outcome.wanted_bodies


def test_verifying_digests_corrects_a_claim_the_file_does_not_support(tmp_path):
    from ril.extractor import ExtractedArticle
    from ril.storage import get_article_path, index_transaction, load_index, save_article
    from ril.sync import verify_digests

    folder = tmp_path / "here"
    saved = save_article(
        folder, ExtractedArticle(url="https://e.com/x", title="X", body_markdown="real body")
    )
    # The file loses its body while the index still claims one.
    path = get_article_path(folder, saved)
    path.write_text("---\nid: x\n---\n\n", encoding="utf-8")

    assert verify_digests(folder) == 1
    assert load_index(folder).by_id()[saved.id].body_sha256 is None

    # And a library that already agrees with itself is left alone.
    with index_transaction(folder):
        pass
    assert verify_digests(folder) == 0


def test_bodies_are_pulled_in_batches(tmp_path):
    """A first pull must not be one round trip per article."""
    import httpx

    from ril.sync import _BODY_BATCH, _pull_bodies

    calls = []

    def server(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        ids = json.loads(request.content)["ids"]
        return httpx.Response(200, json={"bodies": {i: f"body {i}" for i in ids}})

    wanted = {f"id{i:04d}" for i in range(_BODY_BATCH * 3)}
    _pull_bodies(tmp_path / "empty", _client_answering(server), wanted)

    assert len(calls) == 3, f"expected 3 batched calls, made {len(calls)}"
    assert set(calls) == {"/ril/api/sync/bodies"}


def test_a_server_without_batching_still_gets_every_body(tmp_path):
    """The fallback must cover the whole remainder, not what came back."""
    import httpx

    from ril.extractor import ExtractedArticle
    from ril.storage import load_index, read_body, save_article
    from ril.sync import _pull_bodies

    folder = tmp_path / "here"
    saved = [
        save_article(
            folder,
            ExtractedArticle(url=f"https://e.com/{i}", title=f"T{i}", body_markdown="placeholder"),
        )
        for i in range(3)
    ]
    asked_one_at_a_time = []

    def server(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(404, json={"detail": "no batch route here"})
        article_id = request.url.path.rsplit("/", 1)[-1]
        asked_one_at_a_time.append(article_id)
        return httpx.Response(200, json={"markdown": f"real body for {article_id}"})

    _pull_bodies(folder, _client_answering(server), {a.id for a in saved})

    assert sorted(asked_one_at_a_time) == sorted(a.id for a in saved)
    index = load_index(folder)
    for a in saved:
        assert "real body for" in read_body(folder, index.by_id()[a.id])
