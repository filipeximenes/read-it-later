/* Reading preferences: theme, text size, typeface, line spacing, line width.

   This module is the only place that knows what a preference means in CSS.
   It stores the ready-made declarations next to the choices, so the inline
   script in `index.html` can replay them before the first paint without
   knowing anything about fonts or sizes. */

import { $, closeOverlay, openOverlay } from './ui.js';

const KEY = 'ril.reader';

const DEFAULTS = { theme: 'auto', size: 18, family: 'sans', leading: 'normal', measure: 'medium' };

const SIZE_MIN = 14;
const SIZE_MAX = 26;

const FAMILIES = {
  sans: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
  serif: 'Georgia, "Iowan Old Style", "Times New Roman", serif',
};
const LEADINGS = { tight: '1.5', normal: '1.7', loose: '1.95' };
const MEASURES = { narrow: '34rem', medium: '40rem', wide: '48rem' };

let prefs = { ...DEFAULTS };

function load() {
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) || '{}');
    prefs = { ...DEFAULTS, ...saved };
    delete prefs.css;
  } catch (e) {
    prefs = { ...DEFAULTS };
  }
  prefs.size = Math.min(SIZE_MAX, Math.max(SIZE_MIN, Number(prefs.size) || DEFAULTS.size));
  if (!FAMILIES[prefs.family]) prefs.family = DEFAULTS.family;
  if (!LEADINGS[prefs.leading]) prefs.leading = DEFAULTS.leading;
  if (!MEASURES[prefs.measure]) prefs.measure = DEFAULTS.measure;
}

function cssFor(p) {
  return {
    '--reader-size': `${p.size}px`,
    '--reader-family': FAMILIES[p.family],
    '--reader-leading': LEADINGS[p.leading],
    '--reader-measure': MEASURES[p.measure],
  };
}

function save() {
  try {
    localStorage.setItem(KEY, JSON.stringify({ ...prefs, css: cssFor(prefs) }));
  } catch (e) {
    /* private browsing, or storage is full — the session still works */
  }
}

function apply() {
  const root = document.documentElement;
  if (prefs.theme === 'auto') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', prefs.theme);

  const css = cssFor(prefs);
  for (const [name, value] of Object.entries(css)) root.style.setProperty(name, value);

  // Colour the browser chrome to match, so a phone does not frame a dark page
  // in a white bar. Read it back from the cascade to stay in one source.
  const bg = getComputedStyle(root).getPropertyValue('--bg').trim();
  const meta = $('theme-color-meta');
  if (meta && bg) meta.setAttribute('content', bg);
}

function syncSheet() {
  for (const [id, value] of Object.entries({
    'pref-theme': prefs.theme,
    'pref-family': prefs.family,
    'pref-leading': prefs.leading,
    'pref-measure': prefs.measure,
  })) {
    $(id)
      .querySelectorAll('button')
      .forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.value === value)));
  }
  $('pref-size-value').textContent = `${prefs.size} px`;
  const pct = ((prefs.size - SIZE_MIN) / (SIZE_MAX - SIZE_MIN)) * 100;
  $('pref-size-track').style.width = `${pct}%`;
}

function set(key, value) {
  prefs[key] = value;
  save();
  apply();
  syncSheet();
}

export function stepSize(delta) {
  set('size', Math.min(SIZE_MAX, Math.max(SIZE_MIN, prefs.size + delta)));
}

export function resetPrefs() {
  prefs = { ...DEFAULTS };
  save();
  apply();
  syncSheet();
}

export function openTypeSheet() {
  syncSheet();
  openOverlay('type-overlay');
}

export function closeTypeSheet() {
  closeOverlay('type-overlay');
}

export function initPrefs() {
  load();
  apply();
  for (const [id, key] of Object.entries({
    'pref-theme': 'theme',
    'pref-family': 'family',
    'pref-leading': 'leading',
    'pref-measure': 'measure',
  })) {
    $(id).addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-value]');
      if (btn) set(key, btn.dataset.value);
    });
  }
  // "Auto" follows the system, so the browser chrome has to follow it too.
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (prefs.theme === 'auto') apply();
  });
  syncSheet();
}
