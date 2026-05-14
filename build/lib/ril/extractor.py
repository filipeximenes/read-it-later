from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import httpx
import trafilatura
import trafilatura.settings
from trafilatura.metadata import extract_metadata


@dataclass
class ExtractedArticle:
    url: str
    title: str
    author: Optional[str] = None
    description: Optional[str] = None
    published_date: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    body_markdown: str = ""
    fetch_failed: bool = False
    error: Optional[str] = None


def fetch_and_extract(url: str) -> ExtractedArticle:
    try:
        html = _fetch_html(url)
    except Exception as exc:
        return ExtractedArticle(
            url=url,
            title=url,
            fetch_failed=True,
            error=f"Failed to fetch URL: {exc}",
        )

    try:
        return _extract(url, html)
    except Exception as exc:
        return ExtractedArticle(
            url=url,
            title=url,
            fetch_failed=True,
            error=f"Failed to extract content: {exc}",
        )


def _fetch_html(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    response = httpx.get(url, headers=headers, follow_redirects=True, timeout=30)
    response.raise_for_status()
    return response.text


def _extract(url: str, html: str) -> ExtractedArticle:
    config = trafilatura.settings.use_config()
    config.set("DEFAULT", "EXTRACTION_TIMEOUT", "0")

    # Fetch metadata and body separately — trafilatura returns a plain str
    # for the body when output_format="markdown", so metadata must be extracted
    # via extract_metadata() to get the structured fields.
    meta = extract_metadata(html, default_url=url)

    body_markdown: Optional[str] = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_comments=False,
        include_tables=True,
        favor_recall=True,
        config=config,
    )

    if not body_markdown or not body_markdown.strip():
        return _fallback_extract(url, html, meta)

    title = (meta.title if meta and meta.title else None) or url
    author = _join_or_none(meta.author if meta else None)
    description = (meta.description if meta and meta.description else None)
    published_date = _date_str(meta.date if meta else None)
    tags = _split_tags((meta.tags or meta.categories) if meta else None)

    return ExtractedArticle(
        url=url,
        title=title,
        author=author,
        description=description,
        published_date=published_date,
        tags=tags,
        body_markdown=body_markdown,
    )


def _fallback_extract(url: str, html: str, meta: object = None) -> ExtractedArticle:
    """Best-effort extraction using markdownify when trafilatura yields nothing."""
    from markdownify import markdownify

    body_markdown = markdownify(
        html, heading_style="ATX", strip=["script", "style", "nav", "footer"]
    )
    title = (meta.title if meta and meta.title else None) or url  # type: ignore[union-attr]
    author = _join_or_none(meta.author if meta else None)  # type: ignore[union-attr]
    description = (meta.description if meta and meta.description else None)  # type: ignore[union-attr]
    published_date = _date_str(meta.date if meta else None)  # type: ignore[union-attr]
    tags = _split_tags((meta.tags or meta.categories) if meta else None)  # type: ignore[union-attr]
    return ExtractedArticle(
        url=url,
        title=title,
        author=author,
        description=description,
        published_date=published_date,
        tags=tags,
        body_markdown=body_markdown,
    )


def _join_or_none(value: object) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v) or None
    return str(value) if str(value).strip() else None


def _date_str(value: object) -> Optional[str]:
    if value is None:
        return None
    return str(value).strip() or None


def _split_tags(value: object) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(t).strip() for t in value if t]
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    return []
