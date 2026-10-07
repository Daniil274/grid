/** Content-driven tool previews. All tools, including unknown MCP tools, share
 * this fallback. Untrusted HTML never reaches the DOM except through the safe
 * markdown/code renderers. Remote images never trigger an automatic request. */
import { h } from "../lib/dom.js";
import { renderMarkdown } from "../lib/markdown.js";
import { highlight, diffStats } from "../lib/highlight.js";
import { languageFor, toolFamily } from "../lib/languages.js";
import { scopedWorkspaceUrl } from "../lib/workspace-links.js";
import { copyText } from "./toast.js";

const MAX_TEXT = 32000;
const MAX_PARTS = 40;
const textOf = (value) => typeof value === "string" ? value : JSON.stringify(value, null, 2) ?? "null";
const scalar = (value) => JSON.stringify(value) ?? "null";

function safeLink(value) {
  if (typeof value !== "string" || /[\s\\\u0000-\u001f]/.test(value)) return null;
  if (/^https?:\/\//i.test(value)) {
    try {
      const url = new URL(value);
      return url.username || url.password ? null : url.href;
    } catch { return null; }
  }
  // Only known workspace artifacts qualify as local links.
  if (!value.startsWith("/api/workspace/files/") || /%2e|%2f|%5c/i.test(value)) return null;
  const path = value.split("?")[0];
  if (path.includes("#") || path.split("/").some((p) => p === "." || p === "..")) return null;
  return scopedWorkspaceUrl(value);
}

function imageSource(value) {
  if (typeof value !== "string") return null;
  if (value.length <= 512000 && /^data:image\/(png|jpeg|gif|webp);base64,[a-z0-9+/=\r\n]+$/i.test(value)) return value;
  const link = safeLink(value);
  if (!link?.startsWith("/api/workspace/files/")) return null;
  const path = link.split("?")[0];
  if (!/\.(png|jpe?g|gif|webp)$/i.test(path)) return null;
  return scopedWorkspaceUrl(`${path}?inline=1`);
}

function resourceLink(uri, label) {
  const link = safeLink(uri);
  return link
    ? h("a.payload__resource", { href: link, target: "_blank", rel: "noreferrer noopener", text: label || uri })
    : h("span.payload__resource", { text: label || uri || "Resource without a URL" });
}

/** Native JSON values: strings remain strings, not a second round of JSON. */
function jsonView(value) {
  let count = 0;
  const walk = (item, depth = 0) => {
    count++;
    if (item === null || typeof item !== "object") return h("span.payload__value", { dataset: { type: item === null ? "null" : typeof item }, text: scalar(item).slice(0, MAX_TEXT) });
    const entries = Object.entries(item);
    const array = Array.isArray(item);
    if (!entries.length) return h("span.payload__value", { text: array ? "[]" : "{}" });
    if (depth >= 4 || count >= 500) return h("span.payload__summary", { text: `${entries.length} ${array ? "items" : "fields"} · see source` });
    const children = entries.slice(0, 100).map(([key, child]) => h("div.payload__field", {},
      h("span.payload__key", { text: array ? `[${key}]` : `${JSON.stringify(key)}: ` }), walk(child, depth + 1)));
    if (entries.length > 100) children.push(h("small.payload__notice", { text: `${entries.length - 100} more · see source` }));
    if (depth === 0) return h("div.payload__fields", {}, children);
    return h("details.payload__branch", {}, h("summary", { text: `${entries.length} ${array ? "items" : "fields"}` }), h("div.payload__fields", {}, children));
  };
  // A homogeneous array of flat records is naturally tabular.
  if (Array.isArray(value) && value.length && value[0] && typeof value[0] === "object" && !Array.isArray(value[0])) {
    const keys = Object.keys(value[0]);
    if (keys.length > 0 && keys.length <= 12 && value.every((row) => row && typeof row === "object" && !Array.isArray(row)
      && Object.keys(row).length === keys.length && keys.every((key) => Object.hasOwn(row, key) && (row[key] === null || typeof row[key] !== "object")))) {
      return h("div.payload__tableWrap", {}, h("table.payload__table", {},
        h("thead", {}, h("tr", {}, keys.map((key) => h("th", { scope: "col", text: key })))),
        h("tbody", {}, value.slice(0, 100).map((row) => h("tr", {}, keys.map((key) => h("td", { text: scalar(row[key]).slice(0, 2000) }))))),
      ), value.length > 100 ? h("small.payload__notice", { text: `${value.length - 100} more rows · see source` }) : null);
    }
  }
  return walk(value);
}

function partView(part) {
  if (!part || typeof part !== "object") return h("pre.payload__body", { text: "Invalid content block" });
  const text = textOf(part.data).slice(0, MAX_TEXT);
  let body;
  switch (part.kind) {
    case "json": body = jsonView(part.data); break;
    case "markdown": body = h("div.payload__body.payload__body--prose", { html: renderMarkdown(text) }); break;
    case "diff": body = h("pre.payload__body.payload__code.payload__code--diff", { dataset: { language: "diff" } },
      h("code", { html: highlight(text, "diff") })); break;
    case "code": {
      // The file name is a better language signal than anything in the text:
      // it labels new payloads and repairs chats stored before languages were.
      const language = languageFor(part);
      body = h("pre.payload__body.payload__code", { dataset: { language: language || "text" } },
        h("code", { html: highlight(text, language) }));
      break;
    }
    case "image": {
      const src = imageSource(part.data);
      body = src ? h("img.payload__image", { src, alt: part.name || "Tool image", loading: "lazy", referrerpolicy: "no-referrer" })
        : resourceLink(part.data, safeLink(part.data) ? "Open image (external)" : "Image preview unavailable");
      break;
    }
    case "resource": body = resourceLink(part.uri, part.name || part.uri); break;
    default: body = h("pre.payload__body", {}, h("code", { text: text || "Empty text" }));
  }
  const stats = part.kind === "diff" ? diffStats(text) : null;
  return h("div.payload__part", { dataset: { kind: part.kind || "text", tone: partTone(part) } },
    h("div.payload__partHead", {}, h("span", { text: part.name || part.kind || "text" }),
      stats && (stats.add || stats.del) ? h("span.payload__stats", {},
        h("span.payload__stat.payload__stat--add", { text: `+${stats.add}` }),
        h("span.payload__stat.payload__stat--del", { text: `\u2212${stats.del}` })) : null,
      // A diff's `language` is the patched file's suffix - not what the text
      // is - so the label stays "diff".
      h("span.payload__type", { text: part.kind === "diff" ? "diff" : (part.kind === "code" && languageFor(part)) || part.language || part.media_type || "" })), body);
}

/** Request halves of a replacement and a command's error stream, at a glance. */
function partTone(part) {
  if (part.kind === "diff") return "diff";
  if (part.name === "old_text") return "del";
  if (part.name === "new_text") return "add";
  if (part.name === "stderr") return "error";
  if (part.name === "command") return "shell";
  return "";
}

function legacyPayload(text, markdown) {
  if (text == null || text === "") return null;
  const raw = textOf(text);
  let kind = markdown ? "markdown" : "text";
  let data = raw;
  if (!markdown && raw.length <= MAX_TEXT) {
    try { data = JSON.parse(raw); kind = "json"; } catch { /* Keep unknown content verbatim. */ }
  }
  return { version: 1, parts: [{ kind, data }], raw_text: raw, truncated: raw.length > MAX_TEXT };
}

export function renderPayload(label, typed, legacy, { markdown = false, state = {}, tool = "" } = {}) {
  const value = typed?.version === 1 && Array.isArray(typed.parts) ? typed : legacyPayload(legacy, markdown);
  if (!value) return null;
  const parts = value.parts.slice(0, MAX_PARTS);
  const raw = typeof value.raw_text === "string" ? value.raw_text : parts.map((p) => textOf(p?.data)).join("\n\n");
  const source = raw.slice(0, MAX_TEXT);
  const display = h("div.payload__typed", {}, parts.length ? parts.map(partView) : h("span.payload__empty", { text: "No content returned" }));
  const rawBlock = h("pre.payload__body.payload__raw", {}, h("code", { text: source || "Empty text" }));
  const toggle = h("button.payload__source", { type: "button" });
  const apply = () => {
    display.hidden = Boolean(state.source);
    rawBlock.hidden = !state.source;
    toggle.textContent = state.source ? "Preview" : "Source";
    toggle.setAttribute("aria-pressed", String(Boolean(state.source)));
  };
  toggle.addEventListener("click", () => { state.source = !state.source; apply(); });
  apply();
  const truncated = value.truncated || raw.length > MAX_TEXT || value.parts.length > MAX_PARTS
    || parts.some((p) => p && p.kind !== "image" && textOf(p.data).length > MAX_TEXT);
  return h("div.payload", { dataset: { family: toolFamily(tool), error: value.is_error ? "true" : "false" } },
    h("div.payload__bar", {},
    h("span.payload__label", { text: label }),
    h("span.payload__format", { text: [...new Set(parts.map((p) => p?.kind || "text"))].join(" + ") || "empty" }),
    toggle, h("button.payload__source", { type: "button", text: "Copy", title: truncated ? "Copy available source preview" : "Copy source",
      on: { click: () => copyText(source, `${label} copied`) } })), display, rawBlock,
    truncated ? h("small.payload__notice", { text: `Truncated${Number.isFinite(value.original_size) ? ` · original ${value.original_size} bytes` : ""}` }) : null);
}
