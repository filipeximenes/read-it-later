"""Tests for index writes: atomic replacement, locking, transactions."""

from __future__ import annotations

import json
import multiprocessing
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ril.extractor import ExtractedArticle
from ril.models import Article, Index
from ril.storage import (
    INDEX_LOCK_NAME,
    INDEX_NAME,
    delete_article,
    index_transaction,
    is_internal_file,
    load_index,
    save_article,
    save_index,
    update_article,
)


def _extracted(url: str = "https://example.com/a", title: str = "A") -> ExtractedArticle:
    return ExtractedArticle(url=url, title=title, body_markdown="body")


def _article(article_id: str = "abc123", url: str = "https://example.com/a") -> Article:
    return Article(
        id=article_id,
        url=url,
        title="A",
        saved_at=datetime.now(timezone.utc),
        filename=f"20250101T000000Z_{article_id}_a.md",
    )


# --- atomic replacement -----------------------------------------------------


def test_the_index_round_trips(tmp_path):
    index = Index(articles=[_article()])
    save_index(tmp_path, index)
    assert [a.id for a in load_index(tmp_path).articles] == ["abc123"]


def test_a_missing_index_reads_as_empty(tmp_path):
    assert load_index(tmp_path).articles == []


def test_no_temporary_file_is_left_behind(tmp_path):
    save_index(tmp_path, Index(articles=[_article()]))
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != INDEX_NAME]
    assert leftovers == []


def test_a_failed_write_leaves_the_previous_index_intact(tmp_path, monkeypatch):
    """A crash mid-write must not truncate the file that is already there."""
    save_index(tmp_path, Index(articles=[_article()]))

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(OSError):
        save_index(tmp_path, Index(articles=[]))

    # The old content survived, and nothing half-written was left around.
    assert [a.id for a in load_index(tmp_path).articles] == ["abc123"]
    assert [p.name for p in tmp_path.iterdir() if p.name != INDEX_NAME] == []


def test_a_reader_never_sees_a_partial_file(tmp_path):
    """`os.replace` is atomic, so the index parses at every moment."""
    save_index(tmp_path, Index(articles=[_article(f"id{i:04d}") for i in range(500)]))
    raw = (tmp_path / INDEX_NAME).read_text()
    assert json.loads(raw)["articles"][0]["id"] == "id0000"


# --- transactions -----------------------------------------------------------


def test_a_transaction_writes_the_change(tmp_path):
    with index_transaction(tmp_path) as index:
        index.articles.append(_article())
    assert len(load_index(tmp_path).articles) == 1


def test_a_failed_transaction_abandons_the_change(tmp_path):
    save_index(tmp_path, Index(articles=[_article()]))

    with pytest.raises(ValueError):
        with index_transaction(tmp_path) as index:
            index.articles = []
            raise ValueError("something went wrong")

    assert len(load_index(tmp_path).articles) == 1


def test_the_lock_is_released_after_a_failure(tmp_path):
    """A failed transaction must not wedge every later write."""
    with pytest.raises(ValueError):
        with index_transaction(tmp_path):
            raise ValueError("boom")

    with index_transaction(tmp_path) as index:
        index.articles.append(_article())
    assert len(load_index(tmp_path).articles) == 1


# --- the writers all go through a transaction -------------------------------


def test_saving_an_article_adds_it(tmp_path):
    article = save_article(tmp_path, _extracted())
    assert [a.id for a in load_index(tmp_path).articles] == [article.id]
    assert (tmp_path / "articles" / article.filename).exists()


def test_deleting_an_article_leaves_a_tombstone_and_removes_the_file(tmp_path):
    """The row has to stay so the delete can reach the other side."""
    article = save_article(tmp_path, _extracted())
    delete_article(tmp_path, article)

    index = load_index(tmp_path)
    assert index.live == []
    assert len(index.articles) == 1
    assert index.articles[0].deleted
    # The file is what takes up room, so that does go.
    assert not (tmp_path / "articles" / article.filename).exists()


def test_a_deleted_article_cannot_be_found(tmp_path):
    """Otherwise `ril open` would open something the user deleted."""
    article = save_article(tmp_path, _extracted())
    delete_article(tmp_path, article)

    index = load_index(tmp_path)
    assert index.find_by_id(article.id) is None
    assert index.find_by_url(article.url) is None
    # Sync still needs to see it, so it stays reachable that way.
    assert index.by_id()[article.id].deleted


def test_a_deleted_url_can_be_saved_again(tmp_path):
    """Re-adding must revive the one row, not leave two sharing an id."""
    article = save_article(tmp_path, _extracted())
    delete_article(tmp_path, article)

    again = save_article(tmp_path, _extracted())

    index = load_index(tmp_path)
    assert again.id == article.id
    assert len(index.articles) == 1
    assert index.live == index.articles
    assert index.find_by_url(article.url) is not None


def test_updating_an_article_replaces_it(tmp_path):
    article = save_article(tmp_path, _extracted())
    article.title = "Changed"
    update_article(tmp_path, article)
    assert load_index(tmp_path).articles[0].title == "Changed"


# --- internal files ---------------------------------------------------------


@pytest.mark.parametrize("name", [INDEX_LOCK_NAME, ".index-abc.tmp", ".index-.tmp"])
def test_bookkeeping_files_are_recognised(name):
    assert is_internal_file(name)


@pytest.mark.parametrize("name", [INDEX_NAME, "articles", "a.md", ".DS_Store", "index-abc.tmp"])
def test_real_files_are_not_mistaken_for_bookkeeping(name):
    assert not is_internal_file(name)


def test_an_export_leaves_the_lock_out(tmp_path):
    """A backup must not carry this process's lock file into the archive."""
    import zipfile

    from ril.archive import create_archive

    data = tmp_path / "data"
    save_article(data, _extracted())
    with index_transaction(data):
        pass  # creates the lock file
    assert (data / INDEX_LOCK_NAME).exists()

    zip_path = tmp_path / "out.zip"
    create_archive(data, zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        assert not any(is_internal_file(Path(n).name) for n in zf.namelist())


# --- concurrency ------------------------------------------------------------


def _append_in_child(args) -> None:
    """Run in a separate process, so this is a real cross-process lock test."""
    folder, article_id = args
    with index_transaction(Path(folder)) as index:
        index.articles.append(_article(article_id, f"https://example.com/{article_id}"))


def test_concurrent_writers_do_not_lose_changes(tmp_path):
    """Without the lock, each writer loads the same index and one wins.

    Twenty processes each append one article. All twenty must survive.
    """
    save_index(tmp_path, Index())
    jobs = [(str(tmp_path), f"id{i:04d}") for i in range(20)]

    with multiprocessing.get_context("spawn").Pool(8) as pool:
        pool.map(_append_in_child, jobs)

    saved = {a.id for a in load_index(tmp_path).articles}
    assert saved == {f"id{i:04d}" for i in range(20)}


def test_refreshing_moves_the_content_clock(tmp_path):
    """Without this, a re-fetched body never reaches the other side.

    Sync sends what changed since the last cursor. If a refresh left every
    clock alone, the improved body would sit here unnoticed forever.
    """
    from ril.storage import refresh_article

    article = save_article(tmp_path, _extracted())
    before = article.content_updated_at

    refreshed = refresh_article(tmp_path, article, _extracted(title="Now with the real body"))

    assert refreshed.content_updated_at > before
    assert refreshed.body_sha256 is not None


def test_saving_records_the_body_digest(tmp_path):
    article = save_article(tmp_path, _extracted())
    assert article.body_sha256
    assert load_index(tmp_path).articles[0].body_sha256 == article.body_sha256
