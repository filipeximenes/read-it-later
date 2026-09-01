/* Moving the library in and out: sync with the hosted copy, export a backup
   zip, and the guarded import that replaces everything. */

import { api } from './api.js';
import { loadArticles, loadStats } from './library.js';
import { closeReader } from './reader.js';
import { $, closeMenus, closeOverlay, escHtml, openOverlay, toast } from './ui.js';

let syncing = false;
let pendingFile = null;

// --- sync ----------------------------------------------------------------

export async function loadSyncConfig() {
  // The hosted copy serves this same page and has nowhere to sync to, so the
  // button only appears where a remote is configured.
  try {
    const config = await api.syncConfig();
    $('sync-btn').hidden = !config.enabled;
  } catch (e) {
    /* sync is optional; the button stays hidden */
  }
}

export async function runSync() {
  if (syncing) return;
  syncing = true;
  const button = $('sync-btn');
  button.classList.add('busy');
  button.disabled = true;
  try {
    const report = await api.runSync();
    await loadArticles();
    loadStats();
    toast(`Synced — sent ${report.sent}, received ${report.received}.`);
  } catch (e) {
    toast(e.message);
  } finally {
    syncing = false;
    button.classList.remove('busy');
    button.disabled = false;
  }
}

// --- export --------------------------------------------------------------

export function exportData() {
  closeMenus();
  // Let the browser download it, so a large archive never buffers in memory.
  window.location.href = '/api/export';
}

// --- import --------------------------------------------------------------

export function pickImportFile() {
  closeMenus();
  const input = $('import-file');
  input.value = '';
  input.click();
}

export function closeImport() {
  closeOverlay('import-overlay');
  pendingFile = null;
}

function setImportBody(body, footer) {
  $('import-content').innerHTML = body;
  $('import-footer').innerHTML = footer;
}

function showImportError(message) {
  setImportBody(
    `<div class="warn-box">${escHtml(message)}</div>`,
    '<button class="btn" data-action="close-import">Close</button>'
  );
}

async function onFileChosen(event) {
  const file = event.target.files && event.target.files[0];
  if (!file) return;
  pendingFile = file;
  openOverlay('import-overlay');
  setImportBody('<div class="loading-line"><span class="spinner"></span>Reading the archive…</div>', '');
  try {
    renderPreview(file, await api.importArchive(file, true));
  } catch (e) {
    showImportError(e.message);
  }
}

function summaryRow(label, value, cls) {
  return (
    `<div class="summary-row"><span class="k">${escHtml(label)}</span>` +
    `<span class="v ${cls || ''}">${escHtml(String(value))}</span></div>`
  );
}

function renderPreview(file, s) {
  let rows = summaryRow('Articles in the archive', s.article_count) +
    summaryRow('Markdown files', s.file_count);
  if (s.missing_files) rows += summaryRow('Indexed articles with no file', s.missing_files, 'warn');
  if (s.orphan_files) rows += summaryRow('Files not listed in the index', s.orphan_files, 'warn');
  if (s.skipped_entries) rows += summaryRow('Unrelated entries (ignored)', s.skipped_entries, 'warn');

  setImportBody(
    `<div class="summary-title">Archive</div>
     <div class="file-name">${escHtml(file.name)}</div>
     ${rows}
     <div class="summary-title">Your library right now</div>
     ${summaryRow('Articles that will be deleted', s.replaced_articles, s.replaced_articles ? 'danger' : '')}
     <div class="warn-box">
       This replaces your whole library. Everything not in the archive is removed —
       it is not merged with what you have now.
     </div>
     <div class="note-box">
       A snapshot of your current data is saved next to your data folder first, so you
       can undo this with <code>ril import backup &lt;snapshot&gt;</code>.
     </div>`,
    '<button class="btn" data-action="close-import">Cancel</button>' +
      '<button class="btn btn-danger" id="import-confirm" data-action="confirm-import">Replace all data</button>'
  );
}

export async function confirmImport() {
  if (!pendingFile) return;
  const file = pendingFile;
  $('import-footer')
    .querySelectorAll('button')
    .forEach((b) => {
      b.disabled = true;
    });
  const confirm = $('import-confirm');
  if (confirm) confirm.innerHTML = '<span class="spinner"></span>Restoring…';

  let s;
  try {
    s = await api.importArchive(file, false);
  } catch (e) {
    showImportError(e.message);
    return;
  }

  const snapshot = s.snapshot
    ? `<div class="note-box">Your previous data was saved as
       <span class="file-name">${escHtml(s.snapshot)}</span>, next to your data folder.</div>`
    : '';
  setImportBody(
    `<div class="summary-title">Done</div>
     ${summaryRow('Articles restored', s.article_count)}
     ${summaryRow('Articles replaced', s.replaced_articles)}
     ${snapshot}`,
    '<button class="btn" data-action="close-import">Close</button>'
  );

  pendingFile = null;
  closeReader();
  await loadArticles();
  loadStats();
}

export function initTransfer() {
  $('import-file').addEventListener('change', onFileChosen);
}
