/**
 * Code and diff highlighting — adapter over the real highlight.js library.
 *
 * Vendored plain scripts, loaded by index.html before the app bundle:
 * - highlight.lib.js   — highlight.js v11.12.0 "common" UMD build (36 languages)
 * - highlight.extra.js — 17 more language grammars, registered at load
 *
 * Design constraints, in order of importance:
 *
 * 1. **Safety.** Input is raw fence text and the output is always fully
 *    HTML-escaped markup: highlight.js escapes its own output, and the diff
 *    and plain-text paths escape here. A live tag can never reach the DOM.
 * 2. **Streaming.** The renderer runs on every frame over a growing string,
 *    so fences are routinely unterminated. highlight.js is regex-based and
 *    tolerates partial tokens; a size cap keeps huge fences cheap.
 * 3. **Offline.** The library and grammars are vendored; nothing is fetched.
 * 4. **Graceful degradation.** A missing library (blocked script, tests
 *    without the vendor files) or an unknown language yields escaped plain
 *    text — never an exception, never raw markup.
 *
 * Module dependencies: none at module scope. The library is read through
 * `globalThis.hljs` at call time, so this module loads before it if it must.
 */

/** Fences longer than this stay plain text: highlighting them on every
 * frame would stall the renderer for no visual benefit. */
const MAX_HL_CHARS = 120_000;

const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#39;");

const DIFF_LANGS = new Set(["diff", "patch", "udiff"]);
const DIFF_HEAD = /^(?:diff --git |index |--- |\+\+\+ )/;

/** The vendored library, or ``null`` when it did not load. */
function getLibrary() {
  const hljs = globalThis.hljs;
  return hljs && typeof hljs.highlight === "function" && typeof hljs.getLanguage === "function"
    ? hljs
    : null;
}

/**
 * Results by ``(language, source)``.
 *
 * A fence that is already written is highlighted once: a later paint of the
 * same answer, a reloaded chat, or a tool payload carrying the same code reuses
 * the markup instead of running the grammar again. Plain LRU over entries and
 * total characters, so a long chat cannot grow it without bound.
 */
const CACHE_ENTRIES = 128;
const CACHE_CHARS = 4_000_000;
const cache = new Map();
let cachedChars = 0;

const cacheKey = (language, raw) => `${language}\u0000${raw}`;

function cacheGet(key) {
  const hit = cache.get(key);
  if (hit === undefined) return undefined;
  // Touch: the entry used now is the last to be evicted.
  cache.delete(key);
  cache.set(key, hit);
  return hit;
}

function cacheSet(key, html) {
  if (cache.has(key)) return;
  cache.set(key, html);
  cachedChars += key.length;
  while (cache.size > CACHE_ENTRIES || cachedChars > CACHE_CHARS) {
    const oldest = cache.keys().next().value;
    if (oldest === undefined) break;
    cache.delete(oldest);
    cachedChars -= oldest.length;
  }
}

/** True when text is a unified diff even without a language hint. */
export function looksLikeDiff(text) {
  const t = String(text ?? "");
  return t.includes("diff --git ") ||
    (t.includes("+++ ") && t.includes("--- ")) ||
    t.includes("@@ -");
}

function wrap(cls, line) {
  return line ? `<span class="${cls}">${line}</span>` : line;
}

/** One escaped diff line to an escaped span (meta/added/removed/hunk).
 * `---`/`+++` are file headers only as a pair: inside a hunk a removed line
 * such as `-- sql comment` also starts with `---` once the marker is added. */
function diffLine(line, next = "", prev = "") {
  if (line.startsWith("@@")) return wrap("dl--hunk", line);
  if (line.startsWith("--- ")) return wrap(next.startsWith("+++ ") ? "dl--meta" : "dl--del", line);
  if (line.startsWith("+++ ")) return wrap(prev.startsWith("--- ") ? "dl--meta" : "dl--add", line);
  if (DIFF_HEAD.test(line) || line.startsWith("index ")) return wrap("dl--meta", line);
  if (line.startsWith("+")) return wrap("dl--add", line);
  if (line.startsWith("-")) return wrap("dl--del", line);
  if (line.startsWith("&gt;")) return wrap("dl--context", line);
  if (line.startsWith("&lt;")) return wrap("dl--del", line);
  return wrap("dl--context", line);
}


/** A diff as markup. Every line is a block span and lines join with no
 * separators: the newline text nodes between blocks used to render inside
 * `pre` as an extra empty line, double-spacing whole diffs. Context lines
 * get a span of their own so the joining works uniformly. */
function highlightDiff(raw) {
  const lines = escapeHtml(raw).split("\n");
  return lines.map((line, i) => diffLine(line, lines[i + 1], lines[i - 1])).join("");
}

/** Added and removed line counts of a unified diff (headers excluded). */
export function diffStats(text) {
  const lines = String(text ?? "").split("\n");
  let add = 0;
  let del = 0;
  lines.forEach((line, i) => {
    if (line.startsWith("--- ") && (lines[i + 1] ?? "").startsWith("+++ ")) return;
    if (line.startsWith("+++ ") && (lines[i - 1] ?? "").startsWith("--- ")) return;
    if (line.startsWith("+")) add++;
    else if (line.startsWith("-")) del++;
  });
  return { add, del };
}

/**
 * Highlight raw fence text.
 *
 * @param {string} rawCode raw, unescaped fence body
 * @param {string} lang fence info string ("" when absent)
 * @returns {string} fully escaped HTML markup, safe for innerHTML
 */
export function highlight(rawCode, lang) {
  const raw = String(rawCode ?? "");
  if (!raw) return raw;

  const language = String(lang ?? "").trim().toLowerCase();
  // Without the library every result is plain escaped text; caching that would
  // poison the cache once the library arrives (a blocked script, then loaded).
  if (!getLibrary()) return compute(raw, language);
  const key = cacheKey(language, raw);
  const cached = cacheGet(key);
  if (cached !== undefined) return cached;
  const html = compute(raw, language);
  cacheSet(key, html);
  return html;
}

/** The real work behind ``highlight``, once per ``(language, source)``. */
function compute(raw, language) {
  if (DIFF_LANGS.has(language) || looksLikeDiff(raw)) return highlightDiff(raw);
  if (!language) return escapeHtml(raw);

  const hljs = getLibrary();
  if (!hljs || raw.length > MAX_HL_CHARS || !hljs.getLanguage(language)) return escapeHtml(raw);
  try {
    return hljs.highlight(raw, { language, ignoreIllegals: true }).value;
  } catch {
    return escapeHtml(raw);
  }
}
