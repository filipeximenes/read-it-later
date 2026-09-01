from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from datetime import datetime, timedelta
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from ril.archive import (
    ArchiveError,
    create_archive,
    export_filename,
    restore_archive,
)
from ril.extractor import fetch_and_extract
from ril.models import Article, Index
from ril.remote import (
    Remote,
    RemoteClient,
    RemoteError,
    load_remote,
)
from ril.storage import (
    delete_article,
    get_article_path,
    index_transaction,
    load_index,
    read_body,
    refresh_article,
    save_article,
    update_article,
    write_body,
)
from ril.sync import SYNC_PROTOCOL, SyncReport, apply_incoming, sync_once

# Import replaces the whole library, so it is guarded by a header a cross-origin
# page cannot send without a CORS preflight that this app never grants.
_IMPORT_CONFIRM_HEADER = "x-ril-confirm"
_IMPORT_CONFIRM_VALUE = "replace-all"
_MAX_IMPORT_BYTES = 2 * 1024 * 1024 * 1024

# Starting a sync writes to this library and reaches out to the hosted copy, so
# the button that does it is guarded the same way import is.
_SYNC_RUN_HEADER = "x-ril-sync"
_SYNC_RUN_VALUE = "run"

_FRONT_MATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n?", re.DOTALL)

_CLI_SEARCH_TIMEOUT_SEC = 120


class _SearchUnavailableError(RuntimeError):
    pass


class _AddRequest(BaseModel):
    url: str


class _SyncRequest(BaseModel):
    """One round of sync: what the other side changed, and where it left off."""

    since: Optional[datetime] = None
    articles: list[Article] = Field(default_factory=list)


class _BodyRequest(BaseModel):
    markdown: str


class _BodiesRequest(BaseModel):
    """Several bodies at once, so one index write covers all of them."""

    bodies: dict[str, str] = Field(default_factory=dict)


class _BodiesWanted(BaseModel):
    ids: list[str] = Field(default_factory=list)


_YT_WATCH_RE = re.compile(r"(?:https?://)?(?:www\.)?youtube\.com/watch\?.*?v=([A-Za-z0-9_-]+)")
_YT_SHORT_RE = re.compile(r"(?:https?://)?(?:www\.)?youtu\.be/([A-Za-z0-9_-]+)")
_VIMEO_RE = re.compile(r"(?:https?://)?(?:www\.)?vimeo\.com/(\d+)")

# The reader page is a real HTML file, `ril/web/index.html`. It is read
# through the package so an installed wheel finds it the same way a checkout
# does, and cached because it never changes while the server runs.
_PAGE = resources.files("ril").joinpath("web/index.html")


@lru_cache(maxsize=1)
def _reader_page() -> str:
    return _PAGE.read_text(encoding="utf-8")


def _strip_front_matter(content: str) -> str:
    return _FRONT_MATTER_RE.sub("", content, count=1).lstrip()


def _basename_set_from_cli_stdout(stdout: str) -> set[str]:
    basenames: set[str] = set()
    for line in stdout.splitlines():
        line = line.strip()
        if line:
            basenames.add(Path(line).name)
    return basenames


def _article_indices_match(article: Article, needle_lc: str) -> bool:
    for field in (article.title, article.author, article.description):
        if field and needle_lc in field.lower():
            return True
    return False


def _matching_filenames_via_cli(data_folder: Path, needle: str) -> set[str]:
    """Return basenames under articles/ matching `needle`; requires rg or grep.

    Raises _SearchUnavailableError if no tool could scan the corpus.
    Vacuous empty set when `articles/` is missing (nothing on disk yet).
    """
    articles_dir = data_folder / "articles"
    if not articles_dir.is_dir():
        return set()

    errors: list[str] = []

    rg_exe = shutil.which("rg")
    if rg_exe:
        try:
            proc = subprocess.run(
                [rg_exe, "-l", "-F", "-i", "--glob", "*.md", needle, str(articles_dir)],
                capture_output=True,
                text=True,
                timeout=_CLI_SEARCH_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            errors.append(f"ripgrep timed out after {_CLI_SEARCH_TIMEOUT_SEC}s")
        else:
            if proc.returncode in (0, 1):
                return _basename_set_from_cli_stdout(proc.stdout)
            err = proc.stderr.strip() or f"ripgrep exited with status {proc.returncode}"
            errors.append(err)

    grep_exe = shutil.which("grep")
    if grep_exe:
        try:
            proc = subprocess.run(
                [grep_exe, "-R", "-l", "-F", "-i", needle, str(articles_dir)],
                capture_output=True,
                text=True,
                timeout=_CLI_SEARCH_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            errors.append(f"grep timed out after {_CLI_SEARCH_TIMEOUT_SEC}s")
        else:
            if proc.returncode in (0, 1):
                return _basename_set_from_cli_stdout(proc.stdout)
            err = proc.stderr.strip() or f"grep exited with status {proc.returncode}"
            errors.append(err)
    elif not rg_exe:
        raise _SearchUnavailableError(
            "Body search requires ripgrep (`rg`) or `grep` on PATH; neither was found.",
        )

    if errors:
        raise _SearchUnavailableError(
            "Could not scan article files (" + "; ".join(errors) + "). "
            "Install ripgrep or fix `grep`; see server logs.",
        )

    raise RuntimeError("_matching_filenames_via_cli: exhaustive handling failed")


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None)


def _tab_articles(filter_name: str, index: Index) -> list[Article]:
    if filter_name == "read":
        return sorted(
            (a for a in index.live if a.read),
            key=lambda a: _naive(a.read_at or a.saved_at),
            reverse=True,
        )
    if filter_name == "all":
        return sorted(index.live, key=lambda a: _naive(a.saved_at), reverse=True)
    return sorted(
        (a for a in index.live if not a.read),
        key=lambda a: _naive(a.saved_at),
    )


def _bucket_index(dt: datetime, now: datetime, unit: str) -> int:
    """How many buckets ago `dt` falls (0 == current bucket)."""
    if unit == "month":
        return (now.year * 12 + now.month) - (dt.year * 12 + dt.month)
    return (now - dt).days // 7


def _bucket_label(back: int, now: datetime, unit: str) -> str:
    if unit == "month":
        total = now.year * 12 + (now.month - 1) - back
        return datetime(total // 12, total % 12 + 1, 1).strftime("%b '%y")
    return (now - timedelta(days=7 * back)).strftime("%b %d")


def _bucket_activity(
    articles: list[Article], now: datetime, unit: str, count: int, offset: int = 0
) -> list[dict]:
    """Saved/read counts per period bucket, oldest first (index -1 == newest in window).

    `offset` shifts the window back in time: offset=0 ends at the current period,
    offset=count shows the immediately preceding window, and so on.
    """
    saved = [0] * count
    read = [0] * count
    lo, hi = offset, offset + count
    for a in articles:
        b = _bucket_index(_naive(a.saved_at), now, unit)
        if lo <= b < hi:
            saved[count - 1 - (b - offset)] += 1
        if a.read_at is not None:
            br = _bucket_index(_naive(a.read_at), now, unit)
            if lo <= br < hi:
                read[count - 1 - (br - offset)] += 1
    return [
        {
            "label": _bucket_label(count - 1 - i + offset, now, unit),
            "saved": saved[i],
            "read": read[i],
        }
        for i in range(count)
    ]


def _compute_stats(index: Index) -> dict:
    articles = index.live
    total = len(articles)
    read = sum(1 for a in articles if a.read)
    failed = sum(1 for a in articles if a.fetch_failed)
    now = datetime.utcnow()
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)
    authors = Counter(a.author for a in articles if a.author)
    tags = Counter(t for a in articles for t in a.tags)
    # Round for display, but never show 0%/100% unless it is exactly true —
    # a full bar with unread items left is misleading.
    if total == 0:
        read_pct = 0
    elif read == total:
        read_pct = 100
    else:
        read_pct = min(99, max(1, round(read / total * 100)))
    # How long recently-read articles sat between being saved and read. Scoped to
    # the last 3 months so the figure reflects current habits, and median-based so
    # the occasional years-old article finally getting read doesn't skew it.
    three_months_ago = now - timedelta(days=90)
    durations = sorted(
        max(0, (_naive(a.read_at) - _naive(a.saved_at)).days)
        for a in articles
        if a.read and a.read_at is not None and _naive(a.read_at) >= three_months_ago
    )
    if durations:
        mid = len(durations) // 2
        median_ttr = (
            durations[mid]
            if len(durations) % 2
            else round((durations[mid - 1] + durations[mid]) / 2)
        )
    else:
        median_ttr = None
    oldest_unread = max(
        ((now - _naive(a.saved_at)).days for a in articles if not a.read),
        default=None,
    )
    return {
        "total": total,
        "unread": total - read,
        "read": read,
        "failed": failed,
        "read_pct": read_pct,
        "saved_this_week": sum(1 for a in articles if _naive(a.saved_at) >= week_ago),
        "read_this_week": sum(1 for a in articles if a.read_at and _naive(a.read_at) >= week_ago),
        "saved_this_month": sum(1 for a in articles if _naive(a.saved_at) >= month_ago),
        "read_this_month": sum(1 for a in articles if a.read_at and _naive(a.read_at) >= month_ago),
        "median_days_to_read": median_ttr,
        "oldest_unread_days": oldest_unread,
        "top_authors": authors.most_common(5),
        "top_tags": tags.most_common(10),
    }


def _summary_payload(result) -> dict:
    """Shape a RestoreResult for the web UI (counts only, no file listings)."""
    summary = result.summary
    return {
        "article_count": summary.article_count,
        "file_count": summary.file_count,
        "missing_files": len(summary.missing_files),
        "orphan_files": len(summary.orphan_files),
        "skipped_entries": len(summary.skipped_entries),
        "replaced_articles": result.replaced_articles,
        "dry_run": result.dry_run,
        "snapshot": result.snapshot_path.name if result.snapshot_path else None,
    }


async def _spool_upload(request: Request, destination: Path) -> int:
    """Stream the request body to disk so a large archive never sits in memory."""
    written = 0
    with destination.open("wb") as fh:
        async for chunk in request.stream():
            written += len(chunk)
            if written > _MAX_IMPORT_BYTES:
                raise HTTPException(status_code=413, detail="Archive is too large.")
            fh.write(chunk)
    return written


def _sync_with_remote(data_folder: Path, remote: Remote) -> SyncReport:
    """One exchange with the hosted copy, driven from inside the web server.

    The same client the command line uses, so a sync started from the page and
    one started from a terminal do exactly the same thing.
    """
    with RemoteClient(remote) as client:
        return sync_once(data_folder, client)


def build_app(data_folder: Path) -> FastAPI:
    app = FastAPI(title="Read It Later", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_reader_page())

    @app.get("/api/articles")
    async def list_articles(filter: str = "unread", q: Optional[str] = None) -> list[dict]:
        index = load_index(data_folder)
        tab_list = _tab_articles(filter, index)

        needle = (q or "").strip().lower()
        if not needle:
            return [a.model_dump(mode="json") for a in tab_list]

        try:
            file_basenames = await asyncio.to_thread(
                _matching_filenames_via_cli, data_folder, needle
            )
        except _SearchUnavailableError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e

        keep_ids = {a.id for a in tab_list if _article_indices_match(a, needle)}
        keep_ids |= {a.id for a in tab_list if a.filename in file_basenames}
        merged = [a for a in tab_list if a.id in keep_ids]
        return [a.model_dump(mode="json") for a in merged]

    @app.get("/api/stats")
    async def stats() -> dict:
        index = load_index(data_folder)
        return _compute_stats(index)

    @app.get("/api/activity")
    async def activity(unit: str = "week", count: int = 8, offset: int = 0) -> dict:
        unit = "month" if unit == "month" else "week"
        count = max(1, min(52, count))
        offset = max(0, min(offset, 5200))
        index = load_index(data_folder)
        now = datetime.utcnow()
        buckets = _bucket_activity(index.live, now, unit, count, offset)
        oldest = offset + count
        has_older = any(
            _bucket_index(_naive(a.saved_at), now, unit) >= oldest
            or (a.read_at is not None and _bucket_index(_naive(a.read_at), now, unit) >= oldest)
            for a in index.live
        )
        return {"buckets": buckets, "has_older": has_older, "has_newer": offset > 0}

    @app.get("/api/articles/{article_id}/content")
    async def article_content(article_id: str) -> dict:
        index = load_index(data_folder)
        article = index.find_by_id(article_id)
        if article is None:
            raise HTTPException(status_code=404, detail="Article not found")
        try:
            path = get_article_path(data_folder, article)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not path.exists():
            raise HTTPException(status_code=404, detail="Article file not found")
        raw = path.read_text(encoding="utf-8")
        return {"content": _strip_front_matter(raw)}

    @app.post("/api/articles", status_code=201)
    async def add_article(body: _AddRequest) -> dict:
        url = body.url.strip()
        if not url:
            raise HTTPException(status_code=422, detail="URL is required.")
        index = load_index(data_folder)
        existing = index.find_by_url(url)
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"Article already saved: {existing.title}")
        extracted = await asyncio.to_thread(fetch_and_extract, url)
        article = await asyncio.to_thread(save_article, data_folder, extracted)
        return article.model_dump(mode="json")

    @app.delete("/api/articles/{article_id}", status_code=204)
    async def remove_article(article_id: str) -> None:
        index = load_index(data_folder)
        article = index.find_by_id(article_id)
        if article is None:
            raise HTTPException(status_code=404, detail="Article not found")
        try:
            delete_article(data_folder, article)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/articles/{article_id}/refresh")
    async def refresh_article_endpoint(article_id: str) -> dict:
        index = load_index(data_folder)
        article = index.find_by_id(article_id)
        if article is None:
            raise HTTPException(status_code=404, detail="Article not found")
        extracted = await asyncio.to_thread(fetch_and_extract, article.url)
        updated = await asyncio.to_thread(refresh_article, data_folder, article, extracted)
        return updated.model_dump(mode="json")

    @app.post("/api/articles/{article_id}/toggle-read")
    async def toggle_read(article_id: str) -> dict:
        index = load_index(data_folder)
        article = index.find_by_id(article_id)
        if article is None:
            raise HTTPException(status_code=404, detail="Article not found")
        if article.read:
            article.mark_unread()
        else:
            article.mark_read()
        update_article(data_folder, article)
        return article.model_dump(mode="json")

    # -- sync ---------------------------------------------------------------

    @app.get("/api/sync")
    async def sync_handshake() -> dict:
        """Say that this instance speaks sync, and which version of it.

        Side-effect free on purpose: it is what a client calls to check its
        credential, and checking a credential must not change anything.
        """
        return {"protocol": SYNC_PROTOCOL}

    @app.post("/api/sync")
    async def sync_endpoint(body: _SyncRequest) -> dict:
        """Merge the other side's changes and hand back what it has not seen."""
        outcome = await asyncio.to_thread(apply_incoming, data_folder, body.articles, body.since)
        return {
            "now": outcome.now.isoformat(),
            "articles": [a.model_dump(mode="json") for a in outcome.outgoing],
            # Bodies this side lost the merge on, so the other side should
            # send them. Working it out here saves a round trip.
            "want_bodies": sorted(outcome.wanted_bodies),
        }

    @app.get("/api/sync/body/{article_id}")
    async def read_sync_body(article_id: str) -> dict:
        index = load_index(data_folder)
        article = index.by_id().get(article_id)
        if article is None or article.deleted:
            raise HTTPException(status_code=404, detail="Article not found")
        try:
            markdown = await asyncio.to_thread(read_body, data_folder, article)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # No body here is a 404, never an empty one. An empty answer would look
        # like a real body to the other side and overwrite the copy it has.
        if not markdown.strip():
            raise HTTPException(status_code=404, detail="No body stored for this article")
        return {"markdown": markdown}

    @app.post("/api/sync/bodies")
    async def read_sync_bodies(body: _BodiesWanted) -> dict:
        """Several bodies at once, so a first pull is not a round trip each.

        Only bodies that exist are returned. A missing one is simply absent
        from the answer, never an empty string, which the other side would
        have no way to tell from a real body.
        """

        def _read() -> dict[str, str]:
            by_id = load_index(data_folder).by_id()
            found: dict[str, str] = {}
            for article_id in body.ids:
                article = by_id.get(article_id)
                if article is None or article.deleted:
                    continue
                try:
                    markdown = read_body(data_folder, article)
                except ValueError:
                    continue
                if markdown.strip():
                    found[article_id] = markdown
            return found

        return {"bodies": await asyncio.to_thread(_read)}

    @app.put("/api/sync/bodies")
    async def write_sync_bodies(body: _BodiesRequest) -> dict:
        """Store many bodies under a single index write.

        One transaction per body would re-serialise the whole index once per
        article, which is quadratic in the size of the library — slow enough
        on a first sync to look like a hang.
        """

        def _store() -> dict[str, str]:
            written: dict[str, str] = {}
            with index_transaction(data_folder) as index:
                by_id = index.by_id()
                for article_id, markdown in body.bodies.items():
                    article = by_id.get(article_id)
                    if article is None or article.deleted or not markdown.strip():
                        continue
                    write_body(data_folder, article, markdown)
                    written[article_id] = article.body_sha256 or ""
            return written

        try:
            return {"stored": await asyncio.to_thread(_store)}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.put("/api/sync/body/{article_id}")
    async def write_sync_body(article_id: str, body: _BodyRequest) -> dict:
        """Take a body the other side fetched, for a record already merged."""

        if not body.markdown.strip():
            raise HTTPException(status_code=422, detail="Refusing to store an empty body")

        def _store() -> Optional[str]:
            with index_transaction(data_folder) as index:
                article = index.by_id().get(article_id)
                if article is None or article.deleted:
                    return None
                write_body(data_folder, article, body.markdown)
                return article.body_sha256

        try:
            digest = await asyncio.to_thread(_store)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if digest is None:
            raise HTTPException(status_code=404, detail="Article not found")
        return {"body_sha256": digest}

    # -- syncing outward, on behalf of the page -----------------------------

    # One at a time. Two overlapping exchanges would race each other on the
    # index and on the cursors, and the second would carry nothing new.
    sync_running = asyncio.Lock()

    @app.get("/api/sync/config")
    async def sync_config() -> dict:
        """Whether this instance has a hosted copy to sync with.

        The page asks before it shows a sync button, because the hosted copy
        runs this same app and has nowhere of its own to sync to. Only the
        answer to that question is returned — never the URL or the token.
        """
        try:
            load_remote()
        except RemoteError:
            return {"enabled": False}
        return {"enabled": True}

    @app.post("/api/sync/run")
    async def run_sync(request: Request) -> dict:
        """Exchange with the hosted copy now, at the reader's request."""
        if request.headers.get(_SYNC_RUN_HEADER) != _SYNC_RUN_VALUE:
            raise HTTPException(status_code=403, detail="Sync must be started from this page.")
        try:
            remote = load_remote()
        except RemoteError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if sync_running.locked():
            raise HTTPException(status_code=409, detail="A sync is already running.")
        async with sync_running:
            try:
                report = await asyncio.to_thread(_sync_with_remote, data_folder, remote)
            except RemoteError as exc:
                # The library is untouched or partly caught up, and the cursors
                # did not move, so the next sync covers the same ground again.
                raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            "sent": report.sent,
            "received": report.received,
            "bodies_sent": report.bodies_sent,
            "bodies_received": report.bodies_received,
        }

    @app.get("/api/export")
    async def export_archive() -> FileResponse:
        tmp_dir = Path(tempfile.mkdtemp(prefix="ril-export-"))
        filename = export_filename()
        zip_path = tmp_dir / filename
        try:
            await asyncio.to_thread(create_archive, data_folder, zip_path)
        except Exception as exc:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise HTTPException(status_code=500, detail=f"Could not build export: {exc}") from exc
        return FileResponse(
            zip_path,
            media_type="application/zip",
            filename=filename,
            background=BackgroundTask(shutil.rmtree, tmp_dir, ignore_errors=True),
        )

    @app.post("/api/import")
    async def import_archive(request: Request, dry_run: bool = True) -> dict:
        """Replace the whole library with an uploaded backup zip.

        Defaults to `dry_run=true`: the client previews the archive first, then
        repeats the upload with `dry_run=false` once the user has confirmed.
        """
        if request.headers.get(_IMPORT_CONFIRM_HEADER, "") != _IMPORT_CONFIRM_VALUE:
            raise HTTPException(status_code=403, detail="Missing import confirmation header.")

        tmp_dir = Path(tempfile.mkdtemp(prefix="ril-import-"))
        upload = tmp_dir / "upload.zip"
        try:
            size = await _spool_upload(request, upload)
            if size == 0:
                raise HTTPException(status_code=422, detail="No archive was uploaded.")
            try:
                result = await asyncio.to_thread(
                    restore_archive, data_folder, upload, dry_run, True, "the uploaded file"
                )
            except ArchiveError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        return _summary_payload(result)

    return app
