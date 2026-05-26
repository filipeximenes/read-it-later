from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura
import trafilatura.settings
from trafilatura.metadata import extract_metadata

_VIDEO_PROVIDERS = re.compile(
    r"(youtube\.com|youtu\.be|vimeo\.com|dailymotion\.com|player\.vimeo\.com)",
    re.IGNORECASE,
)

_MARKDOWN_IMAGE_RE = re.compile(r"!\[.*?\]\((https?://[^\s)]+)\)")

# Matches a <figure class="*embed*"> block that wraps a video <iframe>.
# Used to replace the whole block with an inline text marker before trafilatura runs,
# because trafilatura treats embed figures as non-content and strips them entirely.
_EMBED_FIGURE_RE = re.compile(
    r'<figure[^>]*class="[^"]*(?:wp-block-embed|embed)[^"]*"[^>]*>'
    r'.*?<iframe[^>]*\bsrc="([^"]*)"[^>]*>'
    r'.*?</figure>',
    re.DOTALL | re.IGNORECASE,
)


@dataclass
class ExtractedArticle:
    url: str
    title: str
    author: Optional[str] = None
    description: Optional[str] = None
    published_date: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    body_markdown: str = ""
    image_urls: list[str] = field(default_factory=list)
    video_urls: list[str] = field(default_factory=list)
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
    cookies = _get_browser_cookies(url)
    response = httpx.get(url, headers=headers, cookies=cookies, follow_redirects=True, timeout=30)
    response.raise_for_status()
    return response.text


def _get_browser_cookies(url: str) -> dict[str, str]:
    """Load session cookies from installed browsers for the URL's domain.

    Tries each supported browser individually and merges the results so that
    a failure in one browser (e.g. Safari permission error) does not prevent
    cookies from being loaded from others. Returns an empty dict silently if
    the library is not installed or no browser has cookies for the domain.
    """
    try:
        import browser_cookie3
    except ImportError:
        return {}

    domain = urlparse(url).netloc
    browser_fns = [
        browser_cookie3.chrome,
        browser_cookie3.firefox,
        browser_cookie3.safari,
    ]

    combined: dict[str, str] = {}
    for fn in browser_fns:
        try:
            for c in fn(domain_name=domain):
                combined[c.name] = c.value
        except Exception:
            continue
    return combined


def _extract(url: str, html: str) -> ExtractedArticle:
    config = trafilatura.settings.use_config()
    config.set("DEFAULT", "EXTRACTION_TIMEOUT", "0")

    # Fetch metadata and body separately — trafilatura returns a plain str
    # for the body when output_format="markdown", so metadata must be extracted
    # via extract_metadata() to get the structured fields.
    meta = extract_metadata(html, default_url=url)

    preprocessed_html = _preprocess_video_embeds(html, url)
    video_urls = _extract_video_urls(html, url)

    body_markdown: Optional[str] = trafilatura.extract(
        preprocessed_html,
        url=url,
        output_format="markdown",
        include_comments=False,
        include_tables=True,
        include_images=True,
        favor_recall=True,
        config=config,
    )

    if not body_markdown or not body_markdown.strip():
        return _fallback_extract(url, html, meta, video_urls)

    title = (meta.title if meta and meta.title else None) or url
    author = _join_or_none(meta.author if meta else None)
    description = (meta.description if meta and meta.description else None)
    published_date = _date_str(meta.date if meta else None)
    tags = _split_tags((meta.tags or meta.categories) if meta else None)

    image_urls = _collect_image_urls(body_markdown, meta.image if meta else None)

    return ExtractedArticle(
        url=url,
        title=title,
        author=author,
        description=description,
        published_date=published_date,
        tags=tags,
        body_markdown=body_markdown,
        image_urls=image_urls,
        video_urls=video_urls,
    )


def _fallback_extract(
    url: str, html: str, meta: object = None, video_urls: Optional[list[str]] = None
) -> ExtractedArticle:
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

    meta_image = getattr(meta, "image", None) if meta else None
    image_urls = _collect_image_urls(body_markdown, meta_image)
    if video_urls is None:
        video_urls = _extract_video_urls(html, url)

    return ExtractedArticle(
        url=url,
        title=title,
        author=author,
        description=description,
        published_date=published_date,
        tags=tags,
        body_markdown=body_markdown,
        image_urls=image_urls,
        video_urls=video_urls,
    )


class _MediaParser(HTMLParser):
    """Collects <video src> and <iframe src> for known video providers."""

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.video_urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attr_dict = dict(attrs)
        src = attr_dict.get("src") or ""
        if tag == "video" and src:
            absolute = urljoin(self.base_url, src)
            if absolute not in self.video_urls:
                self.video_urls.append(absolute)
        elif tag == "iframe" and src and _VIDEO_PROVIDERS.search(src):
            absolute = urljoin(self.base_url, src)
            if absolute not in self.video_urls:
                self.video_urls.append(absolute)


def _extract_video_urls(html: str, base_url: str) -> list[str]:
    parser = _MediaParser(base_url)
    parser.feed(html)
    return parser.video_urls


class _VideoEmbedPreprocessor(HTMLParser):
    """Replaces <iframe> video embeds with an <a> tag so trafilatura keeps them as links."""

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attr_dict = dict(attrs)
        src = attr_dict.get("src") or ""
        if tag == "iframe" and src and _VIDEO_PROVIDERS.search(src):
            absolute = urljoin(self.base_url, src)
            # A plain paragraph with a distinctive marker keeps position in
            # the trafilatura-extracted markdown (unlike <a> or <img> inside
            # embed figures, which trafilatura strips as non-content).
            self._parts.append(f"<p>video-embed:{absolute}</p>")
        else:
            attr_str = "".join(
                f' {k}="{v}"' if v is not None else f" {k}" for k, v in attrs
            )
            self._parts.append(f"<{tag}{attr_str}>")

    def handle_endtag(self, tag: str) -> None:
        self._parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def handle_comment(self, data: str) -> None:
        self._parts.append(f"<!--{data}-->")

    def handle_entityref(self, name: str) -> None:
        self._parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._parts.append(f"&#{name};")

    def get_output(self) -> str:
        return "".join(self._parts)


def _preprocess_video_embeds(html: str, base_url: str) -> str:
    # Replace <figure class="*embed*">...<iframe src="URL">...</figure> blocks first.
    # Trafilatura treats those figures as embed non-content and strips them entirely,
    # so we must hoist the video marker out to a plain <p> before trafilatura sees it.
    def _replace_embed_figure(m: re.Match) -> str:
        src = m.group(1)
        if _VIDEO_PROVIDERS.search(src):
            absolute = urljoin(base_url, src)
            return f"<p>video-embed:{absolute}</p>"
        return m.group(0)

    html = _EMBED_FIGURE_RE.sub(_replace_embed_figure, html)

    # Handle any remaining standalone video iframes (not wrapped in embed figures).
    preprocessor = _VideoEmbedPreprocessor(base_url)
    preprocessor.feed(html)
    return preprocessor.get_output()


def _collect_image_urls(body_markdown: str, meta_image: Optional[str]) -> list[str]:
    """Return deduplicated image URLs: og:image first, then inline images from body."""
    seen: set[str] = set()
    urls: list[str] = []

    def _add(u: str) -> None:
        parsed = urlparse(u)
        if parsed.scheme in ("http", "https") and u not in seen:
            seen.add(u)
            urls.append(u)

    if meta_image:
        _add(meta_image)

    for match in _MARKDOWN_IMAGE_RE.finditer(body_markdown):
        _add(match.group(1))

    return urls


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
