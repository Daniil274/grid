/**
 * Code and diff highlighter.
 *
 * Design constraints, in order of importance:
 *
 * 1. **Safety.** Input is *already HTML-escaped* by markdown.js and this module
 *    must never un-escape it. It only wraps runs of the existing text in its own
 *    `<span class="tok tok--…">` elements, so a fence containing `<span …>` stays
 *    inert text. Nothing here reads from or writes to the DOM.
 * 2. **Streaming.** The renderer runs on every frame over a growing string, and
 *    half-written code (unclosed strings, dangling comments, unclosed fences) is
 *    the normal case. Each language is matched with one sticky regex in a single
 *    left-to-right pass; the walker is O(n) and never backtracks across the
 *    whole document.
 * 3. **Offline.** No dependency, no tokenizer tables fetched at runtime.
 *
 * Because quotes arrive as `&quot;` / `&#39;`, the string patterns below match
 * the entities themselves and treat them as atomic single characters.
 */

const DQ = "&quot;";
const SQ = "&#39;";

/** A double-quoted, single-quoted or backtick string; text is pre-escaped. */
const STRING =
  `${DQ}(?:(?!${DQ})[^\n])*${DQ}` +
  `|${SQ}(?:(?!${SQ})[^\n])*${SQ}` +
  "|`(?:(?!`)[^\n])*`";

/** Decimal and hex literals, kept whole by word boundaries. */
const NUMBER = "\\b(?:0[xX][0-9a-fA-F]+|\\d+(?:\\.\\d+)?)\\b";

const keyword = (words) => `\\b(?:${words.join("|")})\\b`;

const PYTHON_KW = keyword([
  "and", "as", "assert", "async", "await", "break", "class", "continue", "def", "del", "elif",
  "else", "except", "False", "finally", "for", "from", "global", "if", "import", "in", "is",
  "lambda", "None", "nonlocal", "not", "or", "pass", "raise", "return", "True", "try", "while",
  "with", "yield", "self",
]);

const JS_KW = keyword([
  "async", "await", "break", "case", "catch", "class", "const", "continue", "default", "delete",
  "do", "else", "export", "extends", "false", "finally", "for", "from", "function", "if", "import",
  "in", "instanceof", "let", "new", "null", "of", "return", "static", "super", "switch", "this",
  "throw", "true", "try", "typeof", "undefined", "var", "void", "while", "yield",
]);

const SHELL_KW = keyword([
  "case", "do", "done", "elif", "else", "esac", "export", "fi", "for", "function", "if", "in",
  "local", "readonly", "return", "then", "until", "while",
]);

const JSON_KW = keyword(["true", "false", "null"]);

/** Assemble a single sticky alternation; the first matching branch wins. */
function compile(branches) {
  const parts = [];
  if (branches.str) parts.push(`(?<str>${branches.str})`);
  if (branches.com) parts.push(`(?<com>${branches.com})`);
  if (branches.kw) parts.push(`(?<kw>${branches.kw})`);
  if (branches.num) parts.push(`(?<num>${branches.num})`);
  return new RegExp(parts.join("|"), "gy");
}

const RULES = {
  json: compile({ str: STRING, kw: JSON_KW, num: NUMBER }),
  // Python keys on keywords, comments and strings only. Leaving bare digits
  // untouched keeps short snippets (e.g. `print(1)`) byte-identical.
  python: compile({ str: STRING, com: "#[^\\n]*", kw: PYTHON_KW }),
  js: compile({ str: STRING, com: "//[^\\n]*|/\\*[\\s\\S]*?\\*/", kw: JS_KW, num: NUMBER }),
  bash: compile({ str: STRING, com: "#[^\\n]*", kw: SHELL_KW, num: NUMBER }),
  // Universal fallback for other recognised languages: # and // comments,
  // strings and numbers. Deliberately language-agnostic.
  generic: compile({ str: STRING, com: "#[^\\n]*|//[^\\n]*|--[^\\n]*", num: NUMBER }),
};

/** Languages routed to each rule set; anything else is left untouched. */
const LANGUAGE = {
  json: "json", json5: "json", jsonc: "json",
  python: "python", py: "python",
  js: "js", jsx: "js", ts: "js", tsx: "js", javascript: "js", typescript: "js", mjs: "js", cjs: "js",
  bash: "bash", sh: "bash", shell: "bash", zsh: "bash", console: "bash",
};

const GENERIC_LANGS = new Set([
  "c", "h", "cpp", "c++", "cc", "hpp", "cs", "csharp", "java", "kt", "kotlin", "kts", "go",
  "golang", "rs", "rust", "sql", "yaml", "yml", "toml", "ini", "cfg", "conf", "ruby", "rb",
  "php", "swift", "scala", "lua", "pl", "perl", "r", "dart", "groovy", "gradle", "make",
  "makefile", "dockerfile", "hcl", "terraform", "proto", "graphql", "gql", "xml", "html", "css",
  "scss", "less", "vue", "svelte", "nix", "elixir", "ex", "exs", "clj", "clojure", "hs",
  "haskell", "zig", "tex", "vim", "awk", "sed",
]);

const DIFF_LANGS = new Set(["diff", "patch", "udiff"]);
const DIFF_HEAD = /^(?:diff --git|index |--- |\+\+\+ )/;

/** @param {string} text already-escaped fence body @returns {boolean} */
function looksLikeDiff(text) {
  for (const line of text.split("\n")) {
    if (!line) continue;
    return line.startsWith("diff --git") || line.startsWith("@@");
  }
  return false;
}

/** Wrap one diff line; line prefixes are escaped, so `<`/`>` arrive as entities. */
function diffLine(line) {
  if (line.startsWith("@@")) return wrap("dl--hunk", line);
  if (
    DIFF_HEAD.test(line) ||
    line.startsWith("--- ") ||
    line.startsWith("+++ ") ||
    line.startsWith("index ")
  ) {
    return wrap("dl--meta", line);
  }
  if (line.startsWith("+") || line.startsWith(">") || line.startsWith("&gt;")) return wrap("dl--add", line);
  if (line.startsWith("-") || line.startsWith("<") || line.startsWith("&lt;")) return wrap("dl--del", line);
  return line; // context lines and blank lines stay as they are
}

const wrap = (cls, text) => `<span class="${cls}">${text}</span>`;

function highlightDiff(text) {
  return text.split("\n").map(diffLine).join("\n");
}

/** Pull the class name out of the named group that matched. */
function tokenClass(groups) {
  if (groups.str !== undefined) return "str";
  if (groups.com !== undefined) return "com";
  if (groups.kw !== undefined) return "kw";
  return "num";
}

/**
 * Wrap every token found by `re` in a span. The regex is sticky, so `exec`
 * only ever tries the current position; a miss advances one character. Each
 * character is therefore inspected a bounded number of times — linear overall.
 */
function tokenize(text, re) {
  let out = "";
  let last = 0;
  let pos = 0;

  while (pos < text.length) {
    re.lastIndex = pos;
    const match = re.exec(text);
    if (!match) {
      pos += 1;
      continue;
    }
    out += text.slice(last, match.index);
    out += `<span class="tok tok--${tokenClass(match.groups)}">${match[0]}</span>`;
    last = re.lastIndex;
    pos = re.lastIndex;
  }
  return out + text.slice(last);
}

/**
 * @param {string} code already-escaped source (see markdown.escapeHtml)
 * @param {string} [lang] fence info string; unknown values yield plain text
 * @returns {string} HTML fragment consisting only of the original text plus
 *   `tok`/`dl` spans. Safe to embed inside `<code>`.
 */
export function highlight(code, lang) {
  const text = String(code ?? "");
  if (!text) return text;

  const language = String(lang ?? "").trim().toLowerCase();
  if (DIFF_LANGS.has(language) || looksLikeDiff(text)) return highlightDiff(text);

  const ruleName = LANGUAGE[language];
  if (ruleName) return tokenize(text, RULES[ruleName]);
  if (GENERIC_LANGS.has(language)) return tokenize(text, RULES.generic);
  return text;
}
