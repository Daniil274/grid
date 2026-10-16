import { test } from "node:test";
import assert from "node:assert/strict";
import { installFakeDom } from "./fake_dom.mjs";

installFakeDom();

const { renderMarkdown } = await import("../web_chat/js/lib/markdown.js");
const { decorateMath } = await import("../web_chat/js/lib/math.js");

const formulas = (html) => [...html.matchAll(/<(span|div) class="math( math--display)?">([\s\S]*?)<\/\1>/g)]
  .map(([, tag, display, tex]) => ({ tag, display: Boolean(display), tex }));

test("inline formulas keep their TeX whole: no emphasis inside", () => {
  const html = renderMarkdown("Пусть \\(a_i * b_j\\) и \\(C_j\\) - ёмкость; *важно*.");
  assert.deepEqual(formulas(html), [
    { tag: "span", display: false, tex: "a_i * b_j" },
    { tag: "span", display: false, tex: "C_j" },
  ]);
  assert.ok(html.includes("<em>важно</em>"));
});

test("a display formula over several lines is one block", () => {
  const source = [
    "Доступный бюджет:",
    "",
    "\\[",
    "H_j =",
    "\\max\\left(",
    "0,\\;",
    "\\min\\left[",
    "\\alpha_j C_j-A_j-R_j,\\;",
    "C_j-A_j-R_j-U_j^+-M_j",
    "\\right]",
    "\\right)",
    "\\]",
    "",
    "Действие допускается, только если:",
  ].join("\n");
  const html = renderMarkdown(source);
  const [formula, ...rest] = formulas(html);
  assert.equal(rest.length, 0);
  assert.equal(formula.tag, "div");
  assert.ok(formula.display);
  assert.ok(formula.tex.startsWith("H_j =\n\\max\\left("));
  assert.ok(formula.tex.endsWith("\\right]\n\\right)"));
  assert.ok(html.includes("<p>Действие допускается, только если:</p>"));
});

test("$$ blocks, one-line blocks and $ $ inline", () => {
  assert.deepEqual(formulas(renderMarkdown("$$\nE = mc^2\n$$")), [{ tag: "div", display: true, tex: "E = mc^2" }]);
  assert.deepEqual(formulas(renderMarkdown("\\[ x^2 \\]")), [{ tag: "div", display: true, tex: "x^2" }]);
  assert.deepEqual(formulas(renderMarkdown("площадь $\\pi r^2$, где $r$ - радиус")), [
    { tag: "span", display: false, tex: "\\pi r^2" },
    { tag: "span", display: false, tex: "r" },
  ]);
  // A closing delimiter inside the line: a display formula within the paragraph.
  assert.deepEqual(formulas(renderMarkdown("$$x$$ is the value")), [{ tag: "span", display: true, tex: "x" }]);
});

test("dollar signs that are not formulas stay text", () => {
  for (const text of ["from $5 to $10", "$5-$10", "echo $HOME and $PATH", "US$5 and US$6"]) {
    assert.deepEqual(formulas(renderMarkdown(text)), [], text);
  }
});

test("a formula still being written, or cut by a blank line, stays text", () => {
  assert.deepEqual(formulas(renderMarkdown("\\[\nH_j =\n\\max")), []);
  assert.deepEqual(formulas(renderMarkdown("$$\nx\n\ny\n$$")), []);
  assert.deepEqual(formulas(renderMarkdown("начало \\(C_j")), []);
});

test("code is never read as a formula, and TeX cannot inject markup", () => {
  assert.deepEqual(formulas(renderMarkdown("`$x$` and\n```\n\\[ y \\]\n```")), []);
  const html = renderMarkdown("\\(a < b <script>\\)");
  assert.deepEqual(formulas(html), [{ tag: "span", display: false, tex: "a &lt; b &lt;script&gt;" }]);
  assert.ok(!html.includes("<script>"));
});

// --- typesetting -----------------------------------------------------------

function container(...sources) {
  const root = document.createElement("div");
  for (const [tex, display] of sources) {
    const node = document.createElement(display ? "div" : "span");
    node.setAttribute("class", display ? "math math--display" : "math");
    node.textContent = tex;
    root.append(node);
  }
  return root;
}

test("formulas are typeset once each, from the cache on repaints", () => {
  const calls = [];
  globalThis.katex = {
    renderToString(tex, options) {
      calls.push([tex, options.displayMode, options.trust]);
      if (tex === "\\bad{") throw new Error("parse error");
      return `<k>${tex}</k>`;
    },
  };
  try {
    const first = container(["C_j", false], ["H_j = 1", true], ["\\bad{", false]);
    decorateMath(first);
    const [inline, display, bad] = first.querySelectorAll(".math");
    assert.equal(inline.innerHTML, "<k>C_j</k>");
    assert.equal(display.innerHTML, "<k>H_j = 1</k>");
    assert.equal(bad.textContent, "\\bad{");
    assert.ok(bad.classList.contains("math--source"));
    assert.deepEqual(calls, [["C_j", false, false], ["H_j = 1", true, false], ["\\bad{", false, false]]);

    // The next streaming frame: the same formulas, new elements, no new work.
    const repaint = container(["C_j", false], ["H_j = 1", true]);
    decorateMath(repaint);
    assert.equal(calls.length, 3);
    assert.equal(repaint.querySelectorAll(".math")[0].innerHTML, "<k>C_j</k>");
    // Inline and display are typeset apart.
    decorateMath(container(["C_j", true]));
    assert.deepEqual(calls.at(-1), ["C_j", true, false]);
  } finally {
    delete globalThis.katex;
  }
});
