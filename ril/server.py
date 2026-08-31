from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from datetime import datetime, timedelta
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
from ril.sync import SYNC_PROTOCOL, apply_incoming

# Import replaces the whole library, so it is guarded by a header a cross-origin
# page cannot send without a CORS preflight that this app never grants.
_IMPORT_CONFIRM_HEADER = "x-ril-confirm"
_IMPORT_CONFIRM_VALUE = "replace-all"
_MAX_IMPORT_BYTES = 2 * 1024 * 1024 * 1024

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
    --red: #f87171;
    --sidebar-w: 320px;
    --safe-l: env(safe-area-inset-left, 0px);
    --safe-r: env(safe-area-inset-right, 0px);
    --safe-b: env(safe-area-inset-bottom, 0px);
    --safe-t: env(safe-area-inset-top, 0px);
  }

  html { -webkit-text-size-adjust: 100%; }
  html, body { height: 100%; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }
  body { overscroll-behavior-y: none; }

  /* Layout — one column by default; the sidebar and reader share the screen
     and swap as you navigate. Two panes appear from the desktop breakpoint. */
  #shell { display: flex; flex-direction: column; height: 100vh; height: 100dvh; }

  /* Top nav */
  #topnav {
    display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
    padding: calc(8px + var(--safe-t)) calc(12px + var(--safe-r)) 8px calc(12px + var(--safe-l));
    background: var(--surface); border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  .nav-lead { display: flex; align-items: center; gap: 8px; order: 1; min-width: 0; }
  .nav-actions { display: flex; align-items: center; gap: 6px; order: 2; margin-left: auto; }
  #topnav .brand { font-weight: 700; font-size: 16px; color: var(--accent); letter-spacing: 0.03em; }

  #tabbar {
    order: 3; flex-basis: 100%; display: flex; gap: 4px;
    overflow-x: auto; scrollbar-width: none; -webkit-overflow-scrolling: touch;
  }
  #tabbar::-webkit-scrollbar { display: none; }

  .tab {
    display: flex; align-items: center; flex-shrink: 0;
    padding: 8px 13px; min-height: 38px; border-radius: 8px; border: none;
    background: transparent; color: var(--text-dim);
    font-size: 14px; font-weight: 500; cursor: pointer; transition: all .15s;
    white-space: nowrap;
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

  /* Icon / action buttons — sized for touch first */
  .icon-btn {
    display: flex; align-items: center; justify-content: center; gap: 6px;
    min-width: 40px; height: 40px; padding: 0 11px; flex-shrink: 0;
    border-radius: 8px; border: 1px solid var(--border);
    background: transparent; color: var(--text-dim);
    font-size: 15px; font-weight: 500; cursor: pointer; transition: all .15s;
  }
  .icon-btn:hover { border-color: var(--accent); color: var(--accent); }
  .btn-label { display: none; }

  #back-btn { display: none; }
  body.reading #back-btn { display: flex; }

  /* Overflow menu */
  #menu-wrap { position: relative; }
  #menu {
    display: none; position: absolute; right: 0; top: calc(100% + 6px); z-index: 20;
    min-width: 200px; padding: 6px;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; box-shadow: 0 16px 40px rgba(0,0,0,0.55);
  }
  #menu.open { display: block; }
  .menu-item {
    display: flex; align-items: center; gap: 10px; width: 100%;
    padding: 10px 12px; min-height: 44px;
    border: none; background: transparent; color: var(--text);
    font-size: 14px; text-align: left; border-radius: 7px; cursor: pointer;
  }
  .menu-item:hover { background: var(--surface2); }
  .menu-item.danger { color: var(--red); }
  .menu-sep { height: 1px; background: var(--border); margin: 6px 4px; }

  /* Add URL bar — its own full-width row until there is space beside the tabs */
  #add-url-bar { display: none; flex-wrap: wrap; align-items: center; gap: 8px; order: 4; flex-basis: 100%; }
  #add-url-bar.open { display: flex; }
  #add-url-input {
    flex: 1; min-width: 0; padding: 10px 12px; border-radius: 8px;
    border: 1px solid var(--border); background: var(--bg);
    color: var(--text); font-size: 16px; outline: none;
  }
  #add-url-input:focus { border-color: var(--accent); }
  #add-url-submit {
    height: 40px; padding: 0 16px; border-radius: 8px; border: none;
    background: var(--accent); color: #fff; font-size: 14px;
    font-weight: 500; cursor: pointer; white-space: nowrap;
    transition: background .15s;
  }
  #add-url-submit:hover:not(:disabled) { background: var(--accent-hover); }
  #add-url-submit:disabled { opacity: 0.6; cursor: default; }
  #add-url-cancel {
    height: 40px; padding: 0 12px; border-radius: 8px; border: 1px solid var(--border);
    background: transparent; color: var(--text-dim); font-size: 14px;
    cursor: pointer; transition: all .15s;
  }
  #add-url-cancel:hover { border-color: var(--text-dim); color: var(--text); }
  #add-url-error { flex-basis: 100%; font-size: 12px; color: var(--red); }

  /* Main area */
  #main { display: flex; flex: 1; min-height: 0; overflow: hidden; }

  /* Sidebar */
  #sidebar {
    width: 100%; flex-shrink: 0;
    overflow-y: auto; -webkit-overflow-scrolling: touch;
    background: var(--surface);
  }
  body.reading #sidebar { display: none; }
  #sidebar-search {
    position: sticky; top: 0; z-index: 1;
    padding: 10px calc(12px + var(--safe-r)) 10px calc(12px + var(--safe-l));
    background: var(--surface);
    border-bottom: 1px solid var(--border);
  }
  #sidebar-search input {
    width: 100%; padding: 11px 12px; border-radius: 8px;
    border: 1px solid var(--border); background: var(--bg);
    color: var(--text); font-size: 16px; outline: none;
  }
  #sidebar-search input:focus { border-color: var(--accent); }
  #article-list { list-style: none; }
  .article-item {
    padding: 14px calc(16px + var(--safe-r)) 14px calc(16px + var(--safe-l));
    border-bottom: 1px solid var(--border);
    cursor: pointer; transition: background .12s;
  }
  .article-item:hover { background: var(--surface2); }
  .article-item.active { background: var(--surface2); border-left: 3px solid var(--accent); }
  .article-item.is-read .item-title { color: var(--read-text); font-weight: 400; }
  .item-title { font-size: 15px; font-weight: 600; line-height: 1.4; color: var(--text); margin-bottom: 4px; }
  .item-meta { font-size: 12px; color: var(--text-dimmer); }
  .item-badge {
    display: inline-block; font-size: 10px; padding: 1px 5px;
    border-radius: 3px; margin-left: 4px; vertical-align: middle;
  }
  .badge-read { background: #1e2d1e; color: var(--green); }
  .badge-fail { background: #2d1e1e; color: var(--red); }
  #empty-msg { padding: 24px 16px; color: var(--text-dim); font-size: 14px; text-align: center; }
  #empty-msg.search-error { color: var(--red); }

  /* Content pane */
  #content-pane {
    display: none; flex: 1;
    overflow-y: auto; -webkit-overflow-scrolling: touch;
    padding: 20px calc(16px + var(--safe-r)) calc(32px + var(--safe-b)) calc(16px + var(--safe-l));
    background: var(--bg);
  }
  body.reading #content-pane { display: block; }
  #placeholder {
    display: flex; align-items: center; justify-content: center;
    height: 100%; color: var(--text-dimmer); font-size: 15px;
  }

  /* Article view */
  #article-view { max-width: 720px; margin: 0 auto; }
  #article-header { margin-bottom: 24px; }
  #article-title { font-size: 22px; font-weight: 700; line-height: 1.3; margin-bottom: 10px; overflow-wrap: break-word; }
  #article-meta { font-size: 13px; color: var(--text-dim); display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
  #article-meta a { color: var(--accent); text-decoration: none; }
  #article-meta a:hover { text-decoration: underline; }
  #article-actions { display: flex; flex-wrap: wrap; gap: 8px; }
  #article-actions button {
    flex: 1 1 auto; min-height: 42px; padding: 0 14px;
    border-radius: 8px; border: 1px solid var(--border);
    background: var(--surface); color: var(--text-dim); font-size: 14px;
    font-weight: 500; cursor: pointer; transition: all .15s; white-space: nowrap;
  }
  #toggle-btn { color: var(--text); }
  #toggle-btn:hover, #refresh-btn:hover { border-color: var(--accent); color: var(--accent); }
  #toggle-btn.is-read { border-color: var(--green); color: var(--green); }
  #delete-btn:hover { border-color: var(--red); color: var(--red); }
  #refresh-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .article-divider { border: none; border-top: 1px solid var(--border); margin: 24px 0; }

  /* Markdown body */
  #article-body { font-size: 17px; line-height: 1.7; color: var(--text); overflow-wrap: break-word; }
  #article-body h1,
  #article-body h2,
  #article-body h3,
  #article-body h4 { font-weight: 700; line-height: 1.3; margin: 1.6em 0 0.5em; color: var(--text); }
  #article-body h1 { font-size: 1.5em; }
  #article-body h2 { font-size: 1.3em; }
  #article-body h3 { font-size: 1.12em; }
  #article-body h4 { font-size: 1em; }
  #article-body p { margin: 0 0 1.1em; }
  #article-body ul, #article-body ol { margin: 0 0 1.1em 1.25em; }
  #article-body li { margin-bottom: 0.3em; }
  #article-body a { color: var(--accent); text-decoration: none; }
  #article-body a:hover { text-decoration: underline; }
  #article-body blockquote {
    border-left: 3px solid var(--accent); margin: 1em 0;
    padding: 10px 14px; background: var(--surface); border-radius: 0 6px 6px 0;
    color: var(--text-dim);
  }
  #article-body code {
    font-family: "SF Mono", "Fira Code", Consolas, monospace;
    font-size: 0.875em; background: var(--surface2);
    padding: 2px 5px; border-radius: 4px; overflow-wrap: break-word;
  }
  #article-body pre {
    background: var(--surface2); border: 1px solid var(--border);
    border-radius: 8px; padding: 14px; overflow-x: auto;
    -webkit-overflow-scrolling: touch; margin: 0 0 1.2em;
  }
  #article-body pre code { background: none; padding: 0; font-size: 0.85em; white-space: pre; }
  #article-body img {
    max-width: 100%; height: auto; border-radius: 8px;
    margin: 12px 0; display: block;
  }
  /* Wide tables scroll on their own instead of stretching the page */
  #article-body table {
    display: block; width: max-content; max-width: 100%;
    overflow-x: auto; -webkit-overflow-scrolling: touch;
    border-collapse: collapse; margin: 0 0 1.2em;
  }
  #article-body th, #article-body td {
    border: 1px solid var(--border); padding: 8px 12px; text-align: left;
  }
  #article-body th { background: var(--surface2); font-weight: 600; }
  #article-body hr { border: none; border-top: 1px solid var(--border); margin: 2em 0; }

  /* Videos section */
  #videos-section { margin-top: 28px; }
  #videos-section h3 { font-size: 14px; font-weight: 600; color: var(--text-dim); margin-bottom: 14px; text-transform: uppercase; letter-spacing: 0.06em; }
  .video-wrapper {
    position: relative; padding-bottom: 56.25%; height: 0; overflow: hidden;
    border-radius: 10px; margin-bottom: 18px; background: #000;
  }
  .video-wrapper iframe { position: absolute; top: 0; left: 0; width: 100%; height: 100%; border: none; }

  /* Modals — full-screen sheets on small screens, centred cards on desktop */
  .overlay { display: none; position: fixed; inset: 0; z-index: 30; background: rgba(0,0,0,0.65); }
  .overlay.open { display: flex; }
  .modal {
    display: flex; flex-direction: column;
    width: 100%; height: 100%;
    background: var(--surface); overflow: hidden;
  }
  .modal-header {
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    padding: calc(14px + var(--safe-t)) 16px 14px;
    border-bottom: 1px solid var(--border); flex-shrink: 0;
  }
  .modal-header h2 { font-size: 17px; font-weight: 700; }
  .modal-close {
    display: flex; align-items: center; justify-content: center;
    width: 40px; height: 40px; flex-shrink: 0;
    border: none; background: transparent; color: var(--text-dim);
    font-size: 18px; cursor: pointer; border-radius: 8px;
  }
  .modal-close:hover { background: var(--surface2); color: var(--text); }
  .modal-body {
    flex: 1; overflow-y: auto; -webkit-overflow-scrolling: touch;
    padding: 18px 16px calc(18px + var(--safe-b));
  }
  .modal-footer {
    display: flex; flex-wrap: wrap; gap: 8px; flex-shrink: 0;
    padding: 14px 16px calc(14px + var(--safe-b));
    border-top: 1px solid var(--border);
  }
  .modal-footer button {
    flex: 1 1 140px; min-height: 44px; padding: 0 16px;
    border-radius: 8px; border: 1px solid var(--border);
    background: var(--surface2); color: var(--text);
    font-size: 14px; font-weight: 600; cursor: pointer; transition: all .15s;
  }
  .modal-footer button:hover:not(:disabled) { border-color: var(--text-dim); }
  .modal-footer button:disabled { opacity: 0.55; cursor: default; }
  .btn-danger { background: #7f1d1d; border-color: #991b1b; color: #fff; }
  .btn-danger:hover:not(:disabled) { background: #991b1b; border-color: #b91c1c; }

  .stats-loading { color: var(--text-dim); font-size: 14px; text-align: center; padding: 30px; }

  /* Import dialog */
  .summary-row {
    display: flex; align-items: baseline; justify-content: space-between; gap: 12px;
    padding: 10px 0; border-bottom: 1px solid var(--border); font-size: 14px;
  }
  .summary-row:last-child { border-bottom: none; }
  .summary-row .k { color: var(--text-dim); }
  .summary-row .v { font-weight: 600; font-variant-numeric: tabular-nums; flex-shrink: 0; }
  .summary-row .v.warn { color: #fbbf24; }
  .summary-row .v.danger { color: var(--red); }
  .summary-title { font-size: 12px; font-weight: 600; color: var(--text-dim); margin: 20px 0 4px; text-transform: uppercase; letter-spacing: 0.06em; }
  .summary-title:first-child { margin-top: 0; }
  .warn-box {
    margin-top: 18px; padding: 12px 14px; border-radius: 9px;
    background: #2d1e1e; border: 1px solid #7f1d1d;
    color: #fca5a5; font-size: 13px; line-height: 1.55;
  }
  .note-box {
    margin-top: 12px; padding: 12px 14px; border-radius: 9px;
    background: var(--surface2); border: 1px solid var(--border);
    color: var(--text-dim); font-size: 13px; line-height: 1.55;
  }
  .file-name { color: var(--text); font-weight: 600; overflow-wrap: anywhere; }

  /* Stats */
  .stat-tiles { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-bottom: 24px; }
  .stat-tile {
    background: var(--surface2); border: 1px solid var(--border);
    border-radius: 10px; padding: 13px 14px;
  }
  .stat-value { font-size: 23px; font-weight: 700; color: var(--text); line-height: 1.1; }
  .stat-value.accent { color: var(--accent); }
  .stat-value.green { color: var(--green); }
  .stat-value.fail { color: var(--red); }
  .stat-label { font-size: 11px; color: var(--text-dim); margin-top: 4px; text-transform: uppercase; letter-spacing: 0.04em; }

  .stat-section { margin-bottom: 26px; }
  .stat-section:last-child { margin-bottom: 0; }
  .stat-section-title { font-size: 12px; font-weight: 600; color: var(--text-dim); margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.06em; }
  .stat-section-head { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: space-between; margin-bottom: 12px; }
  .stat-section-head .stat-section-title { margin-bottom: 0; }
  .period-toggle { display: flex; gap: 2px; background: var(--surface2); border: 1px solid var(--border); border-radius: 7px; padding: 2px; }
  .period-btn { border: none; background: transparent; color: var(--text-dim); font-size: 12px; font-weight: 600; padding: 6px 11px; border-radius: 5px; cursor: pointer; transition: all .12s; }
  .period-btn:hover { color: var(--text); }
  .period-btn.active { background: var(--accent); color: #fff; }

  .progress-bar { height: 10px; background: var(--surface2); border-radius: 6px; overflow: hidden; }
  .progress-fill { height: 100%; background: var(--green); border-radius: 6px; transition: width .3s; }
  .progress-label { font-size: 12px; color: var(--text-dim); margin-top: 8px; }

  .chart-nav { display: flex; align-items: center; justify-content: center; gap: 14px; margin-bottom: 10px; }
  .chart-range { font-size: 12px; color: var(--text-dim); font-variant-numeric: tabular-nums; min-width: 140px; text-align: center; }
  .nav-arrow {
    border: 1px solid var(--border); background: var(--surface2); color: var(--text);
    width: 34px; height: 34px; border-radius: 8px; cursor: pointer; font-size: 16px;
    line-height: 1; display: flex; align-items: center; justify-content: center; transition: all .12s;
    flex-shrink: 0;
  }
  .nav-arrow:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
  .nav-arrow:disabled { opacity: 0.35; cursor: default; }
  .chart-legend { display: flex; gap: 16px; font-size: 12px; color: var(--text-dim); margin-bottom: 10px; }
  .chart-legend .swatch { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 5px; vertical-align: middle; }
  #activity-chart { width: 100%; height: auto; display: block; touch-action: pan-y; }
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

  .rank-row { display: flex; align-items: center; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid var(--border); font-size: 14px; }
  .rank-row:last-child { border-bottom: none; }
  .rank-name { color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-right: 12px; }
  .rank-count { color: var(--text-dim); font-variant-numeric: tabular-nums; flex-shrink: 0; }

  .tag-cloud { display: flex; flex-wrap: wrap; gap: 8px; }
  .tag-chip { font-size: 13px; padding: 5px 11px; border-radius: 14px; background: var(--surface2); border: 1px solid var(--border); color: var(--text); }
  .tag-chip-count { color: var(--text-dim); font-weight: 600; margin-left: 3px; }

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

  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
  }

  /* ---- Tablet and up: the two-pane layout ---- */
  @media (min-width: 860px) {
    #topnav { flex-wrap: nowrap; gap: 4px; height: 52px; padding: 0 20px; }
    .nav-lead { margin-right: 12px; }
    #tabbar { order: 2; flex-basis: auto; overflow: visible; }
    .nav-actions { order: 3; }
    .tab { min-height: 32px; padding: 6px 14px; }
    .icon-btn { height: 34px; min-width: 34px; font-size: 14px; }
    .btn-label { display: inline; }
    body.reading #back-btn { display: none; }

    #add-url-bar { order: 4; flex: 1; flex-basis: auto; flex-wrap: nowrap; max-width: 480px; margin-left: 8px; }
    #add-url-input { padding: 6px 10px; font-size: 13px; }
    #add-url-submit, #add-url-cancel { height: 32px; font-size: 13px; }
    #add-url-error { flex-basis: auto; white-space: nowrap; }

    #sidebar { width: var(--sidebar-w); border-right: 1px solid var(--border); }
    body.reading #sidebar { display: block; }
    #sidebar-search { padding: 12px; }
    #sidebar-search input { padding: 7px 10px; font-size: 13px; }
    .article-item { padding: 12px 14px; }
    .article-item.active { padding-left: 11px; }
    .item-title { font-size: 13px; }
    .item-meta { font-size: 11px; }

    #content-pane { display: block; padding: 40px 48px; }
    #article-header { margin-bottom: 32px; }
    #article-title { font-size: 28px; margin-bottom: 12px; }
    #article-meta { gap: 12px; margin-bottom: 20px; }
    #article-actions button { flex: 0 0 auto; min-height: 34px; padding: 0 16px; font-size: 13px; }
    #article-body { font-size: 16px; line-height: 1.75; }
    #article-body h1 { font-size: 1.7em; }
    #article-body h2 { font-size: 1.35em; }
    .article-divider { margin: 28px 0; }

    .overlay { align-items: flex-start; justify-content: center; padding: 48px 20px; overflow-y: auto; }
    .modal {
      width: 100%; max-width: 620px; height: auto; max-height: calc(100vh - 96px);
      border: 1px solid var(--border); border-radius: 12px;
      box-shadow: 0 24px 60px rgba(0,0,0,0.5);
    }
    .modal-header { padding: 18px 22px; }
    .modal-body { padding: 22px; }
    .modal-footer { padding: 16px 22px; }
    .modal-footer button { flex: 0 0 auto; min-height: 38px; }
    .modal-footer .spacer { flex: 1 1 auto; }

    .stat-tiles { grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 26px; }
    .stat-value { font-size: 26px; }
    .nav-arrow { width: 26px; height: 26px; font-size: 15px; }
    .period-btn { font-size: 11px; padding: 3px 10px; }
    .rank-row { font-size: 13px; padding: 6px 0; }
    .tag-chip { font-size: 12px; padding: 4px 10px; }
  }
</style>
</head>
<body>
<div id="shell">
  <nav id="topnav">
    <div class="nav-lead">
      <button id="back-btn" class="icon-btn" onclick="goBack()" aria-label="Back to list">←</button>
      <span class="brand">ril</span>
    </div>
    <div class="nav-actions">
      <button id="add-btn" class="icon-btn" onclick="openAddUrl()" aria-label="Add URL">+<span class="btn-label">Add URL</span></button>
      <div id="menu-wrap">
        <button id="menu-btn" class="icon-btn" onclick="toggleMenu(event)" aria-label="More actions" aria-haspopup="true" aria-expanded="false">⋯</button>
        <div id="menu" role="menu">
          <button class="menu-item" role="menuitem" onclick="openStats()">📊 Statistics</button>
          <button class="menu-item" role="menuitem" onclick="exportData()">⬇ Export backup (.zip)</button>
          <div class="menu-sep"></div>
          <button class="menu-item danger" role="menuitem" onclick="pickImportFile()">↺ Import backup…</button>
        </div>
      </div>
    </div>
    <div id="tabbar" role="tablist">
      <button class="tab active" data-filter="unread" onclick="switchTab(this)">Unread <span class="tab-count" id="count-unread"></span></button>
      <button class="tab" data-filter="all" onclick="switchTab(this)">All <span class="tab-count" id="count-all"></span></button>
      <button class="tab" data-filter="read" onclick="switchTab(this)">Read <span class="tab-count" id="count-read"></span></button>
    </div>
    <div id="add-url-bar">
      <input type="url" id="add-url-input" placeholder="https://…" onkeydown="addUrlKey(event)" />
      <button id="add-url-submit" onclick="submitAddUrl()">Save</button>
      <button id="add-url-cancel" onclick="closeAddUrl()">Cancel</button>
      <span id="add-url-error"></span>
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

<input type="file" id="import-file" accept=".zip,application/zip" hidden onchange="onImportFile(event)" />

<div id="stats-overlay" class="overlay" onclick="maybeCloseOverlay(event, 'stats-overlay')">
  <div class="modal">
    <div class="modal-header">
      <h2>Statistics</h2>
      <button class="modal-close" onclick="closeStats()" aria-label="Close">✕</button>
    </div>
    <div class="modal-body" id="stats-content"></div>
  </div>
</div>

<div id="import-overlay" class="overlay" onclick="maybeCloseOverlay(event, 'import-overlay')">
  <div class="modal">
    <div class="modal-header">
      <h2>Import backup</h2>
      <button class="modal-close" onclick="closeImport()" aria-label="Close">✕</button>
    </div>
    <div class="modal-body" id="import-content"></div>
    <div class="modal-footer" id="import-footer"></div>
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

function isNarrow() {
  return window.matchMedia('(max-width: 859.98px)').matches;
}

function switchTab(btn) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  currentFilter = btn.dataset.filter;
  // On a single-column screen the reader covers the list, so switching tabs
  // has to reveal the list it just changed.
  if (isNarrow() && document.body.classList.contains('reading')) goBack();
  loadArticles();
}

async function openArticle(id, fromHistory) {
  currentArticleId = id;
  renderList();

  // On narrow screens the reader replaces the list, so the article becomes a
  // history entry and the device back button returns to the list.
  document.body.classList.add('reading');
  if (!fromHistory) {
    history.pushState({ articleId: id }, '');
  }

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
  document.body.classList.remove('reading');
  document.getElementById('placeholder').style.display = 'flex';
  document.getElementById('article-view').style.display = 'none';
  renderList();
}

function goBack() {
  if (history.state && history.state.articleId) history.back();
  else showPlaceholder();
}

window.addEventListener('popstate', e => {
  const id = e.state && e.state.articleId;
  if (id && allArticles.find(a => a.id === id)) openArticle(id, true);
  else showPlaceholder();
});

function escHtml(str) {
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function openAddUrl() {
  closeMenu();
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
  closeMenu();
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

function maybeCloseOverlay(e, id) {
  // Only a tap on the backdrop itself closes; on small screens the sheet fills
  // the viewport so this never fires there.
  if (e.target.id === id) {
    if (id === 'stats-overlay') closeStats();
    else closeImport();
  }
}

function toggleMenu(e) {
  e.stopPropagation();
  const menu = document.getElementById('menu');
  const open = menu.classList.toggle('open');
  document.getElementById('menu-btn').setAttribute('aria-expanded', open ? 'true' : 'false');
}

function closeMenu() {
  document.getElementById('menu').classList.remove('open');
  document.getElementById('menu-btn').setAttribute('aria-expanded', 'false');
}

document.addEventListener('click', e => {
  if (!e.target.closest('#menu-wrap')) closeMenu();
});

function exportData() {
  closeMenu();
  // Let the browser handle the download so large archives never buffer in JS.
  window.location.href = '/api/export';
}

let pendingImportFile = null;

function pickImportFile() {
  closeMenu();
  const input = document.getElementById('import-file');
  input.value = '';
  input.click();
}

async function onImportFile(e) {
  const file = e.target.files && e.target.files[0];
  if (!file) return;
  pendingImportFile = file;
  openImport();
  setImportBody('<div class="stats-loading"><span class="spinner"></span>Reading archive…</div>', '');

  const result = await uploadImport(file, true);
  if (result.error) {
    showImportError(result.error);
    return;
  }
  renderImportPreview(file, result.data);
}

async function uploadImport(file, dryRun) {
  try {
    const res = await fetch(`/api/import?dry_run=${dryRun}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/zip', 'X-RIL-Confirm': 'replace-all' },
      body: file,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) return { error: data.detail || `Import failed (HTTP ${res.status}).` };
    return { data };
  } catch (err) {
    return { error: 'Could not reach the server. Is `ril serve` still running?' };
  }
}

function openImport() {
  document.getElementById('import-overlay').classList.add('open');
}

function closeImport() {
  document.getElementById('import-overlay').classList.remove('open');
  pendingImportFile = null;
}

function setImportBody(bodyHtml, footerHtml) {
  document.getElementById('import-content').innerHTML = bodyHtml;
  document.getElementById('import-footer').innerHTML = footerHtml;
}

function showImportError(message) {
  setImportBody(
    `<div class="warn-box">${escHtml(message)}</div>`,
    '<button onclick="closeImport()">Close</button>'
  );
}

function summaryRow(label, value, cls) {
  return `<div class="summary-row"><span class="k">${escHtml(label)}</span>` +
    `<span class="v ${cls || ''}">${escHtml(String(value))}</span></div>`;
}

function renderImportPreview(file, s) {
  let archiveRows = summaryRow('Articles in the archive', s.article_count) +
    summaryRow('Markdown files', s.file_count);
  if (s.missing_files) archiveRows += summaryRow('Indexed articles with no file', s.missing_files, 'warn');
  if (s.orphan_files) archiveRows += summaryRow('Files not listed in the index', s.orphan_files, 'warn');
  if (s.skipped_entries) archiveRows += summaryRow('Unrelated entries (ignored)', s.skipped_entries, 'warn');

  const body = `
    <div class="summary-title">Archive</div>
    <div class="file-name">${escHtml(file.name)}</div>
    ${archiveRows}
    <div class="summary-title">Your library right now</div>
    ${summaryRow('Articles that will be deleted', s.replaced_articles, s.replaced_articles ? 'danger' : '')}
    <div class="warn-box">
      This replaces your whole library. Everything not in the archive is removed —
      it is not merged with what you have now.
    </div>
    <div class="note-box">
      A snapshot of your current data is saved next to your data folder first, so you
      can undo this with <code>ril import backup &lt;snapshot&gt;</code>.
    </div>`;

  const footer = '<button onclick="closeImport()">Cancel</button>' +
    '<button class="btn-danger" id="import-confirm" onclick="confirmImport()">Replace all data</button>';
  setImportBody(body, footer);
}

async function confirmImport() {
  if (!pendingImportFile) return;
  const file = pendingImportFile;
  const btn = document.getElementById('import-confirm');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>Restoring…';
  }
  document.querySelectorAll('#import-footer button').forEach(b => { b.disabled = true; });

  const result = await uploadImport(file, false);
  if (result.error) {
    showImportError(result.error);
    return;
  }

  const s = result.data;
  const snapshot = s.snapshot
    ? `<div class="note-box">Your previous data was saved as <span class="file-name">${escHtml(s.snapshot)}</span>, next to your data folder.</div>`
    : '';
  setImportBody(
    `<div class="summary-title">Done</div>
     ${summaryRow('Articles restored', s.article_count)}
     ${summaryRow('Articles replaced', s.replaced_articles)}
     ${snapshot}`,
    '<button onclick="closeImport()">Close</button>'
  );

  pendingImportFile = null;
  statsCache = null;
  showPlaceholder();
  await loadArticles();
  loadStats();
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
  if (e.key !== 'Escape') return;
  if (document.getElementById('menu').classList.contains('open')) { closeMenu(); return; }
  if (document.getElementById('import-overlay').classList.contains('open')) { closeImport(); return; }
  if (document.getElementById('stats-overlay').classList.contains('open')) closeStats();
});

history.replaceState({ articleId: null }, '');
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
        {"label": _bucket_label(count - 1 - i + offset, now, unit), "saved": saved[i], "read": read[i]}
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
        outcome = await asyncio.to_thread(
            apply_incoming, data_folder, body.articles, body.since
        )
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
