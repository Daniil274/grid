/** Content-driven tool previews. All tools, including unknown MCP tools, share
 * this fallback. Untrusted HTML never reaches the DOM except through the safe
 * markdown/code renderers. Remote images never trigger an automatic request. */
import { h } from "../lib/dom.js";
import { renderMarkdown } from "../lib/markdown.js";
import { decorateMath } from "../lib/math.js";
import { highlight, diffStats } from "../lib/highlight.js";
import { languageFor, toolFamily } from "../lib/languages.js";
import { scopedWorkspaceUrl } from "../lib/workspace-links.js";
import { copyText } from "./toast.js";
import { MEMORY_LAYER_NAMES, MEMORY_LEVEL_NAMES } from "../lib/memory-labels.js";
import { beadJson, beadText } from "./bead-cards.js";

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

function partView(part, memory, role, tool) {
  if (!part || typeof part !== "object") return h("pre.payload__body", { text: "Invalid content block" });
  const text = textOf(part.data).slice(0, MAX_TEXT);
  let body;
  switch (part.kind) {
    // A tracker payload reads as task cards; every other JSON keeps the tree.
    case "json":
      body = (memory && memoryJson(part.data, memory, role))
        || beadJson(part.data, tool, role)
        || jsonView(part.data);
      break;
    case "markdown":
      body = h("div.payload__body.payload__body--prose", { html: renderMarkdown(text) });
      decorateMath(body);
      break;
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
    case "text": body = beadText(text, tool, role) || h("pre.payload__body", {}, h("code", { text: text || "Empty text" })); break;
    default: body = (!part.kind && beadText(text, tool, role))
      || h("pre.payload__body", {}, h("code", { text: text || "Empty text" }));
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

/* ------------------------------------------------------------------------- *
 * Memory operations. The four memory_* tools answer with records, a saved
 * record or a deletion flag; their arguments are a search, a listing, a
 * record to keep or an id to drop. Cards read far better than that JSON and
 * say whose memory it is (this chat, the project, the user). Any shape this
 * code does not recognise stays on the generic JSON view, and the card's
 * Source/Copy buttons keep showing the untouched payload.
 * ------------------------------------------------------------------------- */

const MEMORY_TOOLS = new Set(["memory_recall", "memory_overview", "memory_remember", "memory_forget"]);
const MEMORY_PREVIEW = 280; // characters of a record body shown unasked
const MEMORY_FIRST = 5; // cards shown before "Show N more"
const MEMORY_MAX_CARDS = 50; // hard bound on rendered cards

const isObject = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value);
const clean = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
const shorten = (value, limit) => (clean(value).length > limit ? `${clean(value).slice(0, limit).trimEnd()}…` : clean(value));

/** The memory tool behind a (possibly `server.`-prefixed) step name, or "". */
function memoryTool(tool) {
  const key = String(tool ?? "").toLowerCase().split(".").pop();
  return MEMORY_TOOLS.has(key) ? key : "";
}

const memoryTag = (text, kind) => (text ? h("span.payload__memTag", { text, dataset: { kind } }) : null);

/** A rank shown as itself: RRF-style scores are small, so two decimals would
 * turn 0.016 into 0.02. It is a relevance rank, never a probability. */
function scoreText(score) {
  if (!Number.isFinite(score)) return null;
  if (score === 0) return "0";
  if (Math.abs(score) >= 1) return score.toFixed(2);
  return Math.abs(score) >= 0.001 ? String(Number(score.toPrecision(3))) : score.toExponential(2);
}

/** A record as a card: layer/category/owner/staleness, title, source, tags. */
function memoryCard(record, { score, level } = {}) {
  const content = typeof record.content === "string" ? record.content : "";
  const rawTitle = typeof record.title === "string" ? clean(record.title) : "";
  const layer = String(record.layer ?? "");
  // Only an explicit level names the owner: `scope` is a storage label, not
  // the level a record lives at, so it must never stand in for a missing one.
  const owner = String(level ?? record.level ?? "");
  const source = [record.file_path, record.source_ref]
    .find((value) => typeof value === "string" && value.trim()) ?? "";
  const tags = Array.isArray(record.tags) ? record.tags.filter((tag) => typeof tag === "string" && tag).slice(0, 8) : [];
  const stamp = typeof (record.updated_at || record.created_at) === "string" ? clean(record.updated_at || record.created_at) : "";
  const firstLine = content.split("\n")[0];
  // The heading is one line of text at most, whether it is the record's title
  // or its opening words: a title is untrusted data too and may be endless.
  const lead = rawTitle || firstLine;
  const heading = shorten(lead, 80) || "Untitled record";
  // Whatever the shortened heading dropped, and the rest of the text, still
  // gets a readable body.
  const body = [
    lead.length > 80 ? lead.slice(80).trimStart() : "",
    rawTitle ? content : content.slice(firstLine.length).replace(/^\n+/, ""),
  ].filter(Boolean).join("\n");
  const rank = scoreText(score);
  const facts = [
    typeof record.id === "string" && record.id ? ["id", record.id] : null,
    source ? [record.file_path ? "file" : "source", source] : null,
    tags.length ? ["tags", tags.map((tag) => `#${tag}`).join(" ")] : null,
    stamp ? ["updated", stamp] : null,
  ].filter(Boolean);
  return h("article.payload__memCard",
    { dataset: { layer, stale: String(record.stale_status ?? "") } },
    h("div.payload__memHead", {},
      memoryTag(layer, "layer"),
      memoryTag(clean(record.category), "category"),
      memoryTag(MEMORY_LEVEL_NAMES[owner] ?? owner, "level"),
      memoryTag(clean(record.stale_status).toUpperCase(), "stale"),
      rank ? h("span.payload__memScore", { text: rank, title: "Relevance score, not a probability" }) : null),
    h("h5.payload__memTitle", { text: heading }),
    body ? h("p.payload__memBody", { text: shorten(body, MEMORY_PREVIEW) }) : null,
    body.length > MEMORY_PREVIEW
      ? h("details.payload__memDetails", {}, h("summary", { text: "Full text" }),
        h("p.payload__memBody", { text: body.slice(0, MAX_TEXT) })) : null,
    facts.length
      ? h("details.payload__memDetails", {}, h("summary", { text: "Details" }),
        h("div.payload__memFacts", {}, facts.map(([key, value]) => h("div.payload__memFact", {},
          h("span.payload__memKey", { text: `${key}: ` }), h("span.payload__memValue", { text: String(value) }))))) : null);
}

/** A one-line answer: a confirmation, an empty state or a refusal. */
const memoryNote = (tone, ...children) => h("p.payload__memNote", { dataset: { tone } }, ...children);

/** Cards bounded up front; the rest appears on demand and never all at once.
 * Callers pass every entry, so the count that cannot be rendered stays honest
 * instead of disappearing behind a slice taken before this bound. */
function memoryCards(entries, makeCard) {
  const capped = entries.slice(0, MEMORY_MAX_CARDS);
  const shown = capped.slice(0, MEMORY_FIRST).map(makeCard);
  const rest = capped.slice(MEMORY_FIRST).map(makeCard);
  const box = h("div.payload__memList", {}, shown);
  if (rest.length) {
    const more = h("button.payload__memMore", { type: "button", text: `Show ${rest.length} more` });
    more.addEventListener("click", () => { more.remove(); box.append(...rest); });
    box.append(more);
  }
  if (entries.length > MEMORY_MAX_CARDS) {
    box.append(h("small.payload__notice", { text: `${entries.length - MEMORY_MAX_CARDS} more records · see source` }));
  }
  return box;
}

function memoryError(message) {
  return h("div.payload__mem", {}, memoryNote("error", h("strong", { text: "Memory error" }), h("span", { text: ` — ${message}` })));
}

function memoryRecall(data) {
  if (isObject(data) && typeof data.query === "string") {
    const query = clean(data.query);
    // An empty query is a listing, not a search: say what the tool does.
    return h("div.payload__mem", {}, memoryNote("info",
      query ? h("span", { text: "Search memory for " }) : h("span", { text: "List memories" }),
      query ? h("q", { text: query }) : null,
      typeof data.limit === "number" ? h("span", { text: ` · up to ${data.limit} results` }) : null));
  }
  if (!Array.isArray(data)) return null;
  if (!data.length) return h("div.payload__mem", {}, memoryNote("empty", h("span", { text: "No relevant memories found." })));
  const entries = data.map((hit) => {
    const record = isObject(hit) && isObject(hit.record) ? hit.record : hit;
    if (!isObject(record) || typeof record.content !== "string") return null;
    return { record, score: isObject(hit) && typeof hit.score === "number" ? hit.score : undefined,
      level: isObject(hit) && typeof hit.level === "string" ? hit.level : undefined };
  }).filter(Boolean);
  if (!entries.length) return null;
  return h("div.payload__mem", {},
    memoryNote("info", h("span", { text: entries.length === 1 ? "1 related memory" : `${entries.length} related memories` })),
    memoryCards(entries, ({ record, score, level }) => memoryCard(record, { score, level })));
}

function memoryOverview(data) {
  if (isObject(data) && typeof data.limit === "number" && !Array.isArray(data.records)) {
    return h("div.payload__mem", {}, memoryNote("info", h("span", { text: `Up to ${data.limit} records` })));
  }
  if (!isObject(data) || !Array.isArray(data.records)) return null;
  const records = data.records.filter((record) => isObject(record) && typeof record.content === "string");
  // Entries that are not records say nothing about the memory: only an empty
  // answer does, so an unreadable one falls back to the JSON view instead of
  // claiming there is nothing stored.
  if (!records.length) {
    return data.records.length ? null : h("div.payload__mem", {}, memoryNote("empty", h("span", { text: "Memory is empty." })));
  }
  // The tool is capped by its own limit: say so instead of implying a total.
  const bounded = typeof data.limit === "number" && records.length >= data.limit;
  return h("div.payload__mem", {},
    memoryNote("info", h("span", { text: records.length === 1 ? "1 record" : `${records.length} records` }),
      bounded ? h("span", { text: ` · limit ${data.limit} reached, older records may exist` }) : null),
    memoryCards(records, (record) => memoryCard(record)));
}

function memoryRemember(data, role) {
  if (!isObject(data) || typeof data.content !== "string") return null;
  const stored = typeof data.id === "string" && Boolean(data.id);
  const owner = MEMORY_LEVEL_NAMES[String(data.level ?? "")] ?? clean(data.level);
  const card = memoryCard(data);
  if (!stored) {
    // Only an argument can be "about to be saved"; a result without an id is
    // not a confirmation of anything, so it keeps the generic JSON view.
    if (role === "result") return null;
    return h("div.payload__mem", {}, memoryNote("info", h("span", { text: "Record to save" }), owner ? h("span", { text: ` · ${owner}` }) : null), card);
  }
  const layer = MEMORY_LAYER_NAMES[String(data.layer ?? "")] ?? clean(data.layer);
  return h("div.payload__mem", {},
    memoryNote("ok", h("strong", { text: "Saved" }), h("span", { text: ` · ${[layer, clean(data.category), owner].filter(Boolean).join(" · ")}` })),
    card);
}

function memoryForget(data, role) {
  if (!isObject(data)) return null;
  if (typeof data.forgotten === "boolean") {
    return data.forgotten
      ? h("div.payload__mem", {}, memoryNote("ok", h("strong", { text: "Deleted" }), h("span", { text: " · the record is gone from this memory" })))
      : h("div.payload__mem", {}, memoryNote("empty", h("span", { text: "Nothing deleted — no record with that id in this memory." })));
  }
  const id = typeof data.record_id === "string" ? clean(data.record_id) : "";
  // A result without the deletion flag is unknown, not an intent to delete.
  if (role === "result") return null;
  return id ? h("div.payload__mem", {}, memoryNote("info", h("span", { text: "Delete record " }), h("code", { text: id }))) : null;
}

/** A memory-specific view for a typed JSON part, or null to keep the generic one. */
function memoryJson(data, tool, role) {
  if (isObject(data) && typeof data.error === "string" && data.error) return memoryError(clean(data.error));
  try {
    switch (tool) {
      case "memory_recall": return memoryRecall(data);
      case "memory_overview": return memoryOverview(data);
      case "memory_remember": return memoryRemember(data, role);
      case "memory_forget": return memoryForget(data, role);
      default: return null;
    }
  } catch { return null; }
}

/** Which half of a call a payload is: arguments or what came back. Labels are
 * the only hint older call sites give, so an explicit role wins but a known
 * label still separates "Record to save" from "Saved". */
function payloadRole(label, explicit) {
  if (explicit) return explicit === "input" || explicit === "result" ? explicit : "";
  if (label === "Input" || label === "Task") return "input";
  if (label === "Result" || label === "Report" || label === "Error") return "result";
  return "";
}

export function renderPayload(label, typed, legacy, { markdown = false, state = {}, tool = "", role = "" } = {}) {
  const value = typed?.version === 1 && Array.isArray(typed.parts) ? typed : legacyPayload(legacy, markdown);
  if (!value) return null;
  const parts = value.parts.slice(0, MAX_PARTS);
  const given = typeof value.raw_text === "string" ? value.raw_text : null;
  // What the Source view would show, made only when the reader asks for it: a
  // payload without ``raw_text`` is serialised part by part, which is the work
  // the typed view already does - a folded card must not pay for both.
  let rawText = null;
  const source = () => (rawText ??= (given ?? parts.map((p) => textOf(p?.data)).join("\n\n")).slice(0, MAX_TEXT));
  // How long the source is, without building it: only string parts (text,
  // stderr) can hold a long body, and a structured part is bounded by the
  // typed view, which says so itself.
  const size = given != null
    ? given.length
    : parts.reduce((total, p) => total + (p && p.kind !== "image" && typeof p.data === "string" ? p.data.length : 0), 0);
  const truncated = value.truncated || size > MAX_TEXT || value.parts.length > MAX_PARTS;
  const memory = memoryTool(tool);
  const half = payloadRole(label, role);
  const display = h("div.payload__typed", {}, parts.length ? parts.map((part) => partView(part, memory, half, tool)) : h("span.payload__empty", { text: "No content returned" }));
  // The raw view is the payload's own bytes shown a second time. A server-sent
  // ``raw_text`` is already a string, so its node costs nothing to make; a
  // payload without one is serialised part by part - the work the typed view
  // just did - so that view waits for the reader to press Source.
  let rawBlock = given == null ? null : h("pre.payload__body.payload__raw", {}, h("code", { text: given.slice(0, MAX_TEXT) || "Empty text" }));
  const rawView = () => (rawBlock ??= h("pre.payload__body.payload__raw", {}, h("code", { text: source() || "Empty text" })));
  const toggle = h("button.payload__source", { type: "button" });
  const notice = truncated
    ? h("small.payload__notice", { text: `Truncated${Number.isFinite(value.original_size) ? ` · original ${value.original_size} bytes` : ""}` })
    : null;
  const card = h("div.payload", { dataset: { family: toolFamily(tool), error: value.is_error ? "true" : "false" } },
    h("div.payload__bar", {},
    h("span.payload__label", { text: label }),
    h("span.payload__format", { text: [...new Set(parts.map((p) => p?.kind || "text"))].join(" + ") || "empty" }),
    toggle, h("button.payload__source", { type: "button", text: "Copy", title: truncated ? "Copy available source preview" : "Copy source",
      on: { click: () => copyText(source(), `${label} copied`) } })), display, rawBlock, notice);
  const apply = () => {
    // Appending a node already in the card moves it: the source lands between
    // the typed view and the notice, and the notice keeps the last place.
    if (state.source && !rawBlock) card.append(rawView(), ...(notice ? [notice] : []));
    display.hidden = Boolean(state.source);
    if (rawBlock) rawBlock.hidden = !state.source;
    toggle.textContent = state.source ? "Preview" : "Source";
    toggle.setAttribute("aria-pressed", String(Boolean(state.source)));
  };
  toggle.addEventListener("click", () => { state.source = !state.source; apply(); });
  apply();
  return card;
}
