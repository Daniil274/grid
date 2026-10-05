/**
 * Which highlight.js grammar fits a tool part.
 *
 * Tool payloads rarely say what language their code is in, but they almost
 * always name a file. The extension is a far better signal than guessing from
 * content, and it also repairs chats stored before the backend labelled
 * languages. Values are highlight.js names or aliases; a name the vendored
 * build does not know simply renders as plain escaped text (see highlight.js).
 */

const BY_EXT = {
  py: "python", pyi: "python", pyw: "python",
  js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "javascript",
  ts: "typescript", mts: "typescript", cts: "typescript", tsx: "typescript",
  json: "json", jsonc: "json", json5: "json", ipynb: "json", webmanifest: "json",
  yaml: "yaml", yml: "yaml", toml: "ini", ini: "ini", cfg: "ini", conf: "ini", properties: "properties",
  sh: "bash", bash: "bash", zsh: "bash", ps1: "powershell", psm1: "powershell", bat: "dos", cmd: "dos",
  html: "xml", htm: "xml", xml: "xml", svg: "xml", xsd: "xml", vue: "xml", plist: "xml",
  css: "css", scss: "scss", sass: "scss", less: "less",
  md: "markdown", markdown: "markdown", mdx: "markdown",
  sql: "sql",
  c: "c", h: "c", cc: "cpp", cpp: "cpp", cxx: "cpp", hpp: "cpp", hh: "cpp", ino: "cpp",
  cs: "csharp", java: "java", kt: "kotlin", kts: "kotlin", scala: "scala", groovy: "groovy", gradle: "groovy",
  go: "go", rs: "rust", swift: "swift", m: "objectivec", mm: "objectivec", dart: "dart",
  rb: "ruby", php: "php", pl: "perl", lua: "lua", r: "r", ex: "elixir", exs: "elixir", erl: "erlang",
  hs: "haskell", ml: "ocaml", fs: "fsharp", clj: "clojure", nix: "nix", proto: "protobuf",
  graphql: "graphql", gql: "graphql", tf: "ini", diff: "diff", patch: "diff",
  dockerfile: "dockerfile", mk: "makefile", cmake: "cmake", nginx: "nginx", vim: "vim",
};

const BY_NAME = {
  dockerfile: "dockerfile", makefile: "makefile", gnumakefile: "makefile",
  "cmakelists.txt": "cmake", "nginx.conf": "nginx", ".bashrc": "bash", ".zshrc": "bash",
  ".gitignore": "bash", ".env": "bash",
};

/** Labels the backend or models use that highlight.js has no grammar for. */
const ALIASES = { tsx: "typescript", jsx: "javascript", text: "", plain: "", plaintext: "", txt: "" };

const normalize = (value) => {
  const key = String(value ?? "").trim().toLowerCase();
  return Object.hasOwn(ALIASES, key) ? ALIASES[key] : key;
};

/** The grammar for a part: its own label first, then its file name; "" if unknown. */
export function languageFor(part) {
  const explicit = normalize(part?.language);
  if (explicit) return explicit;
  const label = String(part?.name ?? part?.uri ?? "").split(/[?#]/)[0];
  const base = label.split(/[\\/]/).pop().toLowerCase();
  if (!base) return "";
  if (Object.hasOwn(BY_NAME, base)) return BY_NAME[base];
  const dot = base.lastIndexOf(".");
  if (dot <= 0) return "";
  const ext = base.slice(dot + 1);
  return Object.hasOwn(BY_EXT, ext) ? BY_EXT[ext] : "";
}

/** Colour family of a tool, from its (possibly `server.`-prefixed) name. */
const FAMILY_RULES = [
  ["agent", /^(?:agent|webspider|orchestrate|system_agent|delegate)|(?:^|_)agent$/],
  ["shell", /^(?:bash|shell|run_command|exec|terminal|powershell)|bash_tool|(?:^|_)(?:command|shell)$/],
  ["web", /^(?:web_|fetch|browse|http)|(?:^|_)(?:fetch|browse|navigate)(?:_|$)/],
  ["task", /^(?:beads_|todo|pipeline|task_|control_)/],
  ["delete", /(?:^|_)(?:delete|remove|rm|unlink|drop)(?:_|$)/],
  ["edit", /(?:^|_)(?:edit|replace|patch|apply|revert|rename|move)(?:_|$)/],
  ["write", /(?:^|_)(?:write|append|create|save|mkdir|copy|fork|submit|finish|update|close)(?:_|$)/],
  ["search", /(?:^|_)(?:grep|glob|search|find|list|ls|log|impact|callers|callees|explore|context|files|tree)(?:_|$)/],
  ["read", /(?:^|_)(?:read|cat|view|open|get|show|status|node|diff|inspect|perceive|screenshot|crop)(?:_|$)/],
];

export function toolFamily(name) {
  const key = String(name ?? "").toLowerCase().split(".").pop();
  if (!key) return "";
  for (const [family, rule] of FAMILY_RULES) if (rule.test(key)) return family;
  return "";
}
