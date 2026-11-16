/**
 * The project's space, read one folder and one file at a time
 * (web_chat/grid.html).
 *
 * The map already knows a project's top-level entries; the inspector needs
 * more - the entries of a folder, the first lines of a file - and the server
 * already answers both:
 *
 *   GET /api/workspace/tree?context=<chat>&path=project[/dir]
 *   GET /api/workspace/text?context=<chat>&path=project/<file>
 *
 * `map.files_url` is the way in (`/files?context=<chat>&path=project`): its
 * `context` names the chat whose space is shown and its `path` names the root
 * as that chat names it. Those are the only two things taken from it; every
 * request is built from them here, so a folder listing made on the map can
 * never name another space or another chat.
 *
 * Nothing is read ahead: `createExplorer` fetches a path when it is asked for
 * it, keeps the answer, and answers a second question about the same path from
 * that answer. A refresh of the map does not touch any of it.
 */

/** The two existing endpoints: this module invents no others. */
export const TREE_ENDPOINT = "/api/workspace/tree";
export const TEXT_ENDPOINT = "/api/workspace/text";

const text = (value) => String(value ?? "");

/**
 * `map.files_url` as `{base, context, root}`, or null when the server named
 * no way into the space. A URL without a root names nothing to browse.
 */
export function parseFilesUrl(filesUrl) {
  const raw = text(filesUrl).trim();
  if (!raw) return null;
  // The server sends a path on this origin ("/files?context=…&path=project");
  // anything that is not one is not a way into the space.
  const cut = raw.indexOf("?");
  const path = cut < 0 ? raw : raw.slice(0, cut);
  if (!path.startsWith("/") || path.startsWith("//")) return null;
  const params = new URLSearchParams(cut < 0 ? "" : raw.slice(cut + 1));
  const root = text(params.get("path")).replace(/\/+$/g, "");
  if (!root || root.startsWith("/")) return null;
  return { base: path, context: text(params.get("context")), root };
}

/**
 * A path as this module is willing to send it: relative, inside the space,
 * no backslash, no empty or `..` segment, no absolute path and no scheme.
 * Returns "" for anything else - a refusal, not a guess.
 */
export function safePath(value) {
  const raw = text(value);
  if (!raw || raw.includes("\\") || raw.includes("\0")) return "";
  if (raw.startsWith("/") || raw.startsWith("~")) return "";
  if (/^[A-Za-z]:/.test(raw) || raw.includes("://")) return "";
  const parts = [];
  for (const part of raw.split("/")) {
    if (part === "" || part === ".") continue;
    if (part === "..") return "";
    parts.push(part);
  }
  return parts.join("/");
}

/** `base` + `name`, either of which may be empty. */
export function joinPath(base, name) {
  const head = safePath(base);
  const tail = safePath(name);
  if (!head) return tail;
  if (!tail) return head;
  return `${head}/${tail}`;
}

/** The folder *path* is in: "project/a/b.md" -> "project/a". */
export function parentOf(path) {
  const safe = safePath(path);
  const cut = safe.lastIndexOf("/");
  return cut < 0 ? "" : safe.slice(0, cut);
}

/** The last name of *path*. */
export function nameOf(path) {
  const safe = safePath(path);
  return safe.slice(safe.lastIndexOf("/") + 1);
}

const query = (filesUrl, path) => {
  const parsed = parseFilesUrl(filesUrl);
  if (!parsed) return "";
  const params = new URLSearchParams();
  if (parsed.context) params.set("context", parsed.context);
  params.set("path", path || parsed.root);
  return params.toString();
};

/** The listing URL of one folder. */
export function treeUrl(filesUrl, path = "") {
  const search = query(filesUrl, path);
  return search ? `${TREE_ENDPOINT}?${search}` : "";
}

/** The text URL of one file. */
export function textUrl(filesUrl, path = "") {
  const search = query(filesUrl, path);
  return search ? `${TEXT_ENDPOINT}?${search}` : "";
}

/**
 * The files page's URL for an entry named by its path from the space's root:
 * a folder by `path`, a file by `file` - the way web_chat/js/grid/render.js
 * already links a top-level entry.
 */
export function filesHref(filesUrl, { path = "", kind = "file" } = {}) {
  const parsed = parseFilesUrl(filesUrl);
  const safe = safePath(path);
  if (!parsed || !safe) return "";
  const params = new URLSearchParams();
  if (parsed.context) params.set("context", parsed.context);
  params.set(kind === "dir" ? "path" : "file", safe);
  return `${parsed.base}?${params.toString()}`;
}

/**
 * The entries of a listing, kept only when they name a path inside the space;
 * an entry the server names in a shape this page does not know becomes
 * "other" rather than a guess.
 */
export function listingEntries(listing, root = "") {
  const entries = Array.isArray(listing?.entries) ? listing.entries : [];
  const inside = (path) => {
    const safe = safePath(path);
    if (!safe) return "";
    if (!root) return safe;
    return safe === root || safe.startsWith(`${root}/`) ? safe : "";
  };
  return entries
    .map((entry) => {
      const path = inside(entry?.path);
      if (!path) return null;
      const kind = ["dir", "file", "link"].includes(text(entry?.kind)) ? text(entry.kind) : "other";
      return {
        name: text(entry?.name) || nameOf(path),
        path,
        kind,
        size: Number.isFinite(entry?.size) ? entry.size : null,
        mtime: entry?.mtime ?? null,
      };
    })
    .filter(Boolean);
}

/** The lines of a text, without the empty tail a final newline leaves. */
export function splitLines(value, limit = 0) {
  const lines = text(value).split("\n");
  if (lines.length && lines[lines.length - 1] === "") lines.pop();
  return limit > 0 ? lines.slice(0, limit) : lines;
}

/** "1.2 KB", "12 B": a size a reader can judge, never a bare number. */
export function formatBytes(bytes) {
  if (bytes == null || !Number.isFinite(Number(bytes))) return "";
  const value = Number(bytes);
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = value / 1024;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(size < 10 ? 1 : 0)} ${units[unit]}`;
}

/** The honest sentence for a status the read answered with. */
export function fileErrorText(status) {
  const code = Number(status) || 0;
  if (code === 401) return "Sign in to read this project's files.";
  if (code === 403) return "This server may not read that here.";
  if (code === 404) return "It is not there: it may have been moved or deleted.";
  if (code === 413) return "It is too large to preview here.";
  if (code === 415) return "It cannot be shown as text.";
  if (code === 0) return "The files did not answer.";
  return `The file could not be read (status ${code}).`;
}

/** `fetch` as this module uses it: the status and the JSON, never a throw. */
async function defaultFetchJson(url) {
  const response = await fetch(url, { cache: "no-store", credentials: "same-origin" });
  const payload = await response.json().catch(() => null);
  return { ok: response.ok, status: response.status, json: payload };
}

/**
 * A reader of the project's space.
 *
 * `fetchJson(url)` answers `{ok, status, json}`; tests pass their own. Every
 * path is normalized against the space's root before it is sent, so an
 * absolute path or a `..` never leaves this module. Answers are kept: the same
 * folder is asked for once, and asking again after a refresh costs nothing.
 */
export function createExplorer({ filesUrl = "", fetchJson = null } = {}) {
  const parsed = parseFilesUrl(filesUrl);
  const root = parsed?.root ?? "";
  const ask = typeof fetchJson === "function" ? fetchJson : defaultFetchJson;
  const cache = new Map();
  const calls = [];

  /** *path* as a path inside this space, or "" when it is not one. */
  function normalize(path) {
    const safe = safePath(path);
    if (!safe) return "";
    if (!root) return safe;
    return safe === root || safe.startsWith(`${root}/`) ? safe : "";
  }

  async function read(kind, path) {
    const url = kind === "tree" ? treeUrl(filesUrl, path) : textUrl(filesUrl, path);
    if (!url) return { ok: false, status: 0, kind, path, error: fileErrorText(0) };
    const key = `${kind}:${path}`;
    if (cache.has(key)) return cache.get(key);
    calls.push({ kind, path, url });
    const promise = (async () => {
      try {
        const answer = await ask(url);
        const status = Number(answer?.status ?? (answer?.ok ? 200 : 0));
        const ok = answer?.ok ?? (status >= 200 && status < 300);
        if (!ok) return { ok: false, status, kind, path, error: fileErrorText(status) };
        return { ok: true, status, kind, path, data: answer?.json ?? null };
      } catch {
        return { ok: false, status: 0, kind, path, error: fileErrorText(0) };
      }
    })();
    cache.set(key, promise);
    // A refusal is not an answer to keep: the next try asks again.
    promise.then((result) => {
      if (!result.ok) cache.delete(key);
    });
    return promise;
  }

  return {
    filesUrl: text(filesUrl),
    root,
    context: parsed?.context ?? "",
    base: parsed?.base ?? "",
    supported: Boolean(parsed),
    calls,
    normalize,
    /** The entries of folder *path* (default: the space's root). */
    async tree(path = "") {
      const safe = normalize(path || root);
      if (!safe) return { ok: false, status: 0, kind: "tree", path: text(path), error: fileErrorText(0) };
      return read("tree", safe);
    },
    /** One file's text. */
    async text(path = "") {
      const safe = normalize(path);
      if (!safe) return { ok: false, status: 0, kind: "text", path: text(path), error: fileErrorText(0) };
      return read("text", safe);
    },
    /** Forget one path, or the whole cache. */
    forget(path = "") {
      const safe = path ? normalize(path) : "";
      for (const key of [...cache.keys()]) {
        if (!path || key.endsWith(`:${safe}`)) cache.delete(key);
      }
    },
    href(entry) {
      return filesHref(filesUrl, entry);
    },
  };
}
