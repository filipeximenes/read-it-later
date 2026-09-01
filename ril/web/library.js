/* The library screen: the filter tabs, the search box, the list, and the
   little form that saves a new URL. */

import { api, ApiError } from './api.js';
import { openArticle } from './reader.js';
import { articleById, emit, on, state } from './store.js';
import { $, domainOf, escHtml, shortAge, toast } from './ui.js';

let searchTimer = null;

export function scheduleLoad() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadArticles, 250);
}

export async function loadArticles() {
  const query = $('search-input').value.trim();
  try {
    state.articles = await api.articles(state.filter, query);
    state.searchError = null;
  } catch (e) {
    // A failed search is worth explaining in place; anything else leaves the
    // list as it was rather than blanking the screen.
    if (query) {
      state.searchError = e instanceof ApiError ? e.message : 'Search is unavailable.';
      state.articles = [];
    } else {
      toast(e.message);
      return;
    }
  }
  renderList();
  if (state.currentId && !articleById(state.currentId)) emit('current-article-gone');
}

export function renderList() {
  const list = $('article-list');
  const empty = $('empty-msg');
  const query = $('search-input').value.trim();

  if (!state.articles.length) {
    list.innerHTML = '';
    empty.hidden = false;
    empty.classList.toggle('search-error', Boolean(state.searchError));
    if (state.searchError) empty.textContent = state.searchError;
    else if (query) empty.innerHTML = '<span class="empty-mark">🔍</span>Nothing matches that search.';
    else if (state.filter === 'unread')
      empty.innerHTML = '<span class="empty-mark">🎉</span>Nothing left to read. Add a URL with +.';
    else empty.innerHTML = '<span class="empty-mark">📥</span>No articles here yet.';
    return;
  }

  empty.hidden = true;
  list.innerHTML = state.articles
    .map((a) => {
      const classes = [
        'article-item',
        a.id === state.currentId ? 'active' : '',
        a.read ? 'is-read' : '',
      ]
        .filter(Boolean)
        .join(' ');
      const bits = [domainOf(a.url), a.author, shortAge(a.saved_at)].filter(Boolean);
      const meta = bits.map(escHtml).join('<span class="item-dot">·</span>');
      const badges =
        (a.read ? '<span class="item-badge badge-read">read</span>' : '') +
        (a.fetch_failed ? '<span class="item-badge badge-fail">failed</span>' : '');
      return `<li><button type="button" class="${classes}" data-id="${escHtml(a.id)}">
        <div class="item-title">${escHtml(a.title)}</div>
        <div class="item-meta">${meta}${badges}</div>
      </button></li>`;
    })
    .join('');
}

export async function loadStats() {
  try {
    state.stats = await api.stats();
  } catch (e) {
    return null;
  }
  $('count-unread').textContent = state.stats.unread;
  $('count-all').textContent = state.stats.total;
  $('count-read').textContent = state.stats.read;
  return state.stats;
}

export function setFilter(name) {
  if (state.filter === name) return;
  state.filter = name;
  $('tabbar')
    .querySelectorAll('.tab')
    .forEach((t) => t.setAttribute('aria-selected', String(t.dataset.filter === name)));
  emit('filter-changed');
  loadArticles();
}

// --- add a URL -----------------------------------------------------------

export function openAddUrl() {
  const bar = $('add-url-bar');
  bar.classList.add('open');
  $('add-url-error').textContent = '';
  const input = $('add-url-input');
  input.value = '';
  input.focus();
}

export function closeAddUrl() {
  $('add-url-bar').classList.remove('open');
  $('add-url-error').textContent = '';
}

export async function submitAddUrl() {
  const input = $('add-url-input');
  const url = input.value.trim();
  if (!url) return;

  const button = $('add-url-submit');
  const error = $('add-url-error');
  error.textContent = '';
  button.disabled = true;
  button.innerHTML = '<span class="spinner"></span>Saving…';
  try {
    const saved = await api.add(url);
    closeAddUrl();
    await loadArticles();
    loadStats();
    if (articleById(saved.id)) openArticle(saved.id);
    else toast('Saved.');
  } catch (e) {
    error.textContent = e.message;
  } finally {
    button.disabled = false;
    button.textContent = 'Save';
  }
}

// --- wiring --------------------------------------------------------------

export function initLibrary() {
  $('search-input').addEventListener('input', scheduleLoad);
  $('search-input').addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      e.target.value = '';
      loadArticles();
      e.target.blur();
    }
  });
  $('add-url-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submitAddUrl();
    if (e.key === 'Escape') closeAddUrl();
  });
  $('tabbar').addEventListener('click', (e) => {
    const tab = e.target.closest('.tab');
    if (tab) setFilter(tab.dataset.filter);
  });
  $('article-list').addEventListener('click', (e) => {
    const item = e.target.closest('.article-item');
    if (item) openArticle(item.dataset.id);
  });

  on('articles-changed', renderList);
  on('stats-stale', loadStats);
  on('reload-articles', loadArticles);
}

/** Keep the selected row on screen when the keyboard moves the selection. */
export function revealActiveItem() {
  const el = $('article-list').querySelector('.article-item.active');
  if (el) el.scrollIntoView({ block: 'nearest' });
}
