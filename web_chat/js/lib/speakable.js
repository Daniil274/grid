/**
 * Turning written text into text worth hearing.
 *
 * Markdown is written for the eye. Read out verbatim it becomes noise: asterisk
 * asterisk, pipe pipe pipe, "core slash agent underscore factory dot py". This
 * module is the filter between an answer and the synthesizer, and it follows
 * two rules:
 *
 * - **Drop what only means something on screen** - code blocks, tables, rules,
 *   image refs, decoration. Silence is better than a wrong reading, and the
 *   text is still on screen for whoever wants it.
 * - **Say the rest the way a person would** - a path is read by its name, not
 *   its separators; a link is read by its label; a list item is a sentence.
 *
 * Every step is a named rule applied in order, so what happens to a given
 * string can be read off the list rather than inferred from one long regex.
 * Rules must be language-neutral: the text may be in any language, and a
 * marker word inserted in the wrong one is worse than dropping the content.
 */

/** A path or filename: separators, no spaces, and no letters of a natural script. */
const PATH_LIKE = /^[\w@.*-]*(?:[/\\][\w@.*-]+)+$/;
/** `file_name.ext`, the one-segment case of the above. */
const FILE_LIKE = /^[\w@*-]+\.[A-Za-z][\w]{0,4}$/;

/** Read a path as its name: `core/agent_factory.py` -> `agent factory`. */
export function humanizePath(token) {
  const segments = token.split(/[/\\]/).filter((part) => part && part !== "*" && part !== "." && part !== "..");
  const name = segments.at(-1) ?? token;
  return name
    .replace(/\.[A-Za-z][\w]{0,4}$/, "") // trailing extension
    .replace(/[_.-]+/g, " ")
    .replace(/\*/g, " ")
    .trim();
}

/** Identifiers read better as words: `--no-verify` -> `no verify`. */
export function humanizeToken(token) {
  const trimmed = token.trim();
  if (!trimmed) return "";
  if (PATH_LIKE.test(trimmed) || FILE_LIKE.test(trimmed)) return humanizePath(trimmed);
  return trimmed
    .replace(/^-{1,2}(?=\w)/, "") // CLI flags
    .replace(/(\w)_(?=\w)/g, "$1 ") // snake_case
    .trim();
}

const dropLines = (text, pattern) =>
  text
    .split("\n")
    .filter((line) => !pattern.test(line))
    .join("\n");

/**
 * The pipeline, in order. Each entry is `[name, apply]`; the name is
 * documentation and appears in nothing but this file.
 */
const RULES = [
  // Screen-only blocks go first: later rules must not see their contents.
  [
    // `$` is per line under /m, so one lazy match would stop at the first
    // newline and leave the body behind: match a closed fence, then a fence
    // left open by a truncated answer.
    "fenced code",
    (text) =>
      text
        .replace(/(^|\n)[ \t]*(?:```|~~~)[^\n]*\n[\s\S]*?\n[ \t]*(?:```|~~~)[ \t]*(?=\n|$)/g, "\n")
        .replace(/(^|\n)[ \t]*(?:```|~~~)[\s\S]*$/g, "\n"),
  ],
  ["images", (text) => text.replace(/!\[[^\]]*\]\([^)]*\)/g, " ")],
  ["links", (text) => text.replace(/\[([^\]\n]+)\]\([^)]*\)/g, "$1")],
  ["table rows", (text) => dropLines(text, /^\s*\|.*\|\s*$/)],
  ["horizontal rules", (text) => dropLines(text, /^\s*([-*_=])(?:\s*\1){2,}\s*$/)],

  // Line furniture: markers carry structure, and structure is silence.
  ["headings", (text) => text.replace(/^\s{0,3}#{1,6}\s+/gm, "")],
  ["block quotes", (text) => text.replace(/^\s{0,3}>\s?/gm, "")],
  ["bullets", (text) => text.replace(/^\s*[-*+•]\s+/gm, "")],
  ["numbered items", (text) => text.replace(/^\s*\d{1,3}[.)]\s+/gm, "")],
  ["task boxes", (text) => text.replace(/^\s*\[[ xX]\]\s*/gm, "")],

  // Inline spans.
  ["inline code", (text) => text.replace(/`([^`\n]+)`/g, (_, code) => humanizeToken(code))],
  ["emphasis", (text) => text.replace(/(\*\*|__|~~|\*|_)/g, "")],
  [
    // One pass for both: as separate rules the path rule saw the host the url
    // rule had just produced and ate its ".com".
    "urls and paths",
    (text) =>
      text.replace(
        /(^|[\s(])(https?:\/\/[^\s)]+|[\w@.*-]*(?:[/\\][\w@.*-]+)+|[\w@*-]+\.[A-Za-z]\w{0,4})(?=[\s,;:)]|$)/g,
        (match, lead, token) => {
          if (/^https?:/i.test(token)) {
            return `${lead}${token.replace(/^https?:\/\/(?:www\.)?/i, "").split(/[/?#]/)[0]}`;
          }
          // A token carrying non-ASCII letters is prose ("и/или"), not a path.
          return /[^\x00-\x7F]/.test(token) ? match : `${lead}${humanizePath(token)}`;
        },
      ),
  ],
  ["snake case", (text) => text.replace(/(\w)_(?=\w)/g, "$1 ")],

  // Anything left that has no pronunciation.
  ["symbols", (text) => text.replace(/[\p{Extended_Pictographic}\p{So}\p{Sk}]/gu, " ")],
  ["stray brackets", (text) => text.replace(/[<>|`]+/g, " ")],

  // Tidy up so the sentence splitter sees clean punctuation.
  ["repeated punctuation", (text) => text.replace(/([!?,;:])\1+/g, "$1").replace(/\.{3,}/g, "…")],
  ["orphan punctuation", (text) => text.replace(/\s+([,.;:!?…])/g, "$1").replace(/\(\s*\)/g, " ")],
  ["whitespace", (text) => text.replace(/[ \t]+/g, " ").replace(/\n{3,}/g, "\n\n")],
  ["blank lines", (text) => text.split("\n").map((line) => line.trim()).filter((line, index, all) => line || all[index - 1]).join("\n")],
];

/**
 * @param {string} markdown text as it is written on screen
 * @returns {string} text as it should be heard - possibly empty, when the
 *   original was nothing but code, tables or decoration
 */
export function speakableText(markdown) {
  return RULES.reduce((text, [, apply]) => apply(text), String(markdown ?? "")).trim();
}

/** Rule names, in order - for tests and for anyone debugging a reading. */
export const RULE_NAMES = RULES.map(([name]) => name);
