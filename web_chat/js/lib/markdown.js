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
 * 2. **Streaming tolerance.** Half-typed emphasis and unclosed code fences are
 *    normal mid-stream; they render as themselves instead of corrupting the
 *    rest of the document.
 */

import { highlight } from "./highlight.js";

const SAFE_HREF = /^(https?:\/\/|mailto:|#|\/)/i;
/** Sentinel around extracted code spans: escapeHtml never emits this entity, so it cannot collide. */
const MARK = '&#0;';

export function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function safeHref(raw) {
  const href = raw.trim();
  return SAFE_HREF.test(href) ? href : null;
}

/** Inline spans. Code is lifted out first so emphasis never runs inside it. */
function inline(escaped) {
  const codeSpans = [];
  let text = escaped.replace(/`([^`\n]+)`/g, (_, code) => `${MARK}${codeSpans.push(code) - 1}${MARK}`);

  text = text
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

  return text.replace(new RegExp(`${MARK}(\\d+)${MARK}`, "g"), (_, i) => `<code>${codeSpans[Number(i)]}</code>`);
}

const BLOCK = {
  fence: /^\s*(?:```|~~~)\s*([\w+#.-]*)\s*$/,
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
  for (const name of ["fence", "heading", "rule", "quote", "bullet", "ordered", "row"]) {
    const match = BLOCK[name].exec(line);
    if (match) return { name, match };
  }
  return { name: line.trim() ? "text" : "blank", match: null };
}

const cells = (match) => match[1].split("|").map((cell) => cell.trim());
const isDivider = (row) => row.length > 0 && row.every((cell) => /^:?-{2,}:?$/.test(cell));

/**
 * @param {string} source raw markdown
 * @returns {string} HTML, safe to assign to innerHTML
 */
export function renderMarkdown(source) {
  return blocks(escapeHtml(source ?? "").replace(/\r\n?/g, "\n").split("\n"));
}

/** Render already-escaped lines. All recursion goes through here. */
function blocks(lines) {
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
        index += 1; // closing fence; absent mid-stream, which is fine
        const language = match[1] ? ` data-language="${match[1]}"` : "";
        const source = body.join("\n");
        out.push(`<pre${language}><code>${highlight(source, match[1])}</code></pre>`);
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
  return out.join("\n");
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
  const head = header.map((cell) => `<th>${inline(cell)}</th>`).join("");
  const body = rows.map((row) => `<tr>${row.map((cell) => `<td>${inline(cell)}</td>`).join("")}</tr>`).join("");
  out.push(`<div class="tableScroll"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`);
  return index;
}

/** Plain text with line breaks - for content that is not markdown. */
export function renderPlain(text) {
  return escapeHtml(text).replace(/\n/g, "<br>");
}
