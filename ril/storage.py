from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from slugify import slugify

from ril.extractor import ExtractedArticle
from ril.models import INDEX_VERSION, Article, Index, utcnow

INDEX_NAME = "index.json"
INDEX_LOCK_NAME = ".index.lock"
# Sync cursors belong to this copy of the library, not to the library.
SYNC_STATE_NAME = ".sync-state.json"
_INDEX_TEMP_PREFIX = ".index-"


def body_digest(markdown: str) -> str:
    """Digest of an article body, so an unchanged body is never sent again."""
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def compute_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:8]


def is_internal_file(name: str) -> bool:
    """True for bookkeeping files that are not part of the library itself.

    The lock and any half-written index belong to this process, not to the
    data. Nothing should back them up or restore them.
    """
    return name in (INDEX_LOCK_NAME, SYNC_STATE_NAME) or name.startswith(_INDEX_TEMP_PREFIX)


def load_index(data_folder: Path) -> Index:
    index_file = data_folder / INDEX_NAME
    if not index_file.exists():
        return Index()
    with index_file.open() as f:
        data = json.load(f)
    index = Index.model_validate(data)
    # Parsing filled in whatever an older file lacked, so what is in memory is
    # now the current shape. Recording that here means the next write saves it.
    index.version = INDEX_VERSION
    return index


def save_index(data_folder: Path, index: Index) -> None:
    """Replace the index in one step, so a crash cannot truncate it.

    The whole file is written to a temporary name beside it, flushed to disk,
    and then moved over the old one. A reader sees either the previous index
    or the new one, never a half-written file.
    """
    data_folder.mkdir(parents=True, exist_ok=True)
    # Serialised before the temporary file exists, so a model that fails to
    # serialise leaves nothing behind to clean up.
    payload = json.dumps(index.model_dump(mode="json"), indent=2, default=str)

    descriptor, temp_name = tempfile.mkstemp(
        dir=data_folder, prefix=_INDEX_TEMP_PREFIX, suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, data_folder / INDEX_NAME)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


@contextmanager
def _locked(data_folder: Path) -> Iterator[None]:
    """Hold the index lock, waiting for any other writer to finish."""
    data_folder.mkdir(parents=True, exist_ok=True)
    lock_file = data_folder / INDEX_LOCK_NAME
    with lock_file.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def index_transaction(data_folder: Path) -> Iterator[Index]:
    """Read the index, let the caller change it, and write it back as one step.

    Every change to the index goes through here. Holding the lock across both
    the read and the write is what stops two writers — a `ril` command and a
    running `ril serve`, or two requests to that server — from each loading
    the same index and one silently dropping the other's change.

    A failure inside the block abandons the change rather than saving half.
    """
    with _locked(data_folder):
        index = load_index(data_folder)
        yield index
        save_index(data_folder, index)


def save_article(
    data_folder: Path,
    extracted: ExtractedArticle,
    read: bool = False,
    saved_at: Optional[datetime] = None,
) -> Article:
    effective_saved_at = saved_at if saved_at is not None else datetime.now(timezone.utc)
    if effective_saved_at.tzinfo is None:
        effective_saved_at = effective_saved_at.replace(tzinfo=timezone.utc)
    else:
        effective_saved_at = effective_saved_at.astimezone(timezone.utc)

    article_id = compute_id(extracted.url)
    filename = _build_filename(effective_saved_at, article_id, extracted.title)
    article_file = data_folder / "articles" / filename

    article = Article(
        id=article_id,
        url=extracted.url,
        title=extracted.title,
        author=extracted.author,
        description=extracted.description,
        published_date=extracted.published_date,
        tags=extracted.tags,
        image_urls=extracted.image_urls,
        video_urls=extracted.video_urls,
        saved_at=effective_saved_at,
        read=read,
        read_at=effective_saved_at if read else None,
        filename=filename,
        fetch_failed=extracted.fetch_failed,
        content_updated_at=effective_saved_at,
        state_updated_at=effective_saved_at,
        # Now, not `saved_at`: an import can carry an old save date, but this
        # record is known to exist as of this moment, and that is what has to
        # beat any earlier delete on the other side.
        existence_updated_at=utcnow(),
    )

    _write_markdown_file(article_file, article, extracted)

    with index_transaction(data_folder) as index:
        # Replace rather than append: ids come from the URL, so saving a URL
        # that was deleted earlier must revive that one row instead of leaving
        # a tombstone and a live record sharing an id.
        index.articles = [a for a in index.articles if a.id != article.id]
        index.articles.append(article)

    return article


def delete_article(data_folder: Path, article: Article) -> None:
    """Delete an article, leaving a tombstone so the delete can travel.

    The markdown file goes, because that is what takes up room. The row stays
    with a `deleted_at`, because a row that simply vanished is impossible to
    tell from one the other side has not sent yet, and would come back on the
    next sync.
    """
    article_file = get_article_path(data_folder, article)
    if article_file.exists():
        article_file.unlink()

    with index_transaction(data_folder) as index:
        existing = index.by_id().get(article.id)
        if existing is None:
            return
        existing.mark_deleted()
        existing.body_sha256 = None


def update_article(data_folder: Path, article: Article) -> None:
    with index_transaction(data_folder) as index:
        for i, a in enumerate(index.articles):
            if a.id == article.id:
                index.articles[i] = article
                break


def refresh_article(
    data_folder: Path,
    article: Article,
    extracted: ExtractedArticle,
) -> Article:
    article.title = extracted.title
    article.author = extracted.author
    article.description = extracted.description
    article.published_date = extracted.published_date
    article.tags = extracted.tags
    article.image_urls = extracted.image_urls
    article.video_urls = extracted.video_urls
    article.fetch_failed = extracted.fetch_failed
    # The content clock has to move, or the new body never reaches the other
    # side: sync sends what changed since the last cursor, and nothing here
    # would say that anything did.
    article.touch_content()

    article_file = get_article_path(data_folder, article)
    _write_markdown_file(article_file, article, extracted)
    update_article(data_folder, article)
    return article


def get_article_path(data_folder: Path, article: Article) -> Path:
    """Path to an article's markdown file, always inside `{data_folder}/articles`.

    `filename` comes from index.json, which an imported backup can supply, so it
    is treated as untrusted input rather than as a trusted path fragment.
    """
    articles_dir = (data_folder / "articles").resolve()
    path = (articles_dir / article.filename).resolve()
    if path.parent != articles_dir:
        raise ValueError(f"Unsafe article filename: {article.filename!r}")
    return path


def _build_filename(timestamp: datetime, article_id: str, title: str) -> str:
    ts = timestamp.strftime("%Y%m%dT%H%M%SZ")
    slug = slugify(title, max_length=60, separator="-") or "untitled"
    return f"{ts}_{article_id}_{slug}.md"


# The front matter is regenerated from the article record whenever a body is
# written, so a body can travel between machines on its own.
_FRONT_MATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n?", re.DOTALL)


def read_body(data_folder: Path, article: Article) -> str:
    """The markdown of an article, without its front matter."""
    path = get_article_path(data_folder, article)
    if not path.exists():
        return ""
    raw = path.read_text(encoding="utf-8")
    return _FRONT_MATTER_RE.sub("", raw, count=1).lstrip()


def write_body(data_folder: Path, article: Article, body_markdown: str) -> None:
    """Write a body that came from somewhere else, and record its digest.

    The front matter is rebuilt from the record rather than sent along with
    the body, so the file always agrees with the index.
    """
    path = get_article_path(data_folder, article)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = body_markdown.strip()
    path.write_text(_front_matter(article) + "\n\n" + body + "\n", encoding="utf-8")
    article.body_sha256 = body_digest(body)


def _front_matter(article: Article) -> str:
    front_matter_lines = [
        "---",
        f"id: {article.id}",
        f"title: {_yaml_str(article.title)}",
        f"url: {article.url}",
    ]
    if article.author:
        front_matter_lines.append(f"author: {_yaml_str(article.author)}")
    if article.published_date:
        front_matter_lines.append(f"published_date: {article.published_date}")
    front_matter_lines.append(f"saved_at: {article.saved_at.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    if article.tags:
        tags_str = ", ".join(article.tags)
        front_matter_lines.append(f"tags: [{tags_str}]")
    if article.image_urls:
        front_matter_lines.append("images:")
        for img_url in article.image_urls:
            front_matter_lines.append(f"  - {img_url}")
    if article.video_urls:
        front_matter_lines.append("videos:")
        for vid_url in article.video_urls:
            front_matter_lines.append(f"  - {vid_url}")
    if article.fetch_failed:
        front_matter_lines.append("fetch_failed: true")
    front_matter_lines.append("---")
    return "\n".join(front_matter_lines)


def _write_markdown_file(
    path: Path,
    article: Article,
    extracted: ExtractedArticle,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    front_matter = _front_matter(article)

    if extracted.fetch_failed:
        body = _failed_fetch_body(extracted)
    else:
        heading = f"# {article.title}\n\n" if article.title and article.title != article.url else ""
        body = heading + (extracted.body_markdown or "")

    body = body.strip()
    content = front_matter + "\n\n" + body + "\n"
    path.write_text(content, encoding="utf-8")
    article.body_sha256 = body_digest(body)


def _failed_fetch_body(extracted: ExtractedArticle) -> str:
    lines = [
        "> **Note:** This article could not be fetched automatically.",
        ">",
        f"> **URL:** {extracted.url}",
    ]
    if extracted.error:
        lines.append(">")
        lines.append(f"> **Error:** {extracted.error}")
    return "\n".join(lines)


_YAML_SPECIAL_CHARS = set(":#[]{},\"&*?|-<>=!%@`'")


def _yaml_str(value: Optional[str]) -> str:
    if value is None:
        return '""'
    if any(c in _YAML_SPECIAL_CHARS for c in value):
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return value
