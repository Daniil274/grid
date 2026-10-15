import { test } from "node:test";
import assert from "node:assert/strict";
import { installFakeDom } from "./fake_dom.mjs";

installFakeDom();

// --- the browser pieces diagrams.js needs and fake_dom.mjs does not have -----

/** A just-enough XML element: what `sanitizeToNode` reads and edits. */
function xml(localName, attrs = {}, children = []) {
  const el = document.createElement(localName);
  el.localName = localName;
  el.attributes = Object.entries(attrs).map(([name, value]) => ({ name, localName: name, value }));
  el.getAttribute = (name) => el.attributes.find((a) => a.name === name)?.value ?? null;
  el.setAttribute = (name, value) => {
    const found = el.attributes.find((a) => a.name === name);
    if (found) found.value = String(value);
    else el.attributes.push({ name, localName: name, value: String(value) });
  };
  el.removeAttributeNode = (attr) => { el.attributes = el.attributes.filter((a) => a !== attr); };
  el.cloneNode = () => el;
  el.removed = false;
  el.remove = () => { el.removed = true; };
  el.kids = children;
  el.querySelectorAll = () => el.kids.flatMap((kid) => [kid, ...kid.querySelectorAll("*")]);
  return el;
}

// fake_dom.mjs has no `after`/`prepend`, and keeps `data-*` attributes apart
// from `dataset`; the browser reflects them.
const createElement = document.createElement;
document.createElement = (tag) => {
  const el = createElement(tag);
  const setAttribute = el.setAttribute.bind(el);
  el.setAttribute = (name, value) => {
    if (name.startsWith("data-")) el.dataset[name.slice(5)] = String(value);
    setAttribute(name, value);
  };
  el.after = (node) => {
    node.remove();
    const siblings = el.parentNode.childNodes;
    siblings.splice(siblings.indexOf(el) + 1, 0, node);
    node.parentNode = el.parentNode;
  };
  el.prepend = (node) => {
    node.remove();
    el.childNodes.unshift(node);
    node.parentNode = el;
  };
  return el;
};

globalThis.DOMParser = class {
  parseFromString(text) {
    if (/<broken/.test(text)) return { documentElement: xml("parsererror"), querySelector: () => ({}) };
    return { documentElement: globalThis.nextRoot?.() ?? xml("svg"), querySelector: () => null };
  }
};
globalThis.XMLSerializer = class {
  serializeToString(node) { return `<${node.localName} ${node.attributes.map((a) => `${a.name}="${a.value}"`).join(" ")}/>`; }
};
globalThis.getComputedStyle = () => ({ getPropertyValue: () => "" });
globalThis.MutationObserver = class { observe() {} };
document.documentElement = { dataset: {} };
document.importNode = (node) => node;
document.getElementById = () => null;
document.activeElement = null;

let rendered = 0;
globalThis.mermaid = {
  initialize() {},
  async render(_id, code) {
    rendered += 1;
    if (/BAD/.test(code)) throw new Error("Parse error on line 1");
    return { svg: "<svg viewBox='0 0 10 10'></svg>" };
  },
};

const { renderMarkdown } = await import("../web_chat/js/lib/markdown.js");
const { decorateDiagrams, sanitizeSvg } = await import("../web_chat/js/lib/diagrams.js");

const settle = () => new Promise((resolve) => setTimeout(resolve, 5));

/** A message body holding one `<pre data-language>` block, as renderMarkdown would emit. */
function body(language, { closed = true, code = "flowchart TD\n A-->B" } = {}) {
  const container = document.createElement("div");
  const pre = document.createElement("pre");
  pre.dataset.language = language;
  if (closed) pre.dataset.closed = "true";
  const bar = document.createElement("div");
  bar.classList.add("codeBar");
  const lang = document.createElement("span");
  lang.classList.add("codeBar__lang");
  bar.append(lang);
  const codeNode = document.createElement("code");
  codeNode.textContent = code;
  pre.append(bar, codeNode);
  container.append(pre);
  container.querySelectorAll = () => container.childNodes.filter((n) => n.tagName === "PRE");
  return { container, pre };
}

const card = (container) => container.childNodes.find((n) => n.classList?.contains("diagramCard"));

// --- renderMarkdown -----------------------------------------------------------

test("a closed fence is marked data-closed; one still streaming is not", () => {
  const closed = renderMarkdown("```mermaid\nflowchart TD\n A-->B\n```");
  assert.match(closed, /<pre data-language="mermaid" data-closed="true">/);
  const open = renderMarkdown("```mermaid\nflowchart TD\n A-->B");
  assert.match(open, /<pre data-language="mermaid">/);
  assert.doesNotMatch(open, /data-closed/);
});

test("the closing fence marker applies to every language and keeps code escaped", () => {
  const html = renderMarkdown("```svg\n<svg onload=\"x()\"><script>alert(1)</script></svg>\n```");
  assert.match(html, /data-language="svg" data-closed="true"/);
  assert.doesNotMatch(html, /<script>/);
  assert.doesNotMatch(renderMarkdown("```js\nlet a\n"), /data-closed/);
});

// --- decorateDiagrams ---------------------------------------------------------

test("other languages and a missing container are left alone", () => {
  decorateDiagrams(null);
  const { container, pre } = body("python");
  decorateDiagrams(container);
  assert.equal(container.childNodes[0], pre);
});

test("a diagram still being written stays code and shows a status", () => {
  const { container, pre } = body("mermaid", { closed: false });
  rendered = 0;
  decorateDiagrams(container);
  decorateDiagrams(container); // a repaint must not stack a second status
  assert.equal(container.childNodes[0], pre);
  assert.equal(pre.dataset.rendering, "true");
  assert.equal(pre.findAll("codeBar__status").length, 1);
  assert.equal(rendered, 0);
});

test("a finished mermaid block becomes a card that loads, then shows its type", async () => {
  const { container, pre } = body("mermaid", { code: "flowchart TD\n A-->B" });
  decorateDiagrams(container);
  const el = card(container);
  assert.ok(el, "the code block was replaced by a card");
  assert.equal(pre.parentNode, null);
  assert.equal(el.dataset.lang, "mermaid");
  assert.equal(el.classList.contains("is-loading"), true);
  await settle();
  assert.equal(el.classList.contains("is-loading"), false);
  assert.equal(el.classList.contains("is-error"), false);
  const badge = el.find("diagramBar__type");
  assert.equal(badge.textContent, "flowchart");
  assert.equal(badge.hidden, false);
  assert.equal(el.find("diagramCard__error").hidden, true);
  assert.equal(el.find("diagramSource").hidden, true);
  assert.equal(el.find("diagramViewport").hidden, false);
});

test("a repaint of an already drawn diagram is synchronous and draws nothing again", async () => {
  rendered = 0;
  const first = body("mermaid", { code: "sequenceDiagram\n A->>B: hi" });
  decorateDiagrams(first.container);
  await settle();
  assert.equal(rendered, 1);
  const again = body("mermaid", { code: "sequenceDiagram\n A->>B: hi" });
  decorateDiagrams(again.container);
  const el = card(again.container);
  assert.equal(el.classList.contains("is-loading"), false, "no flicker: the SVG is already there");
  assert.equal(el.find("diagramBar__type").textContent, "sequenceDiagram");
  assert.equal(rendered, 1);
});

test("a diagram the engine rejects shows its error and the source", async () => {
  const { container } = body("mermaid", { code: "BAD diagram" });
  decorateDiagrams(container);
  await settle();
  const el = card(container);
  assert.equal(el.classList.contains("is-error"), true);
  assert.equal(el.classList.contains("is-loading"), false);
  assert.equal(el.find("diagramCard__error").hidden, false);
  assert.match(el.find("diagramCard__error").textContent, /Parse error/);
  assert.equal(el.find("diagramSource").hidden, false);
  assert.equal(el.find("diagramViewport").hidden, true);
  const [diagramTab] = el.findAll("diagramTab");
  assert.equal(diagramTab.disabled, true);
});

test("an svg block is drawn in the same tick; malformed svg is an error", () => {
  const good = body("svg", { code: "<svg xmlns='http://www.w3.org/2000/svg'/>" });
  decorateDiagrams(good.container);
  const ok = card(good.container);
  assert.equal(ok.classList.contains("is-loading"), false);
  assert.equal(ok.classList.contains("is-error"), false);
  assert.equal(ok.find("diagramBar__type").hidden, true);

  const bad = body("svg", { code: "<broken" });
  decorateDiagrams(bad.container);
  const failed = card(bad.container);
  assert.equal(failed.classList.contains("is-error"), true);
  assert.match(failed.find("diagramCard__error").textContent, /not well-formed/);
});

test("the Code tab swaps the viewer for the source", async () => {
  const { container } = body("mermaid", { code: "flowchart LR\n X-->Y" });
  decorateDiagrams(container);
  await settle();
  const el = card(container);
  el.findAll("diagramTab").find((t) => t.dataset.view === "code").click();
  assert.equal(el.find("diagramSource").hidden, false);
  assert.equal(el.find("diagramViewport").hidden, true);
  assert.equal(el.find("diagramZoom").hidden, true);
});

// --- sanitizeSvg --------------------------------------------------------------

test("sanitizeSvg drops scripts, handlers and external references", () => {
  const script = xml("script");
  const foreign = xml("foreignObject");
  const rect = xml("rect", { width: "4", onclick: "x()", style: "fill:url(http://evil/x) red" });
  const image = xml("image", { href: "https://evil.example/x.png" });
  const inDoc = xml("use", { href: "#shape" });
  const dataImg = xml("image", { href: "data:image/png;base64,AAAA" });
  const anchor = xml("a", { href: "javascript:alert(1)" });
  const link = xml("a", { href: "https://example.com" });
  const animate = xml("animate", { attributeName: "href" });
  const style = xml("style");
  style.textContent = "@import url(http://evil/x.css); .a { fill: url(http://evil/p) }";
  const kids = [script, foreign, rect, image, inDoc, dataImg, anchor, link, animate, style];
  globalThis.nextRoot = () => xml("svg", { onload: "x()", viewBox: "0 0 4 4" }, kids);
  const out = sanitizeSvg("<svg/>");
  delete globalThis.nextRoot;

  assert.ok(script.removed && foreign.removed && animate.removed);
  assert.equal(rect.getAttribute("onclick"), null);
  assert.doesNotMatch(rect.getAttribute("style"), /evil/);
  assert.equal(image.getAttribute("href"), null);
  assert.equal(inDoc.getAttribute("href"), "#shape");
  assert.match(dataImg.getAttribute("href"), /^data:image\/png/);
  assert.equal(anchor.getAttribute("href"), null);
  assert.equal(link.getAttribute("href"), "https://example.com");
  assert.equal(link.getAttribute("rel"), "noreferrer noopener");
  assert.doesNotMatch(style.textContent, /@import|evil/);
  assert.doesNotMatch(out, /onload/);
  assert.match(out, /viewBox/);
});

test("sanitizeSvg refuses malformed input and a non-svg root", () => {
  assert.throws(() => sanitizeSvg("<broken"), /not well-formed/);
  globalThis.nextRoot = () => xml("html");
  assert.throws(() => sanitizeSvg("<html/>"), /not well-formed/);
  delete globalThis.nextRoot;
});
