from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from ril.models import Article
from ril.storage import get_article_path, load_index, update_article

_FRONT_MATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n?", re.DOTALL)

_YT_WATCH_RE = re.compile(r"(?:https?://)?(?:www\.)?youtube\.com/watch\?.*?v=([A-Za-z0-9_-]+)")
_YT_SHORT_RE = re.compile(r"(?:https?://)?(?:www\.)?youtu\.be/([A-Za-z0-9_-]+)")
_VIMEO_RE = re.compile(r"(?:https?://)?(?:www\.)?vimeo\.com/(\d+)")

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Read It Later</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg: #0f1117;
    --surface: #1a1d27;
    --surface2: #22263a;
    --border: #2e3347;
    --accent: #5b7fff;
    --accent-hover: #7b9bff;
    --text: #e2e8f0;
    --text-dim: #8892a4;
    --text-dimmer: #505a6d;
    --read-bg: #111827;
    --read-text: #6b7280;
    --green: #34d399;
    --sidebar-w: 320px;
  }

  html, body { height: 100%; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }

  /* Layout */
  #shell { display: flex; flex-direction: column; height: 100vh; }

  /* Top nav */
  #topnav {
    display: flex; align-items: center; gap: 4px;
    padding: 0 20px; height: 52px;
    background: var(--surface); border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  #topnav .brand { font-weight: 700; font-size: 15px; color: var(--accent); margin-right: 16px; letter-spacing: 0.03em; }
  .tab {
    padding: 6px 14px; border-radius: 6px; border: none;
    background: transparent; color: var(--text-dim);
    font-size: 14px; font-weight: 500; cursor: pointer; transition: all .15s;
  }
  .tab:hover { background: var(--surface2); color: var(--text); }
  .tab.active { background: var(--accent); color: #fff; }

  /* Main area */
  #main { display: flex; flex: 1; overflow: hidden; }

  /* Sidebar */
  #sidebar {
    width: var(--sidebar-w); flex-shrink: 0;
    border-right: 1px solid var(--border);
    overflow-y: auto; background: var(--surface);
  }
  #sidebar-search {
    position: sticky; top: 0; z-index: 1;
    padding: 12px; background: var(--surface);
    border-bottom: 1px solid var(--border);
  }
  #sidebar-search input {
    width: 100%; padding: 7px 10px; border-radius: 6px;
    border: 1px solid var(--border); background: var(--bg);
    color: var(--text); font-size: 13px; outline: none;
  }
  #sidebar-search input:focus { border-color: var(--accent); }
  #article-list { list-style: none; }
  .article-item {
    padding: 12px 14px; border-bottom: 1px solid var(--border);
    cursor: pointer; transition: background .12s;
  }
  .article-item:hover { background: var(--surface2); }
  .article-item.active { background: var(--surface2); border-left: 3px solid var(--accent); padding-left: 11px; }
  .article-item.is-read .item-title { color: var(--read-text); font-weight: 400; }
  .item-title { font-size: 13px; font-weight: 600; line-height: 1.4; color: var(--text); margin-bottom: 3px; }
  .item-meta { font-size: 11px; color: var(--text-dimmer); }
  .item-badge {
    display: inline-block; font-size: 10px; padding: 1px 5px;
    border-radius: 3px; margin-left: 4px; vertical-align: middle;
  }
  .badge-read { background: #1e2d1e; color: var(--green); }
  .badge-fail { background: #2d1e1e; color: #f87171; }
  #empty-msg { padding: 24px 16px; color: var(--text-dim); font-size: 13px; text-align: center; }

  /* Content pane */
  #content-pane {
    flex: 1; overflow-y: auto; padding: 40px 48px;
    background: var(--bg);
  }
  #placeholder {
    display: flex; align-items: center; justify-content: center;
    height: 100%; color: var(--text-dimmer); font-size: 15px;
  }

  /* Article view */
  #article-view { max-width: 720px; margin: 0 auto; }
  #article-header { margin-bottom: 32px; }
  #article-title { font-size: 28px; font-weight: 700; line-height: 1.3; margin-bottom: 12px; }
  #article-meta { font-size: 13px; color: var(--text-dim); display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }
  #article-meta a { color: var(--accent); text-decoration: none; }
  #article-meta a:hover { text-decoration: underline; }
  #toggle-btn {
    padding: 7px 16px; border-radius: 6px; border: 1px solid var(--border);
    background: var(--surface); color: var(--text); font-size: 13px;
    font-weight: 500; cursor: pointer; transition: all .15s;
  }
  #toggle-btn:hover { border-color: var(--accent); color: var(--accent); }
  #toggle-btn.is-read { border-color: var(--green); color: var(--green); }
  .article-divider { border: none; border-top: 1px solid var(--border); margin: 28px 0; }

  /* Markdown body */
  #article-body { font-size: 16px; line-height: 1.75; color: var(--text); }
  #article-body h1,
  #article-body h2,
  #article-body h3,
  #article-body h4 { font-weight: 700; line-height: 1.3; margin: 1.6em 0 0.5em; color: var(--text); }
  #article-body h1 { font-size: 1.7em; }
  #article-body h2 { font-size: 1.35em; }
  #article-body h3 { font-size: 1.15em; }
  #article-body h4 { font-size: 1em; }
  #article-body p { margin: 0 0 1.1em; }
  #article-body ul, #article-body ol { margin: 0 0 1.1em 1.5em; }
  #article-body li { margin-bottom: 0.3em; }
  #article-body a { color: var(--accent); text-decoration: none; }
  #article-body a:hover { text-decoration: underline; }
  #article-body blockquote {
    border-left: 3px solid var(--accent); margin: 1em 0;
    padding: 10px 16px; background: var(--surface); border-radius: 0 6px 6px 0;
    color: var(--text-dim);
  }
  #article-body code {
    font-family: "SF Mono", "Fira Code", Consolas, monospace;
    font-size: 0.875em; background: var(--surface2);
    padding: 2px 5px; border-radius: 4px;
  }
  #article-body pre {
    background: var(--surface2); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px; overflow-x: auto;
    margin: 0 0 1.2em;
  }
  #article-body pre code { background: none; padding: 0; font-size: 0.9em; }
  #article-body img {
    max-width: 100%; height: auto; border-radius: 8px;
    margin: 12px 0; display: block;
  }
  #article-body table { border-collapse: collapse; width: 100%; margin: 0 0 1.2em; }
  #article-body th, #article-body td {
    border: 1px solid var(--border); padding: 8px 12px; text-align: left;
  }
  #article-body th { background: var(--surface2); font-weight: 600; }
  #article-body hr { border: none; border-top: 1px solid var(--border); margin: 2em 0; }

  /* Videos section */
  #videos-section { margin-top: 32px; }
  #videos-section h3 { font-size: 15px; font-weight: 600; color: var(--text-dim); margin-bottom: 16px; text-transform: uppercase; letter-spacing: 0.06em; }
  .video-wrapper {
    position: relative; padding-bottom: 56.25%; height: 0; overflow: hidden;
    border-radius: 10px; margin-bottom: 20px; background: #000;
  }
  .video-wrapper iframe { position: absolute; top: 0; left: 0; width: 100%; height: 100%; border: none; }

  /* Scrollbar */
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  /* Loading spinner */
  .spinner {
    display: inline-block; width: 18px; height: 18px;
    border: 2px solid var(--border); border-top-color: var(--accent);
    border-radius: 50%; animation: spin 0.7s linear infinite;
    vertical-align: middle; margin-right: 8px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<div id="shell">
  <nav id="topnav">
    <span class="brand">ril</span>
    <button class="tab active" data-filter="unread" onclick="switchTab(this)">Unread</button>
    <button class="tab" data-filter="all" onclick="switchTab(this)">All</button>
    <button class="tab" data-filter="read" onclick="switchTab(this)">Read</button>
  </nav>
  <div id="main">
    <aside id="sidebar">
      <div id="sidebar-search">
        <input type="search" id="search-input" placeholder="Search articles…" oninput="renderList()" />
      </div>
      <ul id="article-list"></ul>
      <div id="empty-msg" style="display:none"></div>
    </aside>
    <section id="content-pane">
      <div id="placeholder">Select an article to read</div>
      <div id="article-view" style="display:none">
        <div id="article-header">
          <div id="article-title"></div>
          <div id="article-meta"></div>
          <button id="toggle-btn" onclick="toggleRead()"></button>
        </div>
        <hr class="article-divider">
        <div id="article-body"></div>
        <div id="videos-section" style="display:none">
          <hr class="article-divider">
          <h3>Videos</h3>
          <div id="videos-container"></div>
        </div>
      </div>
    </section>
  </div>
</div>

<script>
let allArticles = [];
let currentFilter = 'unread';
let currentArticleId = null;

marked.setOptions({ breaks: true, gfm: true });

async function loadArticles() {
  const res = await fetch(`/api/articles?filter=${currentFilter}`);
  allArticles = await res.json();
  renderList();
  // Clear content pane if current article not in new list
  if (currentArticleId && !allArticles.find(a => a.id === currentArticleId)) {
    showPlaceholder();
  }
}

function renderList() {
  const query = document.getElementById('search-input').value.trim().toLowerCase();
  const list = document.getElementById('article-list');
  const emptyMsg = document.getElementById('empty-msg');
  const filtered = query
    ? allArticles.filter(a => a.title.toLowerCase().includes(query) || (a.author || '').toLowerCase().includes(query))
    : allArticles;

  if (!filtered.length) {
    list.innerHTML = '';
    emptyMsg.style.display = '';
    emptyMsg.textContent = query ? 'No articles match your search.' : 'No articles here.';
    return;
  }

  emptyMsg.style.display = 'none';
  list.innerHTML = filtered.map(a => {
    const isActive = a.id === currentArticleId ? ' active' : '';
    const isRead = a.read ? ' is-read' : '';
    const readBadge = a.read ? '<span class="item-badge badge-read">read</span>' : '';
    const failBadge = a.fetch_failed ? '<span class="item-badge badge-fail">failed</span>' : '';
    const meta = [a.author, a.published_date].filter(Boolean).join(' · ');
    return `<li class="article-item${isActive}${isRead}" onclick="openArticle('${a.id}')" data-id="${a.id}">
      <div class="item-title">${escHtml(a.title)}${readBadge}${failBadge}</div>
      ${meta ? `<div class="item-meta">${escHtml(meta)}</div>` : ''}
    </li>`;
  }).join('');
}

function switchTab(btn) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  currentFilter = btn.dataset.filter;
  loadArticles();
}

async function openArticle(id) {
  currentArticleId = id;
  renderList();

  document.getElementById('placeholder').style.display = 'none';
  const view = document.getElementById('article-view');
  view.style.display = 'block';
  document.getElementById('article-body').innerHTML = '<span class="spinner"></span> Loading…';
  document.getElementById('videos-section').style.display = 'none';

  const article = allArticles.find(a => a.id === id);
  if (article) renderArticleHeader(article);

  const res = await fetch(`/api/articles/${id}/content`);
  const data = await res.json();
  document.getElementById('article-body').innerHTML = injectInlineVideos(marked.parse(data.content));

  // Show the fallback Videos section only when the body has no inline embeds
  // (i.e. articles saved before inline-marker extraction was added).
  if (!document.querySelector('#article-body .video-wrapper')) {
    renderVideos(article);
  }
}

function renderArticleHeader(article) {
  document.getElementById('article-title').textContent = article.title;

  const parts = [];
  if (article.author) parts.push(`<span>${escHtml(article.author)}</span>`);
  if (article.published_date) parts.push(`<span>${escHtml(article.published_date)}</span>`);
  parts.push(`<a href="${escHtml(article.url)}" target="_blank" rel="noopener">Original ↗</a>`);
  document.getElementById('article-meta').innerHTML = parts.join('<span style="color:var(--border)">·</span>');

  const btn = document.getElementById('toggle-btn');
  if (article.read) {
    btn.textContent = '↩ Mark as unread';
    btn.classList.add('is-read');
  } else {
    btn.textContent = '✓ Mark as read';
    btn.classList.remove('is-read');
  }
}

function injectInlineVideos(html) {
  // Replace <p>video-embed:URL</p> markers (inserted by the extractor) with
  // real iframe embeds. Handles two marked.js rendering variants:
  //   Plain:      <p>video-embed:https://...</p>
  //   GFM linked: <p>video-embed:<a href="https://...">https://...</a></p>
  // Extra \s* guards against any whitespace marked may insert around the content.
  return html.replace(
    /<p>\s*video-embed:\s*(?:<a[^>]*href="([^"]+)"[^>]*>[^<]*<\/a>|([^<\s]+))\s*<\/p>/gi,
    (_, hrefUrl, textUrl) => {
      const embedUrl = toEmbedUrl(hrefUrl || textUrl);
      if (!embedUrl) return '';
      return `<div class="video-wrapper"><iframe src="${embedUrl}" allowfullscreen allow="autoplay; encrypted-media"></iframe></div>`;
    }
  );
}

function renderVideos(article) {
  if (!article || !article.video_urls || !article.video_urls.length) return;
  const embeds = article.video_urls.map(toEmbedUrl).filter(Boolean);
  if (!embeds.length) return;

  const container = document.getElementById('videos-container');
  container.innerHTML = embeds.map(url =>
    `<div class="video-wrapper"><iframe src="${url}" allowfullscreen allow="autoplay; encrypted-media"></iframe></div>`
  ).join('');
  document.getElementById('videos-section').style.display = 'block';
}

function toEmbedUrl(url) {
  // YouTube watch
  let m = url.match(/[?&]v=([A-Za-z0-9_-]+)/);
  if (m) return `https://www.youtube.com/embed/${m[1]}`;
  // YouTube short
  m = url.match(/youtu\.be\/([A-Za-z0-9_-]+)/);
  if (m) return `https://www.youtube.com/embed/${m[1]}`;
  // Vimeo
  m = url.match(/vimeo\.com\/(\d+)/);
  if (m) return `https://player.vimeo.com/video/${m[1]}`;
  // Already an embed URL — pass through
  if (url.includes('/embed/') || url.includes('player.vimeo')) return url;
  return null;
}

async function toggleRead() {
  if (!currentArticleId) return;
  const res = await fetch(`/api/articles/${currentArticleId}/toggle-read`, { method: 'POST' });
  const updated = await res.json();
  const idx = allArticles.findIndex(a => a.id === currentArticleId);
  if (idx !== -1) allArticles[idx] = updated;
  renderArticleHeader(updated);
  renderList();
}

function showPlaceholder() {
  currentArticleId = null;
  document.getElementById('placeholder').style.display = 'flex';
  document.getElementById('article-view').style.display = 'none';
}

function escHtml(str) {
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

loadArticles();
</script>
</body>
</html>"""


def _strip_front_matter(content: str) -> str:
    return _FRONT_MATTER_RE.sub("", content, count=1).lstrip()


def build_app(data_folder: Path) -> FastAPI:
    app = FastAPI(title="Read It Later", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_HTML)

    @app.get("/api/articles")
    async def list_articles(filter: str = "unread") -> list[dict]:
        index = load_index(data_folder)
        if filter == "read":
            articles = [a for a in index.articles if a.read]
        elif filter == "all":
            articles = index.articles
        else:
            articles = [a for a in index.articles if not a.read]
        return [a.model_dump(mode="json") for a in articles]

    @app.get("/api/articles/{article_id}/content")
    async def article_content(article_id: str) -> dict:
        index = load_index(data_folder)
        article = index.find_by_id(article_id)
        if article is None:
            raise HTTPException(status_code=404, detail="Article not found")
        path = get_article_path(data_folder, article)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Article file not found")
        raw = path.read_text(encoding="utf-8")
        return {"content": _strip_front_matter(raw)}

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

    return app
