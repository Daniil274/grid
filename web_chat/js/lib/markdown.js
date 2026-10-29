/**
 * Markdown renderer for assistant output.
 *
 * Self-contained on purpose: the chat has to render correctly offline and in
 * air-gapped containers, so it cannot depend on a CDN parser. Two properties
 * matter more than feature completeness:
 *
 * 1. **Safety.** The source is escaped once, up front; every later stage works
 *    on escaped text. No tag in model output or tool results can reach the DOM,
 *    and link targets are filtered to safe schemes.
 *    Fenced code is the one deliberate round trip: the escaped fence body is
 *    restored with ``unescapeHtml`` (the exact inverse of ``escapeHtml``) and
 *    the highlighter re-escapes its own output — still no live markup.
 * 2. **Streaming tolerance.** Half-typed emphasis and unclosed code fences are
 *    normal mid-stream; they render as themselves instead of corrupting the
 *    rest of the document.
 *
 * Formulas - TeX between \( \) or $ $, and \[ \] or $$ $$ - become `.math`
 * elements holding their escaped source; lib/math.js typesets them.
 */

import { highlight } from "./highlight.js";

const SAFE_HREF = /^(https?:\/\/|mailto:|#|\/)/i;
/** Sentinel around extracted code spans: escapeHtml never emits this entity, so it cannot collide. */
const MARK = '&#0;';
/** The same for extracted formulas. */
const MATH_MARK = '&#1;';

/** An open fence longer than this waits for its closing fence to be coloured
 * (see the fence case below): per-frame highlighting does not scale. */
const STREAM_HL_CHARS = 4000;

/**
 * Formulas within a line: \( \) inline, \[ \] and $$ $$ display, and $ $ by
 * Pandoc's rule - the opening $ is followed and the closing one preceded by a
 * non-space, and the closing one is not followed by a digit or a letter - so
 * "from $5 to $10" and "$HOME" stay text.
 */
const INLINE_MATH = /\\\((.+?)\\\)|\\\[(.+?)\\\]|\$\$(.+?)\$\$|(?<![\\$\w])\$(?=[^\s$])([^$\n]*?[^\s\\$])\$(?![\d$\w])/g;

/** A formula's element; a *block* one stands on lines of its own, outside any paragraph. */
const mathElement = (tex, display, block = false) => {
  const tag = block ? "div" : "span";
  return `<${tag} class="math${display ? " math--display" : ""}">${tex}</${tag}>`;
};

export function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/**
 * Exact inverse of ``escapeHtml``. Escaped text only ever contains the five
 * entity names that escapeHtml produces, so the round trip restores the raw
 * code exactly — a fence can never smuggle live markup through it.
 */
export function unescapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&lt;", "<")
    .replaceAll("&gt;", ">")
    .replaceAll("&quot;", '"')
    .replaceAll("&#39;", "'")
    .replaceAll("&amp;", "&");
}

function safeHref(raw) {
  const href = raw.trim();
  return SAFE_HREF.test(href) ? href : null;
}

/** Where the backend serves workspace files; a raster image there renders inline. */
const WORKSPACE_FILES = "/api/workspace/files/";

/**
 * The src of an image we are willing to render, or null.
 *
 * Only a workspace file qualifies - written as ``/api/workspace/files/<path>``
 * or as a bare path relative to the workspace root. A remote URL is refused:
 * the browser would fetch it on its own, which lets text the model has read
 * (a prompt injection) smuggle chat data out in the URL. Paths with ``..``,
 * a query or a fragment are refused too, so the src cannot leave the files
 * route. Workspace images carry ``?inline=1`` so the download route answers
 * with the file's own media type; the backend still serves only a raster
 * image inline. Anything else stays literal markdown.
 */
function safeImageSrc(raw) {
  const src = raw.trim();
  if (!src || src.startsWith("//") || /^[a-z][a-z0-9+.-]*:/i.test(src)) return null;
  const query = src.indexOf("?");
  const path = query === -1 ? src : src.slice(0, query);
  const rest = query === -1 ? "" : src.slice(query);
  // The author may have written the flag already - the system prompt suggests
  // it - and nothing else is allowed after the path.
  if (rest && rest !== "?inline=1") return null;
  if (/%2e|%5c/i.test(path) || path.includes("#") || path.includes("\\") || path.split("/").some((part) => part === ".." || part === ".")) return null;
  const full = path.startsWith(WORKSPACE_FILES) ? path : path.startsWith("/") ? null : WORKSPACE_FILES + path;
  return full && full.length > WORKSPACE_FILES.length ? `${full}?inline=1` : null;
}

/** Inline spans. Code is lifted out first so emphasis never runs inside it. */
function inline(escaped) {
  const codeSpans = [];
  let text = escaped.replace(/`([^`\n]+)`/g, (_, code) => `${MARK}${codeSpans.push(code) - 1}${MARK}`);
  // Formulas too: "_" and "*" in TeX are not emphasis.
  const formulas = [];
  text = text.replace(INLINE_MATH, (_, paren, bracket, dollars, single) => {
    const display = paren === undefined && single === undefined;
    const tex = paren ?? bracket ?? dollars ?? single;
    return `${MATH_MARK}${formulas.push(mathElement(tex, display)) - 1}${MATH_MARK}`;
  });

  text = text
    // Images come before links: the leading "!" must not be left behind by the
    // link pass, and "![alt](src)" would otherwise match it as "[alt](src)".
    .replace(/!\[([^\]\n]*)\]\(([^)\s]+)(?:\s+&quot;[^&]*&quot;)?\)/g, (match, alt, href) => {
      const src = safeImageSrc(href);
      return src ? `<img class="msg__inlineImage" src="${src}" alt="${alt}" loading="lazy">` : match;
    })
    .replace(/\[([^\]\n]*)\]\(([^)\s]+)(?:\s+&quot;[^&]*&quot;)?\)/g, (match, label, href) => {
      const url = safeHref(href);
      return url ? `<a href="${url}" target="_blank" rel="noreferrer noopener">${label || url}</a>` : match;
    })
    .replace(
      /(^|[\s(])(https?:\/\/[^\s<>()]+[^\s<>().,;:!?])/g,
      '$1<a href="$2" target="_blank" rel="noreferrer noopener">$2</a>',
    )
    .replace(/\*\*\*([^\s*][^*]*?)\*\*\*/g, "<strong><em>$1</em></strong>")
    .replace(/\*\*([^\s*][^*]*?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^\w*])\*([^\s*][^*]*?)\*(?!\w)/g, "$1<em>$2</em>")
    .replace(/(^|[^\w_])__([^\s_][^_]*?)__(?!\w)/g, "$1<strong>$2</strong>")
    .replace(/(^|[^\w_])_([^\s_][^_]*?)_(?!\w)/g, "$1<em>$2</em>")
    .replace(/~~([^~\n]+)~~/g, "<del>$1</del>");

  return text
    .replace(new RegExp(`${MATH_MARK}(\\d+)${MATH_MARK}`, "g"), (_, i) => formulas[Number(i)])
    .replace(new RegExp(`${MARK}(\\d+)${MARK}`, "g"), (_, i) => `<code>${codeSpans[Number(i)]}</code>`);
}

const BLOCK = {
  fence: /^\s*(?:```|~~~)\s*([\w+#.-]*)\s*$/,
  math: /^\s*(\\\[|\$\$)(.*)$/,
  fenceEnd: /^\s*(?:```|~~~)\s*$/,
  heading: /^(#{1,6})\s+(.*)$/,
  rule: /^\s*([-*_])(?:\s*\1){2,}\s*$/,
  quote: /^&gt;\s?(.*)$/,
  bullet: /^(\s*)[-*+]\s+(.*)$/,
  ordered: /^(\s*)\d+[.)]\s+(.*)$/,
  row: /^\s*\|(.+)\|\s*$/,
};

/** Identify a line's block type once, so the walker never re-tests patterns. */
function classify(line) {
  for (const name of ["fence", "math", "heading", "rule", "quote", "bullet", "ordered", "row"]) {
    const match = BLOCK[name].exec(line);
    if (match) return { name, match };
  }
  return { name: line.trim() ? "text" : "blank", match: null };
}

/**
 * The display formula opened on lines[start] by *open* (\[ or $$, *rest*
 * following it): its TeX and the index after it. It ends on the first line
 * that ends with the closing delimiter. Null while it is unclosed - mid-stream -
 * and for what is not one: a blank line first (TeX itself ends a formula there)
 * or a closing delimiter inside a line, as in "$$x$$ is the value".
 */
function displayMath(lines, start, open, rest) {
  const close = open === "$$" ? "$$" : "\\]";
  const parts = [];
  for (let index = start; index < lines.length; index += 1) {
    const text = index === start ? rest : lines[index];
    if (index > start && !text.trim()) return null;
    const trimmed = text.trimEnd();
    const at = trimmed.indexOf(close);
    if (at === -1) {
      parts.push(text);
      continue;
    }
    if (at !== trimmed.length - close.length) return null;
    parts.push(trimmed.slice(0, at));
    const tex = parts.join("\n").trim();
    return tex ? { tex, next: index + 1 } : null;
  }
  return null;
}

const cells = (match) => match[1].split("|").map((cell) => cell.trim());
const isDivider = (row) => row.length > 0 && row.every((cell) => /^:?-{2,}:?$/.test(cell));

/** Rendered documents, by source: a stored chat is parsed once per session. */
const DOC_ENTRIES = 24;
const DOC_CHARS = 4_000_000;
const documents = new Map();
let documentChars = 0;

function rememberDocument(source, html) {
  documents.set(source, html);
  documentChars += source.length;
  while (documents.size > DOC_ENTRIES || documentChars > DOC_CHARS) {
    const oldest = documents.keys().next().value;
    if (oldest === undefined || oldest === source) break;
    documents.delete(oldest);
    documentChars -= oldest.length;
  }
}

/**
 * @param {string} source raw markdown
 * @returns {string} HTML, safe to assign to innerHTML
 */
export function renderMarkdown(source) {
  const text = String(source ?? "").replace(/\r\n?/g, "\n");
  // A missing highlighter (a blocked script, a test without the vendor files)
  // renders plain text; that answer is never worth remembering.
  if (!globalThis.hljs) return blocks(escapeHtml(text).split("\n"));
  const cached = documents.get(text);
  if (cached !== undefined) {
    documents.delete(text);
    documents.set(text, cached);
    return cached;
  }
  const html = blocks(escapeHtml(text).split("\n"));
  rememberDocument(text, html);
  return html;
}

/** Render already-escaped lines. All recursion goes through here. */
function blocks(lines) {
  return blockList(lines).join("\n");
}

/**
 * The top-level blocks of already-escaped lines, as separate HTML strings.
 *
 * One entry per block is what makes a streaming answer cheap to repaint: a
 * closed block's HTML does not change when more text arrives, so its DOM node
 * can be kept and only the block still being written is rebuilt.
 */
function blockList(lines) {
  const out = [];
  const pending = [];
  let index = 0;

  const flush = () => {
    if (!pending.length) return;
    out.push(`<p>${inline(pending.join("\n")).replace(/\n/g, "<br>")}</p>`);
    pending.length = 0;
  };

  while (index < lines.length) {
    const line = lines[index];
    const { name, match } = classify(line);

    switch (name) {
      case "blank":
        flush();
        index += 1;
        break;

      case "fence": {
        flush();
        const body = [];
        index += 1;
        while (index < lines.length && !BLOCK.fenceEnd.test(lines[index])) body.push(lines[index++]);
        // A closing fence is absent mid-stream, which is fine. Its presence is
        // marked on the block: a diagram is drawn only once it is complete
        // (lib/diagrams.js), never while the model is still writing it.
        const closed = index < lines.length;
        index += 1;
        const language = match[1] ? ` data-language="${match[1]}"` : "";
        const done = closed ? ' data-closed="true"' : "";
        const source = body.join("\n");
        // An open fence is highlighted only while that is cheap: it is rebuilt
        // on every token, and past a few thousand characters the highlighter
        // alone costs more than the rest of a frame (measured: ~3 ms at 8k). A
        // growing block therefore stays its own escaped text and takes its
        // colours when the closing fence arrives - one highlight, then cached.
        // Round trip for the closed case: the escaped body is restored with the
        // exact inverse of escapeHtml and the highlighter re-escapes its own
        // output, so no live tag can reach the DOM either way.
        const code = !closed && source.length > STREAM_HL_CHARS ? source : highlight(unescapeHtml(source), match[1]);
        out.push(`<pre${language}${done}><code>${code}</code></pre>`);
        break;
      }

      case "math": {
        const formula = displayMath(lines, index, match[1], match[2]);
        if (!formula) {
          pending.push(line);
          index += 1;
          break;
        }
        flush();
        out.push(mathElement(formula.tex, true, true));
        index = formula.next;
        break;
      }

      case "heading":
        flush();
        out.push(`<h${match[1].length}>${inline(match[2])}</h${match[1].length}>`);
        index += 1;
        break;

      case "rule":
        flush();
        out.push("<hr>");
        index += 1;
        break;

      case "quote": {
        flush();
        const body = [];
        while (index < lines.length) {
          const quoted = BLOCK.quote.exec(lines[index]);
          if (!quoted) break;
          body.push(quoted[1]);
          index += 1;
        }
        out.push(`<blockquote>${blocks(body)}</blockquote>`);
        break;
      }

      case "bullet":
      case "ordered":
        flush();
        index = list(lines, index, out, name);
        break;

      case "row": {
        const next = index + 1 < lines.length ? classify(lines[index + 1]) : null;
        if (next?.name === "row" && isDivider(cells(next.match))) {
          flush();
          index = table(lines, index, out, cells(match));
          break;
        }
        pending.push(line);
        index += 1;
        break;
      }

      default:
        pending.push(line);
        index += 1;
    }
  }

  flush();
  return out;
}

/**
 * Streaming renderers, by owner - one slot per message being written.
 *
 * A key is whatever the caller uses to tell one growing document from another
 * (ui/message.js passes the message's element id). Two answers streaming at
 * once must not evict each other's work on every frame.
 */
const STREAM_SLOTS = 4;
const streams = new Map();

/**
 * The blocks of *source*, re-rendering only the tail that is still open.
 *
 * A blank line outside a fence ends every block before it: from there on the
 * earlier blocks can never change, so their HTML is kept and only the text
 * after the last such line is rendered again. That is what keeps a frame of a
 * 40k-character answer at the cost of its last paragraph instead of the whole
 * document, and what makes a closed fence highlight exactly once.
 *
 * @param {string} source raw markdown, growing as tokens arrive
 * @param {string} [key] which document this is; "" when there is only one
 * @returns {string[]} one HTML string per top-level block
 */
export function renderMarkdownBlocks(source, key = "") {
  const text = String(source ?? "").replace(/\r\n?/g, "\n");
  let state = streams.get(key);
  if (!state || !text.startsWith(state.source)) {
    state = { source: "", escaped: "", stable: 0, fence: false, closed: [] };
  }
  // Escaping is per character, so only the new suffix needs it.
  if (text.length > state.source.length) state.escaped += escapeHtml(text.slice(state.source.length));
  state.source = text;

  const boundary = stableBoundary(state.escaped, state.stable, state.fence);
  if (boundary.offset > state.stable) {
    const segment = state.escaped.slice(state.stable, boundary.offset);
    state.closed = state.closed.concat(blockList(segment.split("\n")));
    state.stable = boundary.offset;
  }
  state.fence = boundary.fence;

  streams.delete(key);
  streams.set(key, state);
  while (streams.size > STREAM_SLOTS) streams.delete(streams.keys().next().value);

  const tail = state.escaped.slice(state.stable);
  return tail ? state.closed.concat(blockList(tail.split("\n"))) : state.closed.slice();
}

/**
 * Where the text that can still change begins: just after the last blank line
 * that is not inside an open fence. Everything before *offset* renders the
 * same however the answer continues; *fence* is the fence state at that point.
 */
function stableBoundary(text, from, fenceOpen) {
  let offset = from;
  let boundary = from;
  let fence = fenceOpen;
  while (offset < text.length) {
    let end = text.indexOf("\n", offset);
    const last = end === -1;
    if (last) end = text.length;
    const line = text.slice(offset, end);
    if (fence) {
      if (BLOCK.fenceEnd.test(line)) fence = false;
    } else if (BLOCK.fence.test(line)) {
      fence = true;
    } else if (!line.trim() && !last) {
      // Only a line the newline has closed is final: a chunk boundary can cut
      // a whitespace-only line in half, and the rest of it may not be blank.
      boundary = end + 1;
    }
    if (last) break;
    offset = end + 1;
  }
  return { offset: boundary, fence };
}

/** Consume one list, recursing into deeper indentation. Returns the next index. */
function list(lines, start, out, kind) {
  const tag = kind === "ordered" ? "ol" : "ul";
  const baseIndent = BLOCK[kind].exec(lines[start])[1].length;
  const items = [];
  let index = start;

  while (index < lines.length) {
    const { name, match } = classify(lines[index]);
    if ((name !== "bullet" && name !== "ordered") || match[1].length < baseIndent) break;
    if (match[1].length > baseIndent) {
      if (!items.length) break;
      const nested = [];
      index = list(lines, index, nested, name);
      items[items.length - 1] += nested.join("");
      continue;
    }
    if (name !== kind) break;
    items.push(inline(match[2]));
    index += 1;
  }

  out.push(`<${tag}>${items.map((item) => `<li>${item}</li>`).join("")}</${tag}>`);
  return index;
}

function table(lines, start, out, header) {
  const rows = [];
  let index = start + 2; // header row + divider row
  while (index < lines.length) {
    const { name, match } = classify(lines[index]);
    if (name !== "row") break;
    rows.push(cells(match));
    index += 1;
  }
  // A cell's text sits in a block with a least and a most width (styles/chat.css):
  // a narrow screen scrolls the table instead of breaking every word in it.
  const head = header.map((cell) => `<th><div class="cell">${inline(cell)}</div></th>`).join("");
  const body = rows
    .map((row) => `<tr>${row.map((cell) => `<td><div class="cell">${inline(cell)}</div></td>`).join("")}</tr>`)
    .join("");
  out.push(`<div class="tableScroll"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`);
  return index;
}

/** Plain text with line breaks - for content that is not markdown. */
export function renderPlain(text) {
  return escapeHtml(text).replace(/\n/g, "<br>");
}
