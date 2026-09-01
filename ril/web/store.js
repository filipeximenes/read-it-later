/* The little state the screens share, and a two-line event bus so they can
   tell each other about a change without importing each other. */

export const state = {
  articles: [],
  filter: 'unread',
  currentId: null,
  searchError: null,
  stats: null,
};

const listeners = new Map();

export function on(event, fn) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(fn);
}

export function emit(event, payload) {
  (listeners.get(event) || []).forEach((fn) => fn(payload));
}

/** Replace one article everywhere it is held, after the server returns it. */
export function replaceArticle(article) {
  const i = state.articles.findIndex((a) => a.id === article.id);
  if (i !== -1) state.articles[i] = article;
  emit('articles-changed');
}

export function articleById(id) {
  return state.articles.find((a) => a.id === id) || null;
}

/** The article after `id` in the list as it is shown — what "next" means. */
export function neighbour(id, step) {
  const i = state.articles.findIndex((a) => a.id === id);
  if (i === -1) return null;
  return state.articles[i + step] || null;
}
