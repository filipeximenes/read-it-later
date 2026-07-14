from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ril.extractor import fetch_and_extract
from ril.models import Article, Index
from ril.storage import delete_article, get_article_path, load_index, refresh_article, save_article, update_article

_FRONT_MATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n?", re.DOTALL)

_CLI_SEARCH_TIMEOUT_SEC = 120


class _SearchUnavailableError(RuntimeError):
    pass


class _AddRequest(BaseModel):
    url: str

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
  .tab-count {
    display: inline-block; min-width: 18px; padding: 0 5px;
    margin-left: 5px; border-radius: 9px; font-size: 11px; font-weight: 600;
    background: var(--surface2); color: var(--text-dim); text-align: center;
  }
  .tab.active .tab-count { background: rgba(255,255,255,0.22); color: #fff; }
  .tab-count:empty { display: none; }
  #stats-btn { margin-left: 8px; }

  /* Stats modal */
  #stats-overlay {
    display: none; position: fixed; inset: 0; z-index: 10;
    background: rgba(0,0,0,0.6); align-items: flex-start; justify-content: center;
    padding: 48px 20px; overflow-y: auto;
  }
  #stats-overlay.open { display: flex; }
  #stats-modal {
    width: 100%; max-width: 620px; background: var(--surface);
    border: 1px solid var(--border); border-radius: 12px;
    box-shadow: 0 24px 60px rgba(0,0,0,0.5); overflow: hidden;
  }
  #stats-modal-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 18px 22px; border-bottom: 1px solid var(--border);
  }
  #stats-modal-header h2 { font-size: 17px; font-weight: 700; }
  #stats-close {
    border: none; background: transparent; color: var(--text-dim);
    font-size: 18px; cursor: pointer; line-height: 1; padding: 4px 8px; border-radius: 6px;
  }
  #stats-close:hover { background: var(--surface2); color: var(--text); }
  #stats-content { padding: 22px; }
  .stats-loading { color: var(--text-dim); font-size: 14px; text-align: center; padding: 30px; }

  .stat-tiles { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 26px; }
  .stat-tile {
    background: var(--surface2); border: 1px solid var(--border);
    border-radius: 10px; padding: 14px 16px;
  }
  .stat-value { font-size: 26px; font-weight: 700; color: var(--text); line-height: 1.1; }
  .stat-value.accent { color: var(--accent); }
  .stat-value.green { color: var(--green); }
  .stat-value.fail { color: #f87171; }
  .stat-label { font-size: 11px; color: var(--text-dim); margin-top: 4px; text-transform: uppercase; letter-spacing: 0.04em; }

  .stat-section { margin-bottom: 26px; }
  .stat-section:last-child { margin-bottom: 0; }
  .stat-section-title { font-size: 12px; font-weight: 600; color: var(--text-dim); margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.06em; }
  .stat-section-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
  .stat-section-head .stat-section-title { margin-bottom: 0; }
  .period-toggle { display: flex; gap: 2px; background: var(--surface2); border: 1px solid var(--border); border-radius: 7px; padding: 2px; }
  .period-btn { border: none; background: transparent; color: var(--text-dim); font-size: 11px; font-weight: 600; padding: 3px 10px; border-radius: 5px; cursor: pointer; transition: all .12s; }
  .period-btn:hover { color: var(--text); }
  .period-btn.active { background: var(--accent); color: #fff; }

  .progress-bar { height: 10px; background: var(--surface2); border-radius: 6px; overflow: hidden; }
  .progress-fill { height: 100%; background: var(--green); border-radius: 6px; transition: width .3s; }
  .progress-label { font-size: 12px; color: var(--text-dim); margin-top: 8px; }

  .chart-nav { display: flex; align-items: center; justify-content: center; gap: 14px; margin-bottom: 10px; }
  .chart-range { font-size: 12px; color: var(--text-dim); font-variant-numeric: tabular-nums; min-width: 150px; text-align: center; }
  .nav-arrow {
    border: 1px solid var(--border); background: var(--surface2); color: var(--text);
    width: 26px; height: 26px; border-radius: 6px; cursor: pointer; font-size: 15px;
    line-height: 1; display: flex; align-items: center; justify-content: center; transition: all .12s;
  }
  .nav-arrow:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
  .nav-arrow:disabled { opacity: 0.35; cursor: default; }
  .chart-legend { display: flex; gap: 16px; font-size: 12px; color: var(--text-dim); margin-bottom: 10px; }
  .chart-legend .swatch { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 5px; vertical-align: middle; }
  #activity-chart { width: 100%; height: auto; display: block; }
  #activity-chart .bar-saved { fill: var(--accent); }
  #activity-chart .bar-read { fill: var(--green); }
  #activity-chart .axis-label { fill: var(--text-dimmer); font-size: 9px; }
  #activity-chart .grid-line { stroke: var(--border); stroke-width: 1; }
  #activity-chart .hover-zone { fill: transparent; }
  #activity-chart .hover-zone:hover { fill: rgba(91,127,255,0.08); }
  #chart-wrap { position: relative; }
  #chart-tooltip {
    display: none; position: absolute; pointer-events: none; z-index: 2;
    background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
    padding: 8px 10px; font-size: 12px; white-space: nowrap;
    box-shadow: 0 6px 20px rgba(0,0,0,0.45);
  }
  #chart-tooltip .tip-date { font-weight: 600; color: var(--text); margin-bottom: 5px; }
  #chart-tooltip .tip-row { display: flex; align-items: center; gap: 6px; color: var(--text-dim); line-height: 1.6; }
  #chart-tooltip .tip-row b { color: var(--text); margin-left: 14px; font-variant-numeric: tabular-nums; }
  #chart-tooltip .tip-dot { width: 8px; height: 8px; border-radius: 2px; display: inline-block; flex-shrink: 0; }
  #chart-tooltip .tip-dot.saved { background: var(--accent); }
  #chart-tooltip .tip-dot.read { background: var(--green); }

  .rank-row { display: flex; align-items: center; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid var(--border); font-size: 13px; }
  .rank-row:last-child { border-bottom: none; }
  .rank-name { color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-right: 12px; }
  .rank-count { color: var(--text-dim); font-variant-numeric: tabular-nums; flex-shrink: 0; }

  .tag-cloud { display: flex; flex-wrap: wrap; gap: 8px; }
  .tag-chip { font-size: 12px; padding: 4px 10px; border-radius: 14px; background: var(--surface2); border: 1px solid var(--border); color: var(--text); }
  .tag-chip-count { color: var(--text-dim); font-weight: 600; margin-left: 3px; }

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
  #empty-msg.search-error { color: #f87171; }

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
  #article-actions { display: flex; gap: 8px; align-items: center; }
  #toggle-btn {
    padding: 7px 16px; border-radius: 6px; border: 1px solid var(--border);
    background: var(--surface); color: var(--text); font-size: 13px;
    font-weight: 500; cursor: pointer; transition: all .15s;
  }
  #toggle-btn:hover { border-color: var(--accent); color: var(--accent); }
  #toggle-btn.is-read { border-color: var(--green); color: var(--green); }
  #delete-btn {
    padding: 7px 16px; border-radius: 6px; border: 1px solid var(--border);
    background: var(--surface); color: var(--text-dim); font-size: 13px;
    font-weight: 500; cursor: pointer; transition: all .15s;
  }
  #delete-btn:hover { border-color: #f87171; color: #f87171; }
  #refresh-btn {
    padding: 7px 16px; border-radius: 6px; border: 1px solid var(--border);
    background: var(--surface); color: var(--text-dim); font-size: 13px;
    font-weight: 500; cursor: pointer; transition: all .15s;
  }
  #refresh-btn:hover { border-color: var(--accent); color: var(--accent); }
  #refresh-btn:disabled { opacity: 0.5; cursor: not-allowed; }
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

  /* Add URL bar */
  #add-url-bar {
    display: none; align-items: center; gap: 8px;
    margin-left: auto; flex: 1; max-width: 480px;
  }
  #add-url-bar.open { display: flex; }
  #add-url-input {
    flex: 1; padding: 6px 10px; border-radius: 6px;
    border: 1px solid var(--border); background: var(--bg);
    color: var(--text); font-size: 13px; outline: none;
  }
  #add-url-input:focus { border-color: var(--accent); }
  #add-url-submit {
    padding: 6px 14px; border-radius: 6px; border: none;
    background: var(--accent); color: #fff; font-size: 13px;
    font-weight: 500; cursor: pointer; white-space: nowrap;
    transition: background .15s;
  }
  #add-url-submit:hover:not(:disabled) { background: var(--accent-hover); }
  #add-url-submit:disabled { opacity: 0.6; cursor: default; }
  #add-url-cancel {
    padding: 6px 10px; border-radius: 6px; border: 1px solid var(--border);
    background: transparent; color: var(--text-dim); font-size: 13px;
    cursor: pointer; transition: all .15s;
  }
  #add-url-cancel:hover { border-color: var(--text-dim); color: var(--text); }
  #add-btn {
    margin-left: auto; padding: 6px 14px; border-radius: 6px;
    border: 1px solid var(--border); background: transparent;
    color: var(--text-dim); font-size: 13px; font-weight: 500;
    cursor: pointer; transition: all .15s;
  }
  #add-btn:hover { border-color: var(--accent); color: var(--accent); }
  #add-url-error { font-size: 12px; color: #f87171; white-space: nowrap; }

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
    <button class="tab active" data-filter="unread" onclick="switchTab(this)">Unread <span class="tab-count" id="count-unread"></span></button>
    <button class="tab" data-filter="all" onclick="switchTab(this)">All <span class="tab-count" id="count-all"></span></button>
    <button class="tab" data-filter="read" onclick="switchTab(this)">Read <span class="tab-count" id="count-read"></span></button>
    <button id="stats-btn" class="tab" onclick="openStats()">📊 Stats</button>
    <button id="add-btn" onclick="openAddUrl()">+ Add URL</button>
    <div id="add-url-bar">
      <input type="url" id="add-url-input" placeholder="https://…" onkeydown="addUrlKey(event)" />
      <span id="add-url-error"></span>
      <button id="add-url-submit" onclick="submitAddUrl()">Save</button>
      <button id="add-url-cancel" onclick="closeAddUrl()">Cancel</button>
    </div>
  </nav>
  <div id="main">
    <aside id="sidebar">
      <div id="sidebar-search">
        <input type="search" id="search-input" placeholder="Search articles…" oninput="scheduleLoadArticles()" />
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
          <div id="article-actions">
            <button id="toggle-btn" onclick="toggleRead()"></button>
            <button id="refresh-btn" onclick="refreshArticle()">↺ Refresh</button>
            <button id="delete-btn" onclick="deleteArticle()">Delete</button>
          </div>
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

<div id="stats-overlay" onclick="maybeCloseStats(event)">
  <div id="stats-modal">
    <div id="stats-modal-header">
      <h2>Statistics</h2>
      <button id="stats-close" onclick="closeStats()">✕</button>
    </div>
    <div id="stats-content"></div>
  </div>
</div>

<script>
let allArticles = [];
let currentFilter = 'unread';
let currentArticleId = null;
let searchLoadError = null;
let loadArticlesDebounced = null;

marked.setOptions({ breaks: true, gfm: true });

function scheduleLoadArticles() {
  clearTimeout(loadArticlesDebounced);
  loadArticlesDebounced = setTimeout(() => loadArticles(), 250);
}

async function loadArticles() {
  const queryRaw = document.getElementById('search-input').value.trim();
  const qs = `/api/articles?filter=${currentFilter}`;
  const url = queryRaw.length ? `${qs}&q=${encodeURIComponent(queryRaw)}` : qs;

  const res = await fetch(url);

  if (!res.ok) {
    if (queryRaw.length) {
      let detail = 'Search unavailable (install ripgrep or grep, or retry).';
      try {
        const data = await res.json();
        if (typeof data.detail === 'string') detail = data.detail;
      } catch (e) { /* ignore */ }
      searchLoadError = detail;
      allArticles = [];
    } else {
      searchLoadError = null;
      try {
        allArticles = await res.json();
      } catch (e) {
        allArticles = [];
      }
    }
    renderList();
    if (currentArticleId && !allArticles.find(a => a.id === currentArticleId)) {
      showPlaceholder();
    }
    return;
  }

  searchLoadError = null;
  allArticles = await res.json();
  renderList();
  // Clear content pane if current article not in new list
  if (currentArticleId && !allArticles.find(a => a.id === currentArticleId)) {
    showPlaceholder();
  }
}

function renderList() {
  const query = document.getElementById('search-input').value.trim();
  const list = document.getElementById('article-list');
  const emptyMsg = document.getElementById('empty-msg');

  if (!allArticles.length) {
    list.innerHTML = '';
    emptyMsg.style.display = '';
    if (searchLoadError && query.length) {
      emptyMsg.textContent = searchLoadError;
      emptyMsg.classList.add('search-error');
      return;
    }
    emptyMsg.classList.remove('search-error');
    emptyMsg.textContent = query.length ? 'No articles match your search.' : 'No articles here.';
    return;
  }

  emptyMsg.classList.remove('search-error');
  emptyMsg.style.display = 'none';
  list.innerHTML = allArticles.map(a => {
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
  // Optional whitespace allowed around markers (\\s below in the regexp).
  return html.replace(
    /<p>\\s*video-embed:\\s*(?:<a[^>]*href="([^"]+)"[^>]*>[^<]*<\\/a>|([^<\\s]+))\\s*<\\/p>/gi,
    (_, hrefUrl, textUrl) => {
      const embedUrl = toEmbedUrl(hrefUrl || textUrl);
      if (!embedUrl) return '';
      return `<div class="video-wrapper"><iframe src="${embedUrl}" allowfullscreen allow="autoplay; encrypted-media"></iframe></div>`;
    }
  );
}

function renderVideos(article) {
  if (!article) return;
  const videoUrls = [...(article.video_urls || [])];
  // If the article URL itself is a video (e.g. a YouTube link saved directly),
  // include it first so the player is always shown.
  const selfEmbed = toEmbedUrl(article.url);
  if (selfEmbed && !videoUrls.some(u => toEmbedUrl(u) === selfEmbed)) {
    videoUrls.unshift(article.url);
  }
  if (!videoUrls.length) return;
  const embeds = videoUrls.map(toEmbedUrl).filter(Boolean);
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
  m = url.match(/youtu\\.be\\/([A-Za-z0-9_-]+)/);
  if (m) return `https://www.youtube.com/embed/${m[1]}`;
  // Vimeo
  m = url.match(/vimeo\\.com\\/(\\d+)/);
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
  loadStats();
}

async function deleteArticle() {
  if (!currentArticleId) return;
  const article = allArticles.find(a => a.id === currentArticleId);
  const title = article ? article.title : 'this article';
  if (!confirm(`Delete "${title}"?\n\nThis cannot be undone.`)) return;

  const res = await fetch(`/api/articles/${currentArticleId}`, { method: 'DELETE' });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    alert(data.detail || 'Failed to delete article.');
    return;
  }
  showPlaceholder();
  await loadArticles();
  loadStats();
}

async function refreshArticle() {
  if (!currentArticleId) return;
  const btn = document.getElementById('refresh-btn');
  btn.disabled = true;
  btn.textContent = '↺ Refreshing…';
  try {
    const res = await fetch(`/api/articles/${currentArticleId}/refresh`, { method: 'POST' });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      alert(data.detail || 'Failed to refresh article.');
      return;
    }
    const updated = await res.json();
    const idx = allArticles.findIndex(a => a.id === currentArticleId);
    if (idx !== -1) allArticles[idx] = updated;
    renderArticleHeader(updated);
    renderList();
    const contentRes = await fetch(`/api/articles/${currentArticleId}/content`);
    const contentData = await contentRes.json();
    document.getElementById('article-body').innerHTML = injectInlineVideos(marked.parse(contentData.content));
    if (!document.querySelector('#article-body .video-wrapper')) {
      renderVideos(updated);
    }
  } finally {
    btn.disabled = false;
    btn.textContent = '↺ Refresh';
  }
}

function showPlaceholder() {
  currentArticleId = null;
  document.getElementById('placeholder').style.display = 'flex';
  document.getElementById('article-view').style.display = 'none';
}

function escHtml(str) {
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function openAddUrl() {
  document.getElementById('add-btn').style.display = 'none';
  document.getElementById('add-url-bar').classList.add('open');
  document.getElementById('add-url-error').textContent = '';
  const input = document.getElementById('add-url-input');
  input.value = '';
  input.focus();
}

function closeAddUrl() {
  document.getElementById('add-url-bar').classList.remove('open');
  document.getElementById('add-btn').style.display = '';
  document.getElementById('add-url-error').textContent = '';
}

function addUrlKey(e) {
  if (e.key === 'Enter') submitAddUrl();
  if (e.key === 'Escape') closeAddUrl();
}

async function submitAddUrl() {
  const input = document.getElementById('add-url-input');
  const url = input.value.trim();
  if (!url) return;

  const submitBtn = document.getElementById('add-url-submit');
  const errorEl = document.getElementById('add-url-error');
  errorEl.textContent = '';
  submitBtn.disabled = true;
  submitBtn.innerHTML = '<span class="spinner"></span>Saving…';

  try {
    const res = await fetch('/api/articles', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();
    if (!res.ok) {
      errorEl.textContent = data.detail || 'Failed to save article.';
      return;
    }
    closeAddUrl();
    await loadArticles();
    loadStats();
    // Open the newly added article if it exists in the current view
    const found = allArticles.find(a => a.id === data.id);
    if (found) openArticle(data.id);
  } catch (err) {
    errorEl.textContent = 'Network error. Please try again.';
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = 'Save';
  }
}

let statsCache = null;
const ACTIVITY_PERIODS = [
  { key: '8w',  label: '8 wk',  unit: 'week',  count: 8 },
  { key: '26w', label: '26 wk', unit: 'week',  count: 26 },
  { key: '12m', label: '12 mo', unit: 'month', count: 12 },
];
let activityPeriod = '8w';
let activityOffset = 0;
let activityData = [];
let activityReqKey = null;

function setActivityPeriod(key) {
  if (!ACTIVITY_PERIODS.find(x => x.key === key)) return;
  activityPeriod = key;
  activityOffset = 0;  // jump back to the most-recent window
  document.querySelectorAll('.period-btn').forEach(b => b.classList.toggle('active', b.dataset.key === key));
  loadActivity();
}

function shiftActivity(dir) {
  // dir = +1 goes older (back in time), -1 goes newer (toward now).
  const p = ACTIVITY_PERIODS.find(x => x.key === activityPeriod);
  const next = activityOffset + dir * p.count;
  if (next < 0) return;
  activityOffset = next;
  loadActivity();
}

async function loadActivity() {
  const p = ACTIVITY_PERIODS.find(x => x.key === activityPeriod);
  const slot = document.getElementById('activity-chart-slot');
  if (slot) slot.innerHTML = '<div class="stats-loading"><span class="spinner"></span></div>';
  const reqKey = `${activityPeriod}:${activityOffset}`;
  activityReqKey = reqKey;
  let payload = { buckets: [], has_older: false, has_newer: activityOffset > 0 };
  try {
    const res = await fetch(`/api/activity?unit=${p.unit}&count=${p.count}&offset=${activityOffset}`);
    if (res.ok) payload = await res.json();
  } catch (e) { /* fall through to empty */ }
  if (activityReqKey !== reqKey) return;  // a newer request superseded this one
  activityData = payload.buckets || [];
  const slot2 = document.getElementById('activity-chart-slot');
  if (slot2) slot2.innerHTML = buildActivityChart(activityData);
  const range = document.getElementById('chart-range');
  if (range) {
    range.textContent = activityData.length
      ? `${activityData[0].label} – ${activityData[activityData.length - 1].label}`
      : '—';
  }
  const older = document.getElementById('nav-older');
  const newer = document.getElementById('nav-newer');
  if (older) older.disabled = !payload.has_older;
  if (newer) newer.disabled = !payload.has_newer;
}

async function loadStats() {
  try {
    const res = await fetch('/api/stats');
    if (!res.ok) return null;
    statsCache = await res.json();
    document.getElementById('count-unread').textContent = statsCache.unread;
    document.getElementById('count-all').textContent = statsCache.total;
    document.getElementById('count-read').textContent = statsCache.read;
    return statsCache;
  } catch (e) {
    return null;
  }
}

async function openStats() {
  const overlay = document.getElementById('stats-overlay');
  const content = document.getElementById('stats-content');
  overlay.classList.add('open');
  if (statsCache) renderStats(statsCache);
  else content.innerHTML = '<div class="stats-loading"><span class="spinner"></span>Loading…</div>';
  const fresh = await loadStats();
  renderStats(fresh || statsCache);
}

function closeStats() {
  document.getElementById('stats-overlay').classList.remove('open');
}

function maybeCloseStats(e) {
  if (e.target.id === 'stats-overlay') closeStats();
}

function renderStats(s) {
  const content = document.getElementById('stats-content');
  if (!s) {
    content.innerHTML = '<div class="stats-loading">Statistics unavailable.</div>';
    return;
  }
  if (!s.total) {
    content.innerHTML = '<div class="stats-loading">No articles saved yet.</div>';
    return;
  }

  const fmtDays = v => (v == null ? '—' : v + 'd');
  const tiles = [
    { label: 'Saved this week', value: s.saved_this_week, cls: 'accent' },
    { label: 'Read this week', value: s.read_this_week, cls: 'green' },
    { label: 'Median time to read', value: fmtDays(s.median_days_to_read), cls: '' },
    { label: 'Saved this month', value: s.saved_this_month, cls: 'accent' },
    { label: 'Read this month', value: s.read_this_month, cls: 'green' },
    { label: 'Oldest unread', value: fmtDays(s.oldest_unread_days), cls: '' },
  ];
  const tilesHtml = tiles.map(t =>
    `<div class="stat-tile"><div class="stat-value ${t.cls}">${t.value}</div><div class="stat-label">${t.label}</div></div>`
  ).join('');

  const progress = `<div class="stat-section">
    <div class="stat-section-title">Reading progress</div>
    <div class="progress-bar"><div class="progress-fill" style="width:${s.read_pct}%"></div></div>
    <div class="progress-label">${s.read_pct}% read · ${s.read} of ${s.total} articles</div>
  </div>`;

  activityPeriod = '8w';
  activityOffset = 0;
  const periodBtns = ACTIVITY_PERIODS.map(p =>
    `<button class="period-btn${p.key === activityPeriod ? ' active' : ''}" data-key="${p.key}" onclick="setActivityPeriod('${p.key}')">${p.label}</button>`
  ).join('');
  const chart = `<div class="stat-section">
    <div class="stat-section-head">
      <div class="stat-section-title">Activity</div>
      <div class="period-toggle">${periodBtns}</div>
    </div>
    <div class="chart-legend">
      <span><span class="swatch" style="background:var(--accent)"></span>Saved</span>
      <span><span class="swatch" style="background:var(--green)"></span>Read</span>
    </div>
    <div class="chart-nav">
      <button class="nav-arrow" id="nav-older" onclick="shiftActivity(1)" aria-label="Older">‹</button>
      <span class="chart-range" id="chart-range">—</span>
      <button class="nav-arrow" id="nav-newer" onclick="shiftActivity(-1)" aria-label="Newer" disabled>›</button>
    </div>
    <div id="activity-chart-slot"><div class="stats-loading"><span class="spinner"></span></div></div>
  </div>`;

  const authorsHtml = s.top_authors.length ? `<div class="stat-section">
    <div class="stat-section-title">Top authors</div>
    ${s.top_authors.map(([name, count]) =>
      `<div class="rank-row"><span class="rank-name">${escHtml(name)}</span><span class="rank-count">${count}</span></div>`
    ).join('')}
  </div>` : '';

  const tagsHtml = s.top_tags.length ? `<div class="stat-section">
    <div class="stat-section-title">Top tags</div>
    <div class="tag-cloud">${s.top_tags.map(([tag, count]) =>
      `<span class="tag-chip">${escHtml(tag)}<span class="tag-chip-count">${count}</span></span>`
    ).join('')}</div>
  </div>` : '';

  content.innerHTML = `<div class="stat-tiles">${tilesHtml}</div>${progress}${chart}${authorsHtml}${tagsHtml}`;
  loadActivity();
}

function buildActivityChart(weekly) {
  if (!weekly || !weekly.length) return '';
  const W = 560, H = 200, padL = 26, padR = 8, padT = 10, padB = 30;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const n = weekly.length;
  const maxVal = Math.max(1, ...weekly.flatMap(d => [d.saved, d.read]));
  const groupW = plotW / n;
  const barW = Math.max(4, Math.min(16, groupW / 2 - 4));
  const y = v => padT + plotH - (v / maxVal) * plotH;

  // Gridlines + y ticks at 0, mid, max
  const ticks = [0, Math.round(maxVal / 2), maxVal].filter((v, i, a) => a.indexOf(v) === i);
  const grid = ticks.map(v =>
    `<line class="grid-line" x1="${padL}" y1="${y(v)}" x2="${W - padR}" y2="${y(v)}"></line>` +
    `<text class="axis-label" x="${padL - 5}" y="${y(v) + 3}" text-anchor="end">${v}</text>`
  ).join('');

  // Thin x-axis labels when there are many buckets so they don't collide.
  const labelEvery = n > 14 ? Math.ceil(n / 10) : 1;
  const bars = weekly.map((d, i) => {
    const cx = padL + groupW * i + groupW / 2;
    const savedX = cx - barW - 1, readX = cx + 1;
    const s = `<rect class="bar-saved" x="${savedX}" y="${y(d.saved)}" width="${barW}" height="${padT + plotH - y(d.saved)}" rx="2"></rect>`;
    const r = `<rect class="bar-read" x="${readX}" y="${y(d.read)}" width="${barW}" height="${padT + plotH - y(d.read)}" rx="2"></rect>`;
    const showLbl = (i % labelEvery === 0) || (i === n - 1);
    const lbl = showLbl ? `<text class="axis-label" x="${cx}" y="${H - 10}" text-anchor="middle">${escHtml(d.label)}</text>` : '';
    return s + r + lbl;
  }).join('');

  // Transparent per-week hover zones (drawn last so they sit on top and capture events).
  const zones = weekly.map((d, i) =>
    `<rect class="hover-zone" x="${padL + groupW * i}" y="${padT}" width="${groupW}" height="${plotH}" rx="3" onmousemove="showChartTip(event, ${i})" onmouseleave="hideChartTip()"></rect>`
  ).join('');

  return `<div id="chart-wrap">
    <svg id="activity-chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Weekly saved and read activity">${grid}${bars}${zones}</svg>
    <div id="chart-tooltip"></div>
  </div>`;
}

function showChartTip(evt, i) {
  const wk = activityData && activityData[i];
  if (!wk) return;
  const tip = document.getElementById('chart-tooltip');
  const wrap = document.getElementById('chart-wrap');
  if (!tip || !wrap) return;
  tip.innerHTML = `<div class="tip-date">${escHtml(wk.label)}</div>
    <div class="tip-row"><span class="tip-dot saved"></span>Saved <b>${wk.saved}</b></div>
    <div class="tip-row"><span class="tip-dot read"></span>Read <b>${wk.read}</b></div>`;
  tip.style.display = 'block';
  const wrapRect = wrap.getBoundingClientRect();
  const x = evt.clientX - wrapRect.left;
  const y = evt.clientY - wrapRect.top;
  const left = Math.max(0, Math.min(x + 14, wrap.clientWidth - tip.offsetWidth - 2));
  const top = Math.max(0, y - tip.offsetHeight - 10);
  tip.style.left = left + 'px';
  tip.style.top = top + 'px';
}

function hideChartTip() {
  const tip = document.getElementById('chart-tooltip');
  if (tip) tip.style.display = 'none';
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.getElementById('stats-overlay').classList.contains('open')) closeStats();
});

loadArticles();
loadStats();
</script>
</body>
</html>"""


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
            (a for a in index.articles if a.read),
            key=lambda a: _naive(a.read_at or a.saved_at),
            reverse=True,
        )
    if filter_name == "all":
        return sorted(index.articles, key=lambda a: _naive(a.saved_at), reverse=True)
    return sorted(
        (a for a in index.articles if not a.read),
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
        {"label": _bucket_label(count - 1 - i + offset, now, unit), "saved": saved[i], "read": read[i]}
        for i in range(count)
    ]


def _compute_stats(index: Index) -> dict:
    articles = index.articles
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
    # How long read articles sat between being saved and read (median is robust
    # against the occasional years-old article finally getting read).
    durations = sorted(
        max(0, (_naive(a.read_at) - _naive(a.saved_at)).days)
        for a in articles
        if a.read and a.read_at is not None
    )
    if durations:
        mid = len(durations) // 2
        median_ttr = durations[mid] if len(durations) % 2 else round((durations[mid - 1] + durations[mid]) / 2)
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


def build_app(data_folder: Path) -> FastAPI:
    app = FastAPI(title="Read It Later", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_HTML)

    @app.get("/api/articles")
    async def list_articles(filter: str = "unread", q: Optional[str] = None) -> list[dict]:
        index = load_index(data_folder)
        tab_list = _tab_articles(filter, index)

        needle = (q or "").strip().lower()
        if not needle:
            return [a.model_dump(mode="json") for a in tab_list]

        try:
            file_basenames = await asyncio.to_thread(_matching_filenames_via_cli, data_folder, needle)
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
        buckets = _bucket_activity(index.articles, now, unit, count, offset)
        oldest = offset + count
        has_older = any(
            _bucket_index(_naive(a.saved_at), now, unit) >= oldest
            or (a.read_at is not None and _bucket_index(_naive(a.read_at), now, unit) >= oldest)
            for a in index.articles
        )
        return {"buckets": buckets, "has_older": has_older, "has_newer": offset > 0}

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
        delete_article(data_folder, article)

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

    return app
