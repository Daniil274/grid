/**
 * The two requests the map page makes (web_chat/grid.html).
 *
 * The project map is revalidated: the first answer's ETag goes back as
 * If-None-Match on every refresh, and a 304 means "nothing changed" - the page
 * keeps what it already shows instead of re-reading a whole map. The projects
 * list, for the picker, is the chat's own endpoint via the shared REST client.
 *
 * On a server with accounts a 401 means the session is gone: the page goes to
 * sign-in, the same way the shared client does (js/net/api.js).
 */

import { api } from "../net/api.js";

const SIGN_IN = "/login";

/**
 * GET /api/projects/{id}/map.
 *
 * @param {string} projectId
 * @param {{etag?: string, signal?: AbortSignal}} [options]
 * @returns {Promise<{notModified: boolean, etag: string, map?: object}>}
 */
export async function fetchProjectMap(projectId, { etag = "", signal } = {}) {
  const response = await fetch(`/api/projects/${encodeURIComponent(String(projectId))}/map`, {
    cache: "no-cache",
    headers: etag ? { "If-None-Match": etag } : {},
    signal,
  });
  if (response.status === 401) {
    location.assign(SIGN_IN);
    throw new Error("Signed out");
  }
  if (response.status === 304) return { notModified: true, etag };
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(
      payload?.detail || payload?.reason || `GET /api/projects/${projectId}/map failed (${response.status})`,
    );
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return { notModified: false, etag: response.headers?.get?.("ETag") ?? "", map: payload };
}

/** GET /api/projects, for the picker. */
export async function fetchProjects() {
  const payload = await api.projects();
  return Array.isArray(payload?.projects) ? payload.projects : [];
}
