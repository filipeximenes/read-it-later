/* The reading screen.

   Three things matter here and shape the rest of the file:
   the article gets the whole screen (the chrome steps aside as you read down
   and returns the moment you scroll up), every reading action stays within
   thumb reach in the dock at the bottom, and coming back to a half-read
   article puts you back where you stopped. */

import { api } from './api.js';
import { articleById, emit, neighbour, on, state } from './store.js';
import { $, closeMenus, domainOf, escHtml, readingMinutes, show, toast } from './ui.js';

const POSITIONS_KEY = 'ril.positions';
const POSITIONS_KEPT = 300;

let contentToken = 0;
let userScrolled = false;

// --- opening and closing -------------------------------------------------

export async function openArticle(id, fromHistory) {
  const article = articleById(id);
  if (!article) return;

  if (state.currentId && state.currentId !== id) savePosition();
  state.currentId = id;
  emit('articles-changed');

  // The list is the screen behind every article, so stepping from one article
  // to the next replaces the history entry instead of stacking another one:
  // Back and Escape then always lead back to the list, never through a trail
  // of articles already read.
  const wasReading = document.body.classList.contains('reading');
  document.body.classList.add('reading', 'has-article');
  setChrome(true);
  closeMenus();
  if (!fromHistory) {
    const entry = { articleId: id };
    if (wasReading) history.replaceState(entry, '');
    else history.pushState(entry, '');
  }

  show($('placeholder'), false);
  show($('article-view'), true);
  renderHeader(article, null);
  $('reader-scroll').scrollTop = 0;
  resetScrollTracking();
  $('article-body').innerHTML = '<div class="loading-line"><span class="spinner"></span>Loading…</div>';
  show($('videos-section'), false);
  renderNext(id);

  const token = ++contentToken;
  let markdown;
  try {
    markdown = (await api.content(id)).content;
  } catch (e) {
    if (token !== contentToken) return;
    $('article-body').innerHTML = `<div class="warn-box">${escHtml(e.message)}</div>`;
    return;
  }
  if (token !== contentToken) return;  // another article was opened meanwhile
  renderBody(article, markdown);
  renderHeader(article, readingMinutes(markdown));
  restorePosition(id);
}

export function closeReader() {
  savePosition();
  state.currentId = null;
  contentToken++;
  document.body.classList.remove('reading', 'has-article');
  resetScrollTracking();
  setChrome(true);
  show($('article-view'), false);
  show($('placeholder'), true);
  $('reader-progress-fill').style.width = '0%';
  emit('articles-changed');
}

/** The back affordance: unwind the history entry the article pushed. */
export function goBack() {
  if (history.state && history.state.articleId) history.back();
  else closeReader();
}

// --- rendering -----------------------------------------------------------

function renderHeader(article, minutes) {
  $('article-title').textContent = article.title;
  $('reader-bar-title').textContent = article.title;

  const bits = [];
  if (article.author) bits.push(escHtml(article.author));
  const site = domainOf(article.url);
  if (site) bits.push(escHtml(site));
  if (article.published_date) bits.push(escHtml(article.published_date));
  if (minutes) bits.push(`${minutes} min read`);
  const link = `<a href="${escHtml(article.url)}" target="_blank" rel="noopener">Original ↗</a>`;
  $('article-meta').innerHTML =
    bits.join('<span class="meta-dot">·</span>') + (bits.length ? '<span class="meta-dot">·</span>' : '') + link;

  renderReadButtons(article.read);
}

function renderReadButtons(isRead) {
  const label = isRead ? '↩ Mark as unread' : '✓ Mark as read';
  $('dock-read').textContent = label;
  $('dock-read').classList.toggle('is-read', isRead);

  // At the end of the article the obvious next move is the next article, so
  // the button there says so — and does both in one tap.
  const hasNext = Boolean(neighbour(state.currentId, 1));
  const end = $('end-read-btn');
  end.textContent = !isRead && hasNext ? '✓ Mark as read & next' : label;
  end.classList.toggle('is-read', isRead);
  const bar = $('bar-read-btn');
  bar.textContent = isRead ? '↩' : '✓';
  bar.setAttribute('aria-label', isRead ? 'Mark as unread' : 'Mark as read');
  bar.style.color = isRead ? 'var(--green)' : '';
}

function renderBody(article, markdown) {
  $('article-body').innerHTML = injectInlineVideos(toHtml(markdown));
  // The Videos section is a fallback for articles saved before the extractor
  // began leaving inline markers in the body.
  if (!$('article-body').querySelector('.video-wrapper')) renderVideos(article);
  else show($('videos-section'), false);
}

/** Markdown to HTML — with a plain-text fallback if the CDN cannot be reached,
    so an article still reads on a phone that is offline. */
function toHtml(markdown) {
  if (window.marked) return marked.parse(markdown);
  return markdown
    .split(/\n{2,}/)
    .map((block) => `<p>${escHtml(block)}</p>`)
    .join('');
}

function renderNext(id) {
  const next = neighbour(id, 1);
  const slot = $('next-slot');
  if (!next) {
    slot.innerHTML = '';
    return;
  }
  slot.innerHTML = `<button type="button" class="next-card" data-id="${escHtml(next.id)}">
    <span>
      <span class="next-label">Next in ${escHtml(state.filter)}</span>
      <span class="next-title">${escHtml(next.title)}</span>
    </span>
    <span class="next-arrow" aria-hidden="true">›</span>
  </button>`;
}

// --- actions -------------------------------------------------------------

/** Flip the read flag on the article being read, and return what came back. */
async function applyToggle() {
  const id = state.currentId;
  if (!id) return null;
  let updated;
  try {
    updated = await api.toggleRead(id);
  } catch (e) {
    toast(e.message);
    return null;
  }
  const i = state.articles.findIndex((a) => a.id === id);
  if (i !== -1) state.articles[i] = updated;
  renderReadButtons(updated.read);
  emit('articles-changed');
  emit('stats-stale');
  return updated;
}

export async function toggleRead() {
  const id = state.currentId;
  const updated = await applyToggle();
  if (!updated) return;
  const next = neighbour(id, 1);
  if (updated.read && next) {
    toast('Marked as read.', { label: 'Next ›', run: () => openArticle(next.id) });
  } else {
    toast(updated.read ? 'Marked as read.' : 'Marked as unread.');
  }
}

/** The end-of-article button: mark it read, then move on if there is more. */
export async function readAndNext() {
  const next = neighbour(state.currentId, 1);
  const wasUnread = !(articleById(state.currentId) || {}).read;
  const updated = await applyToggle();
  if (!updated) return;
  if (wasUnread && next) {
    openArticle(next.id);
    toast('Marked as read.');
  } else {
    toast(updated.read ? 'Marked as read.' : 'Marked as unread.');
  }
}

export async function refreshCurrent() {
  const id = state.currentId;
  if (!id) return;
  closeMenus();
  toast('Re-fetching…');
  try {
    const updated = await api.refresh(id);
    const i = state.articles.findIndex((a) => a.id === id);
    if (i !== -1) state.articles[i] = updated;
    const markdown = (await api.content(id)).content;
    renderBody(updated, markdown);
    renderHeader(updated, readingMinutes(markdown));
    emit('articles-changed');
    toast('Re-fetched.');
  } catch (e) {
    toast(e.message);
  }
}

export async function deleteCurrent() {
  const id = state.currentId;
  if (!id) return;
  closeMenus();
  const article = articleById(id);
  const title = article ? article.title : 'this article';
  if (!confirm(`Delete "${title}"?\n\nThis cannot be undone.`)) return;
  try {
    await api.remove(id);
  } catch (e) {
    toast(e.message);
    return;
  }
  closeReader();
  emit('reload-articles');
  emit('stats-stale');
  toast('Deleted.');
}

export function openOriginal() {
  closeMenus();
  const article = articleById(state.currentId);
  if (article) window.open(article.url, '_blank', 'noopener');
}

export async function copyLink() {
  closeMenus();
  const article = articleById(state.currentId);
  if (!article) return;
  try {
    await navigator.clipboard.writeText(article.url);
    toast('Link copied.');
  } catch (e) {
    toast('Could not copy the link.');
  }
}

/** Move to the article before or after this one in the list being shown. */
export function step(direction) {
  if (!state.currentId) return;
  const target = neighbour(state.currentId, direction);
  if (target) openArticle(target.id);
}

// --- chrome, progress, position -----------------------------------------

function setChrome(visible) {
  document.body.classList.toggle('chrome-hidden', !visible);
}

let lastY = 0;
let drift = 0;
let ticking = false;

/** Forget the last article's scrolling, so the next one starts at the top. */
function resetScrollTracking() {
  lastY = 0;
  drift = 0;
  userScrolled = false;
  document.body.classList.remove('title-passed');
}

function onScroll() {
  if (ticking) return;
  ticking = true;
  requestAnimationFrame(() => {
    ticking = false;
    const scroller = $('reader-scroll');
    const max = scroller.scrollHeight - scroller.clientHeight;
    const y = scroller.scrollTop;
    $('reader-progress-fill').style.width = `${max > 0 ? Math.min(100, (y / max) * 100) : 0}%`;

    document.body.classList.toggle('title-passed', y > 90);

    const dy = y - lastY;
    lastY = y;
    // Near either end of the article the chrome is always welcome; in between,
    // a deliberate scroll one way or the other moves it.
    if (y < 72 || y > max - 140) {
      drift = 0;
      setChrome(true);
    } else if (dy > 0) {
      drift = Math.max(0, drift) + dy;
      if (drift > 28 && !document.querySelector('.menu.open')) setChrome(false);
    } else if (dy < 0) {
      drift = Math.min(0, drift) + dy;
      if (drift < -18) setChrome(true);
    }
    savePosition();
  });
}

function positions() {
  try {
    return JSON.parse(localStorage.getItem(POSITIONS_KEY) || '{}');
  } catch (e) {
    return {};
  }
}

function savePosition() {
  const id = state.currentId;
  if (!id) return;
  const scroller = $('reader-scroll');
  const max = scroller.scrollHeight - scroller.clientHeight;
  if (max <= 0) return;
  const all = positions();
  all[id] = Math.min(1, scroller.scrollTop / max);
  // Keep the store small: the oldest keys go once it grows past the cap.
  const keys = Object.keys(all);
  if (keys.length > POSITIONS_KEPT) {
    for (const key of keys.slice(0, keys.length - POSITIONS_KEPT)) delete all[key];
  }
  try {
    localStorage.setItem(POSITIONS_KEY, JSON.stringify(all));
  } catch (e) {
    /* nothing to do — losing a scroll position is not worth an error */
  }
}

function restorePosition(id) {
  const ratio = positions()[id];
  if (!ratio || ratio < 0.02 || ratio > 0.97) return;
  const scroller = $('reader-scroll');
  const jump = () => {
    const max = scroller.scrollHeight - scroller.clientHeight;
    if (max > 0) scroller.scrollTop = max * ratio;
  };
  requestAnimationFrame(jump);
  // Images settle after the markdown lands and change the page height, so aim
  // once more — unless the reader has already taken over the scrolling.
  setTimeout(() => {
    if (!userScrolled) jump();
  }, 450);
}

// --- videos --------------------------------------------------------------

function injectInlineVideos(html) {
  // Replace the <p>video-embed:URL</p> markers the extractor leaves behind.
  // marked renders them two ways: bare, or with the URL auto-linked.
  return html.replace(
    /<p>\s*video-embed:\s*(?:<a[^>]*href="([^"]+)"[^>]*>[^<]*<\/a>|([^<\s]+))\s*<\/p>/gi,
    (_, hrefUrl, textUrl) => {
      const embed = toEmbedUrl(hrefUrl || textUrl);
      return embed ? videoHtml(embed) : '';
    }
  );
}

function renderVideos(article) {
  const urls = [...(article.video_urls || [])];
  // A saved YouTube link is itself the video, so it plays first.
  const self = toEmbedUrl(article.url);
  if (self && !urls.some((u) => toEmbedUrl(u) === self)) urls.unshift(article.url);
  const embeds = urls.map(toEmbedUrl).filter(Boolean);
  if (!embeds.length) {
    show($('videos-section'), false);
    return;
  }
  $('videos-container').innerHTML = embeds.map(videoHtml).join('');
  show($('videos-section'), true);
}

function videoHtml(embedUrl) {
  return `<div class="video-wrapper"><iframe src="${escHtml(embedUrl)}" allowfullscreen
    allow="autoplay; encrypted-media" referrerpolicy="no-referrer"></iframe></div>`;
}

function toEmbedUrl(url) {
  let m = url.match(/[?&]v=([A-Za-z0-9_-]+)/);
  if (m) return `https://www.youtube.com/embed/${m[1]}`;
  m = url.match(/youtu\.be\/([A-Za-z0-9_-]+)/);
  if (m) return `https://www.youtube.com/embed/${m[1]}`;
  m = url.match(/vimeo\.com\/(\d+)/);
  if (m) return `https://player.vimeo.com/video/${m[1]}`;
  if (url.includes('/embed/') || url.includes('player.vimeo')) return url;
  return null;
}

// --- wiring --------------------------------------------------------------

export function initReader() {
  const scroller = $('reader-scroll');
  scroller.addEventListener('scroll', onScroll, { passive: true });
  ['wheel', 'touchstart', 'keydown'].forEach((evt) =>
    scroller.addEventListener(evt, () => { userScrolled = true; }, { passive: true })
  );
  $('next-slot').addEventListener('click', (e) => {
    const card = e.target.closest('.next-card');
    if (card) openArticle(card.dataset.id);
  });

  // The list can change under the reader — a filter switch, a sync, a delete.
  on('current-article-gone', closeReader);
  on('filter-changed', () => {
    if (state.currentId) renderNext(state.currentId);
  });
  on('articles-changed', () => {
    if (state.currentId) renderNext(state.currentId);
  });

  window.addEventListener('popstate', (e) => {
    const id = e.state && e.state.articleId;
    if (id && articleById(id)) openArticle(id, true);
    else closeReader();
  });
  window.addEventListener('pagehide', savePosition);
  history.replaceState({ articleId: null }, '');
}
