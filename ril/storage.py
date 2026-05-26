from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from slugify import slugify

from ril.extractor import ExtractedArticle
from ril.models import Article, Index


def compute_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:8]


def load_index(data_folder: Path) -> Index:
    index_file = data_folder / "index.json"
    if not index_file.exists():
        return Index()
    with index_file.open() as f:
        data = json.load(f)
    return Index.model_validate(data)


def save_index(data_folder: Path, index: Index) -> None:
    index_file = data_folder / "index.json"
    with index_file.open("w") as f:
        json.dump(index.model_dump(mode="json"), f, indent=2, default=str)


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
    )

    _write_markdown_file(article_file, article, extracted)

    index = load_index(data_folder)
    index.articles.append(article)
    save_index(data_folder, index)

    return article


def delete_article(data_folder: Path, article: Article) -> None:
    article_file = data_folder / "articles" / article.filename
    if article_file.exists():
        article_file.unlink()

    index = load_index(data_folder)
    index.articles = [a for a in index.articles if a.id != article.id]
    save_index(data_folder, index)


def update_article(data_folder: Path, article: Article) -> None:
    index = load_index(data_folder)
    for i, a in enumerate(index.articles):
        if a.id == article.id:
            index.articles[i] = article
            break
    save_index(data_folder, index)


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

    article_file = data_folder / "articles" / article.filename
    _write_markdown_file(article_file, article, extracted)
    update_article(data_folder, article)
    return article


def get_article_path(data_folder: Path, article: Article) -> Path:
    return data_folder / "articles" / article.filename


def _build_filename(timestamp: datetime, article_id: str, title: str) -> str:
    ts = timestamp.strftime("%Y%m%dT%H%M%SZ")
    slug = slugify(title, max_length=60, separator="-") or "untitled"
    return f"{ts}_{article_id}_{slug}.md"


def _write_markdown_file(
    path: Path,
    article: Article,
    extracted: ExtractedArticle,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

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
    front_matter = "\n".join(front_matter_lines)

    if extracted.fetch_failed:
        body = _failed_fetch_body(extracted)
    else:
        heading = f"# {article.title}\n\n" if article.title and article.title != article.url else ""
        body = heading + (extracted.body_markdown or "")

    content = front_matter + "\n\n" + body.strip() + "\n"
    path.write_text(content, encoding="utf-8")


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


_YAML_SPECIAL_CHARS = set(':#[]{},"&*?|-<>=!%@`\'')


def _yaml_str(value: Optional[str]) -> str:
    if value is None:
        return '""'
    if any(c in _YAML_SPECIAL_CHARS for c in value):
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return value
