/* The statistics sheet: the tiles, the reading-progress bar, and the
   saved/read activity chart with its own paging. */

import { api } from './api.js';
import { loadStats } from './library.js';
import { $, closeOverlay, escHtml, openOverlay } from './ui.js';

const PERIODS = [
  { key: '8w', label: '8 wk', unit: 'week', count: 8 },
  { key: '26w', label: '26 wk', unit: 'week', count: 26 },
  { key: '12m', label: '12 mo', unit: 'month', count: 12 },
];

let period = '8w';
let offset = 0;
let buckets = [];
let pending = null;

export async function openStats() {
  openOverlay('stats-overlay');
  const content = $('stats-content');
  if (!content.innerHTML) {
    content.innerHTML = '<div class="loading-line"><span class="spinner"></span>Loading…</div>';
  }
  renderStats(await loadStats());
}

export function closeStats() {
  closeOverlay('stats-overlay');
}

function renderStats(s) {
  const content = $('stats-content');
  if (!s) {
    content.innerHTML = '<div class="loading-line">Statistics are unavailable.</div>';
    return;
  }
  if (!s.total) {
    content.innerHTML = '<div class="loading-line">No articles saved yet.</div>';
    return;
  }

  const days = (v) => (v == null ? '—' : `${v}d`);
  const tiles = [
    { label: 'Saved this week', value: s.saved_this_week, cls: 'accent' },
    { label: 'Read this week', value: s.read_this_week, cls: 'green' },
    { label: 'Median time to read', value: days(s.median_days_to_read), cls: '' },
    { label: 'Saved this month', value: s.saved_this_month, cls: 'accent' },
    { label: 'Read this month', value: s.read_this_month, cls: 'green' },
    { label: 'Oldest unread', value: days(s.oldest_unread_days), cls: '' },
  ]
    .map(
      (t) =>
        `<div class="stat-tile"><div class="stat-value ${t.cls}">${t.value}</div>` +
        `<div class="stat-label">${t.label}</div></div>`
    )
    .join('');

  const progress = `<div class="stat-section">
    <div class="stat-section-title">Reading progress</div>
    <div class="progress-bar"><div class="progress-fill" style="width:${s.read_pct}%"></div></div>
    <div class="progress-label">${s.read_pct}% read · ${s.read} of ${s.total} articles</div>
  </div>`;

  period = '8w';
  offset = 0;
  const periodButtons = PERIODS.map(
    (p) =>
      `<button class="tab" data-period="${p.key}" aria-pressed="${p.key === period}">${p.label}</button>`
  ).join('');
  const chart = `<div class="stat-section">
    <div class="stat-section-head">
      <div class="stat-section-title">Activity</div>
      <div id="period-toggle" style="display:flex;gap:6px">${periodButtons}</div>
    </div>
    <div class="chart-legend">
      <span><span class="swatch" style="background:var(--accent)"></span>Saved</span>
      <span><span class="swatch" style="background:var(--green)"></span>Read</span>
    </div>
    <div class="chart-nav">
      <button class="nav-arrow" id="nav-older" data-shift="1" aria-label="Older">‹</button>
      <span class="chart-range" id="chart-range">—</span>
      <button class="nav-arrow" id="nav-newer" data-shift="-1" aria-label="Newer" disabled>›</button>
    </div>
    <div id="activity-slot"><div class="loading-line"><span class="spinner"></span></div></div>
  </div>`;

  const authors = s.top_authors.length
    ? `<div class="stat-section"><div class="stat-section-title">Top authors</div>${s.top_authors
        .map(
          ([name, count]) =>
            `<div class="rank-row"><span class="rank-name">${escHtml(name)}</span>` +
            `<span class="rank-count">${count}</span></div>`
        )
        .join('')}</div>`
    : '';

  const tags = s.top_tags.length
    ? `<div class="stat-section"><div class="stat-section-title">Top tags</div>
        <div class="tag-cloud">${s.top_tags
          .map(
            ([tag, count]) =>
              `<span class="tag-chip">${escHtml(tag)}<span class="tag-chip-count">${count}</span></span>`
          )
          .join('')}</div></div>`
    : '';

  content.innerHTML = `<div class="stat-tiles">${tiles}</div>${progress}${chart}${authors}${tags}`;
  loadActivity();
}

async function loadActivity() {
  const p = PERIODS.find((x) => x.key === period);
  const slot = $('activity-slot');
  if (slot) slot.innerHTML = '<div class="loading-line"><span class="spinner"></span></div>';

  const token = `${period}:${offset}`;
  pending = token;
  let payload = { buckets: [], has_older: false, has_newer: offset > 0 };
  try {
    payload = await api.activity(p.unit, p.count, offset);
  } catch (e) {
    /* an empty chart says enough */
  }
  if (pending !== token) return;

  buckets = payload.buckets || [];
  if (slot) slot.innerHTML = chartHtml(buckets);
  const range = $('chart-range');
  if (range) {
    range.textContent = buckets.length
      ? `${buckets[0].label} – ${buckets[buckets.length - 1].label}`
      : '—';
  }
  if ($('nav-older')) $('nav-older').disabled = !payload.has_older;
  if ($('nav-newer')) $('nav-newer').disabled = !payload.has_newer;
}

function chartHtml(data) {
  if (!data.length) return '';
  const W = 560;
  const H = 200;
  const padL = 26;
  const padR = 8;
  const padT = 10;
  const padB = 30;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const n = data.length;
  const maxVal = Math.max(1, ...data.flatMap((d) => [d.saved, d.read]));
  const groupW = plotW / n;
  const barW = Math.max(4, Math.min(16, groupW / 2 - 4));
  const y = (v) => padT + plotH - (v / maxVal) * plotH;

  const ticks = [0, Math.round(maxVal / 2), maxVal].filter((v, i, a) => a.indexOf(v) === i);
  const grid = ticks
    .map(
      (v) =>
        `<line class="grid-line" x1="${padL}" y1="${y(v)}" x2="${W - padR}" y2="${y(v)}"></line>` +
        `<text class="axis-label" x="${padL - 5}" y="${y(v) + 3}" text-anchor="end">${v}</text>`
    )
    .join('');

  const labelEvery = n > 14 ? Math.ceil(n / 10) : 1;
  const bars = data
    .map((d, i) => {
      const cx = padL + groupW * i + groupW / 2;
      const saved = `<rect class="bar-saved" x="${cx - barW - 1}" y="${y(d.saved)}" width="${barW}"
        height="${padT + plotH - y(d.saved)}" rx="2"></rect>`;
      const read = `<rect class="bar-read" x="${cx + 1}" y="${y(d.read)}" width="${barW}"
        height="${padT + plotH - y(d.read)}" rx="2"></rect>`;
      const label =
        i % labelEvery === 0 || i === n - 1
          ? `<text class="axis-label" x="${cx}" y="${H - 10}" text-anchor="middle">${escHtml(d.label)}</text>`
          : '';
      return saved + read + label;
    })
    .join('');

  // Hover zones come last so they sit above the bars and catch the pointer.
  const zones = data
    .map(
      (d, i) =>
        `<rect class="hover-zone" data-bucket="${i}" x="${padL + groupW * i}" y="${padT}"
          width="${groupW}" height="${plotH}" rx="3"></rect>`
    )
    .join('');

  return `<div id="chart-wrap">
    <svg id="activity-chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet"
         role="img" aria-label="Articles saved and read over time">${grid}${bars}${zones}</svg>
    <div id="chart-tooltip"></div>
  </div>`;
}

function showTip(event, index) {
  const bucket = buckets[index];
  const tip = $('chart-tooltip');
  const wrap = $('chart-wrap');
  if (!bucket || !tip || !wrap) return;
  tip.innerHTML = `<div class="tip-date">${escHtml(bucket.label)}</div>
    <div class="tip-row"><span class="tip-dot saved"></span>Saved <b>${bucket.saved}</b></div>
    <div class="tip-row"><span class="tip-dot read"></span>Read <b>${bucket.read}</b></div>`;
  tip.style.display = 'block';
  const box = wrap.getBoundingClientRect();
  const x = event.clientX - box.left;
  const yPos = event.clientY - box.top;
  tip.style.left = `${Math.max(0, Math.min(x + 14, wrap.clientWidth - tip.offsetWidth - 2))}px`;
  tip.style.top = `${Math.max(0, yPos - tip.offsetHeight - 10)}px`;
}

function hideTip() {
  const tip = $('chart-tooltip');
  if (tip) tip.style.display = 'none';
}

export function initStats() {
  const content = $('stats-content');
  content.addEventListener('click', (e) => {
    const periodBtn = e.target.closest('[data-period]');
    if (periodBtn) {
      period = periodBtn.dataset.period;
      offset = 0;
      content
        .querySelectorAll('[data-period]')
        .forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.period === period)));
      loadActivity();
      return;
    }
    const shift = e.target.closest('[data-shift]');
    if (shift && !shift.disabled) {
      const p = PERIODS.find((x) => x.key === period);
      const next = offset + Number(shift.dataset.shift) * p.count;
      if (next < 0) return;
      offset = next;
      loadActivity();
    }
  });
  content.addEventListener('mousemove', (e) => {
    const zone = e.target.closest('.hover-zone');
    if (zone) showTip(e, Number(zone.dataset.bucket));
    else hideTip();
  });
  content.addEventListener('mouseleave', hideTip);
}
