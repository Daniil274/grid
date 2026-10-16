/**
 * Formulas in assistant output: the `.math` spans and blocks that markdown.js
 * leaves for TeX written between \( \) or $ $ (inline) and \[ \] or $$ $$
 * (display) are typeset with KaTeX.
 *
 * As with diagrams (lib/diagrams.js):
 *
 * 1. **Offline.** KaTeX is vendored (`katex/`) and loaded only when the first
 *    formula appears.
 * 2. **Streaming.** The answer is repainted on every frame, so each formula is
 *    typeset once and kept in `cache`; later frames insert it synchronously.
 * 3. **Safety.** KaTeX runs with `trust: false`: no \href, \url or HTML
 *    extension reaches the page. A formula KaTeX cannot read stays its source.
 */

const KATEX_SRC = new URL("./katex/katex.min.js", import.meta.url).href;
const KATEX_CSS = new URL("./katex/katex.min.css", import.meta.url).href;
const CACHE_LIMIT = 400;
const OPTIONS = { throwOnError: true, strict: "ignore", trust: false, output: "htmlAndMathml" };

/** Typeset HTML by mode and source; null for a source KaTeX rejects. */
const cache = new Map();
let loader = null;

function loadKatex() {
  if (globalThis.katex) return Promise.resolve(globalThis.katex);
  loader ??= new Promise((resolve, reject) => {
    const style = document.createElement("link");
    style.rel = "stylesheet";
    style.href = KATEX_CSS;
    document.head.append(style);
    const script = document.createElement("script");
    script.src = KATEX_SRC;
    script.async = true;
    script.onload = () => (globalThis.katex ? resolve(globalThis.katex) : reject(new Error("KaTeX did not initialise")));
    script.onerror = () => {
      loader = null;
      style.remove();
      script.remove();
      reject(new Error("KaTeX could not be loaded"));
    };
    document.head.append(script);
  });
  return loader;
}

function typeset(katex, source, display) {
  const key = `${display ? "D" : "I"}${source}`;
  if (cache.has(key)) return cache.get(key);
  let html = null;
  try {
    html = katex.renderToString(source, { ...OPTIONS, displayMode: display });
  } catch {
    // Not TeX that KaTeX reads - often a "$" that was only a dollar sign.
  }
  if (cache.size >= CACHE_LIMIT) cache.delete(cache.keys().next().value);
  cache.set(key, html);
  return html;
}

function apply(katex, node) {
  if (node.dataset.typeset) return;
  node.dataset.typeset = "true";
  const html = typeset(katex, node.textContent, node.classList.contains("math--display"));
  if (html === null) node.classList.add("math--source");
  else node.innerHTML = html;
}

/** Typeset the formulas inside *container*; call on freshly rendered markdown. */
export function decorateMath(container) {
  if (!container) return;
  const nodes = [...container.querySelectorAll(".math")].filter((node) => !node.dataset.typeset);
  if (!nodes.length) return;
  if (globalThis.katex) {
    for (const node of nodes) apply(globalThis.katex, node);
    return;
  }
  // Until KaTeX arrives a formula reads as its source; a node a later frame
  // replaced meanwhile is typeset all the same, which costs one cached lookup.
  loadKatex().then((katex) => nodes.forEach((node) => apply(katex, node)), () => {});
}
