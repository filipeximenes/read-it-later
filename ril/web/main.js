/* Wiring: one delegated click handler for every `data-action` in the page,
   the keyboard shortcuts, and the start-up sequence. */

import { closeAddUrl, initLibrary, loadArticles, loadStats, openAddUrl, revealActiveItem, submitAddUrl } from './library.js';
import { closeTypeSheet, initPrefs, openTypeSheet, resetPrefs, stepSize } from './prefs.js';
import {
  copyLink,
  deleteCurrent,
  goBack,
  initReader,
  openOriginal,
  readAndNext,
  refreshCurrent,
  step,
  toggleRead,
} from './reader.js';
import { closeStats, initStats, openStats } from './stats.js';
import {
  closeImport,
  confirmImport,
  exportData,
  initTransfer,
  loadSyncConfig,
  pickImportFile,
  runSync,
} from './transfer.js';
import { state } from './store.js';
import { $, closeMenus, closeOverlay, toggleMenu, topOverlay } from './ui.js';

const ACTIONS = {
  sync: runSync,
  'add-url': openAddUrl,
  'add-url-submit': submitAddUrl,
  'add-url-cancel': closeAddUrl,
  'library-menu': () => toggleMenu('library-menu', 'library-menu-btn'),
  stats: openStats,
  export: exportData,
  import: pickImportFile,

  back: goBack,
  typography: openTypeSheet,
  'toggle-read': toggleRead,
  'read-and-next': readAndNext,
  'reader-menu': () => toggleMenu('reader-menu', 'reader-menu-btn'),
  'dock-menu': () => toggleMenu('dock-menu', 'dock-menu-btn'),
  'open-original': openOriginal,
  'copy-link': copyLink,
  refresh: refreshCurrent,
  delete: deleteCurrent,

  'close-type': closeTypeSheet,
  'close-stats': closeStats,
  'close-import': closeImport,
  'confirm-import': confirmImport,
  'size-up': () => stepSize(1),
  'size-down': () => stepSize(-1),
  'reset-prefs': resetPrefs,
};

document.addEventListener('click', (e) => {
  const trigger = e.target.closest('[data-action]');
  if (trigger) {
    const run = ACTIONS[trigger.dataset.action];
    if (run) {
      e.preventDefault();
      run();
      return;
    }
  }
  // A tap on the backdrop closes the sheet; a tap anywhere outside an open
  // menu closes that.
  const backdrop = e.target.dataset && e.target.dataset.closeOverlay;
  if (backdrop) closeOverlay(backdrop);
  if (!e.target.closest('.menu-wrap')) closeMenus();
});

document.addEventListener('keydown', (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);

  if (e.key === 'Escape') {
    const overlay = topOverlay();
    if (overlay) closeOverlay(overlay);
    else if (document.querySelector('.menu.open')) closeMenus();
    else if (document.body.classList.contains('reading')) goBack();
    return;
  }
  if (typing) return;

  const reading = Boolean(state.currentId);
  switch (e.key) {
    case '/':
      e.preventDefault();
      $('search-input').focus();
      break;
    case 'a':
      e.preventDefault();
      openAddUrl();
      break;
    case 'j':
      if (reading) { step(1); revealActiveItem(); }
      break;
    case 'k':
      if (reading) { step(-1); revealActiveItem(); }
      break;
    case 'm':
      if (reading) toggleRead();
      break;
    case '+':
    case '=':
      stepSize(1);
      break;
    case '-':
      stepSize(-1);
      break;
    default:
      break;
  }
});

initPrefs();
initLibrary();
initReader();
initStats();
initTransfer();
loadArticles();
loadStats();
loadSyncConfig();
