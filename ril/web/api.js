/* Every call to the server lives here, so the screens deal in data and never
   in URLs or status codes. A failed call throws `ApiError`, whose message is
   already fit to show to a person. */

export class ApiError extends Error {}

async function request(url, options = {}) {
  let res;
  try {
    res = await fetch(url, options);
  } catch (e) {
    throw new ApiError('Could not reach the server. Is `ril serve` still running?');
  }
  const body = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) {
    const detail = body && typeof body.detail === 'string' ? body.detail : null;
    throw new ApiError(detail || `Request failed (HTTP ${res.status}).`);
  }
  return body;
}

const json = (method, body) => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

export const api = {
  articles(filter, query) {
    const q = query ? `&q=${encodeURIComponent(query)}` : '';
    return request(`/api/articles?filter=${encodeURIComponent(filter)}${q}`);
  },
  content(id) {
    return request(`/api/articles/${id}/content`);
  },
  add(url) {
    return request('/api/articles', json('POST', { url }));
  },
  remove(id) {
    return request(`/api/articles/${id}`, { method: 'DELETE' });
  },
  refresh(id) {
    return request(`/api/articles/${id}/refresh`, { method: 'POST' });
  },
  toggleRead(id) {
    return request(`/api/articles/${id}/toggle-read`, { method: 'POST' });
  },
  stats() {
    return request('/api/stats');
  },
  activity(unit, count, offset) {
    return request(`/api/activity?unit=${unit}&count=${count}&offset=${offset}`);
  },
  syncConfig() {
    return request('/api/sync/config');
  },
  runSync() {
    return request('/api/sync/run', { method: 'POST', headers: { 'X-RIL-Sync': 'run' } });
  },
  importArchive(file, dryRun) {
    return request(`/api/import?dry_run=${dryRun}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/zip', 'X-RIL-Confirm': 'replace-all' },
      body: file,
    });
  },
};
