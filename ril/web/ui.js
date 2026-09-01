/* Small helpers shared by every screen: lookups, escaping, formatting, and
   the three bits of chrome (menus, overlays, the toast) that more than one
   module needs to open or close. */

export const $ = (id) => document.getElementById(id);

export function escHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

export function show(el, visible) {
  if (el) el.hidden = !visible;
}

/** The host of a URL, without `www.`, for the one-line item subtitle. */
export function domainOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch (e) {
    return '';
  }
}

/** "today", "3d", "5w", "Mar 2024" — short enough for a list row. */
export function shortAge(iso) {
  if (!iso) return '';
  const then = new Date(iso);
  if (isNaN(then)) return '';
  const days = Math.floor((Date.now() - then.getTime()) / 86400000);
  if (days <= 0) return 'today';
  if (days === 1) return 'yesterday';
  if (days < 7) return `${days}d`;
  if (days < 56) return `${Math.floor(days / 7)}w`;
  if (days < 365) return then.toLocaleDateString(undefined, { month: 'short' });
  return then.toLocaleDateString(undefined, { month: 'short', year: 'numeric' });
}

/** Reading time from the markdown body, at a middling 220 words a minute. */
export function readingMinutes(markdown) {
  const words = markdown.trim().split(/\s+/).filter(Boolean).length;
  return Math.max(1, Math.round(words / 220));
}

// --- menus ---------------------------------------------------------------

export function toggleMenu(menuId, buttonId) {
  const menu = $(menuId);
  const wasOpen = menu.classList.contains('open');
  closeMenus();
  if (!wasOpen) {
    menu.classList.add('open');
    $(buttonId).setAttribute('aria-expanded', 'true');
  }
}

export function closeMenus() {
  document.querySelectorAll('.menu.open').forEach((m) => m.classList.remove('open'));
  document.querySelectorAll('[aria-haspopup="true"]').forEach((b) =>
    b.setAttribute('aria-expanded', 'false')
  );
}

export function anyMenuOpen() {
  return document.querySelector('.menu.open') !== null;
}

// --- overlays ------------------------------------------------------------

export function openOverlay(id) {
  closeMenus();
  $(id).classList.add('open');
}

export function closeOverlay(id) {
  $(id).classList.remove('open');
}

export function topOverlay() {
  const open = document.querySelectorAll('.overlay.open');
  return open.length ? open[open.length - 1].id : null;
}

// --- toast ---------------------------------------------------------------

let toastTimer = null;

/** A short confirmation, with one optional action (used for Undo). */
export function toast(message, action) {
  const el = $('toast');
  el.innerHTML = `<span class="toast-text"></span>`;
  el.firstChild.textContent = message;
  if (action) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = action.label;
    btn.addEventListener('click', () => {
      hideToast();
      action.run();
    });
    el.appendChild(btn);
  }
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(hideToast, action ? 6000 : 2600);
}

export function hideToast() {
  clearTimeout(toastTimer);
  $('toast').classList.remove('show');
}
