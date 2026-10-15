/**
 * Diagrams in assistant output: fenced ```mermaid and ```svg blocks become an
 * interactive `.diagramCard` (Diagram / Code, zoom, pan, fullscreen, export).
 *
 * Three properties shape this module:
 *
 * 1. **Streaming.** The answer is repainted via `body.innerHTML` on every
 *    frame, so a card is rebuilt each time. A block still being written (no
 *    `data-closed`) stays plain code with a status; a finished one is drawn
 *    once and kept in `cache`, so every later frame inserts the ready SVG
 *    synchronously - no flicker. Per-card UI state (tab, zoom, pan) lives in a
 *    WeakMap keyed by the message container, so it survives the rebuilds.
 * 2. **Safety.** Mermaid runs with `securityLevel: "strict"`, and every SVG -
 *    ours or the model's own - passes `sanitizeSvg` before it reaches the DOM.
 * 3. **Offline.** Mermaid is vendored (`mermaid.lib.js`) and loaded lazily,
 *    only when the first diagram appears.
 */

import { $$, h, icon } from "./dom.js";
import { ICONS } from "../ui/icons.js";
import { copyText, toast } from "../ui/toast.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const MERMAID_SRC = new URL("./mermaid.lib.js", import.meta.url).href;
const LANGS = { mermaid: "mermaid", mmd: "mermaid", diagram: "mermaid", svg: "svg" };
const CACHE_LIMIT = 80;
const SCALE_MIN = 0.2;
const SCALE_MAX = 8;

const PATH = {
  minus: "M5 12h14",
  fullscreen: "M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5",
  download: "M12 4v11M7 11l5 5 5-5M5 20h14",
};

/* ------------------------------------------------------------------ theme */

const lightQuery = globalThis.matchMedia?.("(prefers-color-scheme: light)") ?? null;

/** "light" or "dark": an explicit `data-theme` wins over the OS preference. */
function themeKey() {
  const set = document.documentElement.dataset.theme;
  if (set === "light" || set === "dark") return set;
  return lightQuery?.matches ? "light" : "dark";
}

const COLOR = /^(#[0-9a-f]{3,8}|rgba?\(|hsla?\()/i;

/** Mermaid's color parser accepts hex/rgb/hsl only; anything else falls back. */
function themeVariables(key) {
  const css = getComputedStyle(document.documentElement);
  const dark = key === "dark";
  const color = (name, fallback) => {
    const value = css.getPropertyValue(name).trim();
    return COLOR.test(value) ? value : fallback;
  };
  const bg = color("--bg", dark ? "#0f1115" : "#ffffff");
  const raised = color("--bg-2", dark ? "#171a21" : "#f7f8fa");
  const inset = color("--bg-inset", dark ? "#1d2129" : "#eef0f3");
  const text = color("--text", dark ? "#e8eaed" : "#1b1d21");
  const text2 = color("--text-2", dark ? "#a6adb8" : "#4a505a");
  const line = color("--line", dark ? "#2b303a" : "#d9dce1");
  const strong = color("--line-strong", dark ? "#3b4250" : "#b7bcc5");
  const accent = color("--accent", "#5b8def");
  const font = css.getPropertyValue("--font").trim() || "sans-serif";
  return {
    font,
    vars: {
      darkMode: dark, background: bg, fontFamily: font, fontSize: "14px",
      primaryColor: raised, primaryTextColor: text, primaryBorderColor: strong,
      secondaryColor: inset, secondaryTextColor: text, secondaryBorderColor: line,
      tertiaryColor: bg, tertiaryTextColor: text, tertiaryBorderColor: line,
      lineColor: text2, textColor: text, mainBkg: raised, nodeBorder: strong, nodeTextColor: text,
      clusterBkg: inset, clusterBorder: line, titleColor: text, edgeLabelBackground: bg,
      noteBkgColor: inset, noteTextColor: text, noteBorderColor: line,
      actorBkg: raised, actorBorder: strong, actorTextColor: text, actorLineColor: text2,
      signalColor: text2, signalTextColor: text, labelBoxBkgColor: raised, labelBoxBorderColor: strong,
      labelTextColor: text, loopTextColor: text, activationBkgColor: inset, activationBorderColor: accent,
    },
  };
}

let watching = false;
let lastTheme = null;

/** Redraw every card when the theme flips (explicit toggle or OS preference). */
function watchTheme() {
  if (watching) return;
  watching = true;
  lastTheme = themeKey();
  const changed = () => {
    const key = themeKey();
    if (key === lastTheme) return;
    lastTheme = key;
    for (const card of document.querySelectorAll(".diagramCard")) controllers.get(card)?.render();
  };
  new MutationObserver(changed).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  lightQuery?.addEventListener?.("change", changed);
}

/* ---------------------------------------------------------------- sanitize */

const BLOCKED = new Set(["script", "foreignobject", "iframe", "object", "embed", "audio", "video", "canvas", "link", "meta", "base"]);

/** Only in-document refs and raster data URLs; links may also leave the page. */
function safeRef(element, value) {
  const ref = value.replace(/[\u0000-\u0020]+/g, "");
  if (ref.startsWith("#")) return true;
  if (/^data:image\/(png|jpe?g|gif|webp);/i.test(ref)) return true;
  return element.localName.toLowerCase() === "a" && /^(https?:|mailto:)/i.test(ref);
}

/** Drop @import and any url() that is not a local `#id` reference. */
const cleanCss = (css) => css.replace(/@import[^;]*;?/gi, "").replace(/url\(\s*(['"]?)(?!#)[^)]*\)/gi, "none");

/**
 * Confine a model-written stylesheet to its own SVG: inlined in the page, a bare
 * `body {…}` rule would restyle the whole chat. At-rules are dropped.
 */
function scopeCss(css, scope) {
  return css
    .replace(/@[^{;]+\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}/g, "")
    .replace(/(^|\})\s*([^{}]+)\{/g, (_, close, selectors) =>
      `${close}${selectors.split(",").map((s) => `#${scope} ${s.trim()}`).join(", ")}{`);
}

/**
 * Parse *rawSvg* as XML and strip everything active. Returns the root element
 * (owned by its own document - `importNode` it), or throws. With *scope* (an
 * id), the SVG's own <style> rules are confined to that id.
 */
function sanitizeToNode(rawSvg, scope = "") {
  const doc = new DOMParser().parseFromString(String(rawSvg ?? "").trim(), "image/svg+xml");
  const root = doc.documentElement;
  if (!root || doc.querySelector("parsererror") || root.localName.toLowerCase() !== "svg") {
    throw new Error("The SVG is not well-formed");
  }
  for (const element of [root, ...root.querySelectorAll("*")]) {
    const name = element.localName.toLowerCase();
    if (element !== root && BLOCKED.has(name)) {
      element.remove();
      continue;
    }
    if ((name === "animate" || name === "set") && /^(xlink:)?href$|^on/i.test(element.getAttribute("attributeName") ?? "")) {
      element.remove();
      continue;
    }
    if (name === "style") element.textContent = scope ? scopeCss(cleanCss(element.textContent), scope) : cleanCss(element.textContent);
    for (const attr of [...element.attributes]) {
      const attrName = attr.localName.toLowerCase();
      if (attrName.startsWith("on")) element.removeAttributeNode(attr);
      else if (attrName === "href" && !safeRef(element, attr.value)) element.removeAttributeNode(attr);
      else if (attrName === "style") attr.value = cleanCss(attr.value);
    }
    if (name === "a") {
      element.setAttribute("target", "_blank");
      element.setAttribute("rel", "noreferrer noopener");
    }
  }
  if (scope) root.setAttribute("id", scope);
  return root;
}

/** Sanitized SVG markup, safe to inject; throws on malformed input. */
export function sanitizeSvg(rawSvg) {
  return new XMLSerializer().serializeToString(sanitizeToNode(rawSvg));
}

/* ----------------------------------------------------------------- mermaid */

let loader = null;

function loadMermaid() {
  if (globalThis.mermaid) return Promise.resolve(globalThis.mermaid);
  loader ??= new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = MERMAID_SRC;
    script.async = true;
    script.onload = () => (globalThis.mermaid ? resolve(globalThis.mermaid) : reject(transient("Mermaid did not initialise")));
    script.onerror = () => {
      loader = null;
      script.remove();
      reject(transient("Mermaid could not be loaded"));
    };
    document.head.append(script);
  });
  return loader;
}

/** A failure that says nothing about the diagram itself: never cached. */
function transient(message) {
  return Object.assign(new Error(message), { transient: true });
}

let configuredFor = null;
let uid = 0;
let queue = Promise.resolve();

/** Mermaid keeps global state, so renders run strictly one after another. */
function enqueue(task) {
  const run = queue.then(task);
  queue = run.catch(() => {});
  return run;
}

function renderMermaid(code) {
  return enqueue(async () => {
    const mermaid = await loadMermaid();
    const key = themeKey();
    if (configuredFor !== key) {
      const { font, vars } = themeVariables(key);
      mermaid.initialize({
        startOnLoad: false, theme: "base", securityLevel: "strict", suppressErrorRendering: true,
        htmlLabels: false, flowchart: { htmlLabels: false }, fontFamily: font, themeVariables: vars,
      });
      configuredFor = key;
    }
    const id = `grid-diagram-${(uid += 1)}`;
    try {
      const { svg } = await mermaid.render(id, code);
      // Mermaid emits HTML-flavoured markup in places; the XML parser needs it closed.
      return svg.replace(/<br\s*>/gi, "<br/>").replace(/&nbsp;/g, "&#160;");
    } finally {
      document.getElementById(id)?.remove();
      document.getElementById(`d${id}`)?.remove();
    }
  });
}

/** "flowchart", "sequenceDiagram", ... - the first keyword after any front matter. */
function diagramType(code) {
  const lines = code.replace(/^\s*---[\s\S]*?\n---\s*\n/, "").split("\n");
  const first = lines.find((line) => line.trim() && !line.trim().startsWith("%%"));
  return /^\s*([A-Za-z][\w-]*)/.exec(first ?? "")?.[1] ?? "";
}

/* ------------------------------------------------------------------- cache */

/** key -> {node, type} | {error}. Insertion order doubles as age. */
const cache = new Map();
/** key -> Promise<result>: concurrent frames share one render. */
const pending = new Map();

const cacheKey = (lang, code) => `${themeKey()}:${lang}:${code}`;

function remember(key, result) {
  if (result.transient) return result;
  cache.set(key, result);
  if (cache.size > CACHE_LIMIT) cache.delete(cache.keys().next().value);
  return result;
}

const errorText = (error) => String(error?.message ?? error).trim().split("\n").slice(0, 3).join("\n").slice(0, 400);

function buildSync(lang, code) {
  try {
    return { node: sanitizeToNode(code, `grid-svg-${(uid += 1)}`), type: "" };
  } catch (error) {
    return { error: errorText(error) };
  }
}

function peek(lang, code) {
  const key = cacheKey(lang, code);
  if (cache.has(key)) return cache.get(key);
  // An SVG block needs no engine: draw it in the same tick.
  return lang === "svg" ? remember(key, buildSync(lang, code)) : null;
}

function produce(lang, code) {
  const key = cacheKey(lang, code);
  let job = pending.get(key);
  if (!job) {
    job = renderMermaid(code.trim())
      .then(
        (raw) => ({ node: sanitizeToNode(raw), type: diagramType(code) }),
        (error) => ({ error: errorText(error), transient: Boolean(error?.transient) }),
      )
      .catch((error) => ({ error: errorText(error) }))
      .then((result) => remember(key, result))
      .finally(() => pending.delete(key));
    pending.set(key, job);
  }
  return job;
}

/* ------------------------------------------------------------------ export */

function svgSize(node) {
  const box = (node.getAttribute("viewBox") ?? "").split(/[\s,]+/).map(Number);
  if (box.length === 4 && box[2] > 0 && box[3] > 0) return { width: box[2], height: box[3] };
  const width = parseFloat(node.getAttribute("width"));
  const height = parseFloat(node.getAttribute("height"));
  return { width: width > 0 ? width : 800, height: height > 0 ? height : 600 };
}

/** Standalone SVG file content with explicit pixel size. */
function exportSvg(result) {
  const clone = result.node.cloneNode(true);
  const { width, height } = svgSize(clone);
  clone.setAttribute("xmlns", SVG_NS);
  clone.setAttribute("width", width);
  clone.setAttribute("height", height);
  clone.style?.removeProperty("max-width");
  return { svg: new XMLSerializer().serializeToString(clone), width, height };
}

function save(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = h("a", { href: url, download: filename });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

function downloadSvg(result, name) {
  save(new Blob([exportSvg(result).svg], { type: "image/svg+xml;charset=utf-8" }), `${name}.svg`);
}

/** PNG at 2x pixel density on the page background. */
async function downloadPng(result, name) {
  const { svg, width, height } = exportSvg(result);
  const url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml;charset=utf-8" }));
  try {
    const image = new Image();
    await new Promise((resolve, reject) => {
      image.onload = resolve;
      image.onerror = () => reject(new Error("The diagram could not be rasterised"));
      image.src = url;
    });
    const factor = Math.min(2, 8192 / Math.max(width, height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(width * factor));
    canvas.height = Math.max(1, Math.round(height * factor));
    const context = canvas.getContext("2d");
    context.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim() || "#ffffff";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((resolve, reject) => canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("PNG export failed"))), "image/png"));
    save(blob, `${name}.png`);
  } finally {
    URL.revokeObjectURL(url);
  }
}

/* ------------------------------------------------------------------ viewer */

/**
 * Pan/zoom surface. *state* ({scale, panX, panY}) is owned by the caller so it
 * can outlive this element. Drag pans; Ctrl/Cmd + wheel zooms (any wheel when
 * *wheelAlways* - the fullscreen view has nothing else to scroll).
 */
function createViewer(state, { onChange, wheelAlways = false } = {}) {
  const canvas = h("div.diagramCanvas");
  const viewport = h("div.diagramViewport", { tabindex: "0" }, canvas);
  const apply = () => {
    canvas.style.transform = `translate(${state.panX}px, ${state.panY}px) scale(${state.scale})`;
    onChange?.();
  };
  const zoomBy = (factor) => {
    state.scale = Math.min(SCALE_MAX, Math.max(SCALE_MIN, state.scale * factor));
    apply();
  };
  const reset = () => {
    Object.assign(state, { scale: 1, panX: 0, panY: 0 });
    apply();
  };

  let drag = null;
  viewport.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    drag = { x: event.clientX, y: event.clientY, panX: state.panX, panY: state.panY };
    viewport.classList.add("is-panning");
    viewport.setPointerCapture?.(event.pointerId);
  });
  viewport.addEventListener("pointermove", (event) => {
    if (!drag) return;
    state.panX = drag.panX + event.clientX - drag.x;
    state.panY = drag.panY + event.clientY - drag.y;
    apply();
  });
  const stop = (event) => {
    if (!drag) return;
    drag = null;
    viewport.classList.remove("is-panning");
    viewport.releasePointerCapture?.(event.pointerId);
  };
  viewport.addEventListener("pointerup", stop);
  viewport.addEventListener("pointercancel", stop);
  viewport.addEventListener(
    "wheel",
    (event) => {
      if (!wheelAlways && !event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      zoomBy(event.deltaY < 0 ? 1.1 : 1 / 1.1);
    },
    { passive: false },
  );

  return {
    viewport,
    apply,
    reset,
    zoom: (kind) => (kind === "reset" ? reset() : zoomBy(kind === "in" ? 1.25 : 0.8)),
    setSvg(node) {
      canvas.replaceChildren(document.importNode(node, true));
      apply();
    },
  };
}

/* ---------------------------------------------------------------------- UI */

function iconButton(path, title, attrs = {}, label = "") {
  return h(
    "button.iconBtn.iconBtn--xs",
    { type: "button", title, "aria-label": title, ...attrs },
    icon(path, { size: 13 }),
    label ? h("span.iconBtn__text", { text: label }) : null,
  );
}

/** `−  100%  +`, driving whichever viewer *getViewer* returns. */
function createZoomGroup(getViewer, state) {
  const pct = h("button.diagramZoom__pct", { type: "button", "data-zoom": "reset", title: "Reset zoom" });
  const el = h(
    "div.diagramZoom",
    {},
    iconButton(PATH.minus, "Zoom out", { "data-zoom": "out" }),
    pct,
    iconButton(ICONS.plus, "Zoom in", { "data-zoom": "in" }),
  );
  el.addEventListener("click", (event) => {
    const kind = event.target.closest("[data-zoom]")?.dataset.zoom;
    if (kind) getViewer()?.zoom(kind);
  });
  return { el, sync: () => { pct.textContent = `${Math.round(state.scale * 100)}%`; } };
}

/** Run an export, reporting a failure as a toast instead of an unhandled rejection. */
async function attempt(job) {
  try {
    await job();
  } catch (error) {
    toast(errorText(error), { timeout: 4000 });
  }
}

/** Fullscreen view: its own zoom state, closed by Escape, the backdrop or ×. */
function openModal({ title, result, name }) {
  const opener = document.activeElement;
  const state = { scale: 1, panX: 0, panY: 0 };
  let zoom = null;
  const viewer = createViewer(state, { wheelAlways: true, onChange: () => zoom?.sync() });
  zoom = createZoomGroup(() => viewer, state);
  viewer.setSvg(result.node);

  const close = () => {
    document.removeEventListener("keydown", onKey, true);
    modal.remove();
    opener?.focus?.();
  };
  const onKey = (event) => {
    if (event.key !== "Escape") return;
    event.stopPropagation();
    close();
  };
  const modal = h(
    "div.diagramModal",
    { role: "dialog", "aria-modal": "true", "aria-label": title },
    h(
      "div.diagramModal__panel",
      {},
      h(
        "div.diagramBar",
        {},
        h("div.diagramBar__title", {}, h("span.diagramBar__lang", { text: title })),
        h(
          "div.diagramBar__actions",
          {},
          zoom.el,
          iconButton(PATH.download, "Download SVG", { "data-action": "svg", on: { click: () => attempt(() => downloadSvg(result, name)) } }, "SVG"),
          iconButton(PATH.download, "Download PNG", { "data-action": "png", on: { click: () => attempt(() => downloadPng(result, name)) } }, "PNG"),
          iconButton(ICONS.close, "Close", { "data-action": "close", on: { click: close } }),
        ),
      ),
      viewer.viewport,
    ),
  );
  modal.addEventListener("click", (event) => {
    if (event.target === modal) close();
  });
  document.addEventListener("keydown", onKey, true);
  document.body.append(modal);
  zoom.sync();
  viewer.viewport.focus();
}

/** Build one card for a finished block. Returns its element and a `render` hook. */
function createCard({ lang, code, source, state }) {
  const name = lang === "svg" ? "diagram" : `${diagramType(code) || "diagram"}`.toLowerCase();
  let result = null;
  let ticket = 0;
  let zoom = null;

  const viewer = createViewer(state, { onChange: () => zoom?.sync() });
  zoom = createZoomGroup(() => viewer, state);

  const badge = h("span.diagramBar__type", { hidden: true });
  const tabs = ["diagram", "code"].map((view) =>
    h("button.diagramTab", { type: "button", "data-view": view, on: { click: () => { state.view = view; layout(); } } }, view === "diagram" ? "Diagram" : "Code"),
  );
  const actions = {
    fullscreen: iconButton(PATH.fullscreen, "Fullscreen", { "data-action": "fullscreen" }),
    svg: iconButton(PATH.download, "Download SVG", { "data-action": "svg" }, "SVG"),
    png: iconButton(PATH.download, "Download PNG", { "data-action": "png" }, "PNG"),
    copy: iconButton(ICONS.copy, "Copy code", { "data-action": "copy" }),
  };
  const bar = h(
    "div.diagramBar",
    {},
    h("div.diagramBar__title", {}, h("span.diagramBar__lang", { text: lang }), badge),
    h("div.diagramBar__actions", {}, h("div.diagramTabs", { role: "group", "aria-label": "View" }, tabs), zoom.el, Object.values(actions)),
  );
  bar.addEventListener("click", (event) => {
    const action = event.target.closest("[data-action]")?.dataset.action;
    if (action === "copy") copyText(code, "Code copied");
    if (!result?.node) return;
    if (action === "fullscreen") openModal({ title: lang, result, name });
    if (action === "svg") attempt(() => downloadSvg(result, name));
    if (action === "png") attempt(() => downloadPng(result, name));
  });

  const errorBox = h("div.diagramCard__error", { role: "alert", hidden: true });
  const sourceBox = h("div.diagramSource", {}, source);
  const el = h("figure.diagramCard", { dataset: { lang } }, bar, viewer.viewport, errorBox, sourceBox);

  /** Show the diagram, or the code while it is not ready / has failed. */
  function layout() {
    const failed = Boolean(result?.error);
    const code_ = failed || !result || state.view === "code";
    el.classList.toggle("is-loading", !result);
    el.classList.toggle("is-error", failed);
    viewer.viewport.hidden = code_;
    sourceBox.hidden = !code_;
    errorBox.hidden = !failed;
    zoom.el.hidden = code_;
    for (const tab of tabs) {
      const active = tab.dataset.view === (code_ ? "code" : "diagram");
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-pressed", String(active));
      tab.disabled = tab.dataset.view === "diagram" && !result?.node;
    }
    for (const key of ["fullscreen", "svg", "png"]) actions[key].disabled = !result?.node;
    badge.hidden = !result?.type;
    badge.textContent = result?.type ?? "";
    zoom.sync();
  }

  function show(next) {
    result = next;
    if (next?.node) viewer.setSvg(next.node);
    if (next?.error) {
      errorBox.replaceChildren(h("strong", { text: "Diagram error" }), h("span", { text: next.error }));
    }
    layout();
  }

  const controller = {
    render() {
      const hit = peek(lang, code);
      if (hit) {
        ticket += 1;
        show(hit);
        return;
      }
      // A redraw (theme change) keeps the old picture until the new one is ready.
      if (!result) show(null);
      const mine = (ticket += 1);
      produce(lang, code).then((done) => {
        if (mine === ticket) show(done);
      });
    },
  };
  controllers.set(el, controller);
  controller.render();
  return el;
}

/** Card element -> controller, for the theme redraw. */
const controllers = new WeakMap();
/** Message container -> block index -> UI state; survives innerHTML repaints. */
const states = new WeakMap();

function stateFor(container, index) {
  let perContainer = states.get(container);
  if (!perContainer) states.set(container, (perContainer = new Map()));
  if (!perContainer.has(index)) perContainer.set(index, { view: "diagram", scale: 1, panX: 0, panY: 0 });
  return perContainer.get(index);
}

/** A block still being written: stays code, with a visible status. */
function markStreaming(pre) {
  pre.dataset.rendering = "true";
  const bar = pre.querySelector(".codeBar");
  if (!bar || bar.querySelector(".codeBar__status")) return;
  const status = h("span.codeBar__status", { text: "Rendering diagram…" });
  const lang = bar.querySelector(".codeBar__lang");
  if (lang) lang.after(status);
  else bar.prepend(status);
}

/**
 * Turn the mermaid/svg code blocks inside *container* into diagram cards.
 * Call after `decorateCode`, on freshly rendered markdown.
 */
export function decorateDiagrams(container) {
  if (!container) return;
  let index = 0;
  for (const pre of $$("pre[data-language]", container)) {
    const lang = LANGS[pre.dataset.language.toLowerCase()];
    if (!lang) continue;
    const state = stateFor(container, index);
    index += 1;
    if (pre.dataset.closed !== "true") {
      markStreaming(pre);
      continue;
    }
    watchTheme();
    const codeNode = pre.querySelector("code");
    const code = codeNode?.textContent ?? "";
    // Reuse the already highlighted <code> for the Code view.
    const source = h("pre", { dataset: { decorated: "true" } }, codeNode);
    pre.replaceWith(createCard({ lang, code, source, state }));
  }
}
