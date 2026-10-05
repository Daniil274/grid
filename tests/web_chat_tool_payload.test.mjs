import { test } from "node:test";
import assert from "node:assert/strict";
import { installFakeDom } from "./fake_dom.mjs";
import { renderPayload } from "../web_chat/js/ui/tool-payload.js";
import { languageFor } from "../web_chat/js/lib/languages.js";
import { createReasoningPanel } from "../web_chat/js/ui/reasoning.js";

installFakeDom();
const payload = (...parts) => ({ version: 1, parts, raw_text: "source" });
const json = (data) => ({ kind: "json", media_type: "application/json", data });

test("JSON retains objects, arrays, scalar strings, false, zero, null and empty values", () => {
  for (const data of [{ a: false, b: 0, c: null, d: "" }, [], {}, null, false, 0, "hello", '{"not":"object"}']) {
    const el = renderPayload("Result", payload(json(data)));
    assert.ok(el.find("payload__typed"));
    assert.equal(el.find("payload__typed").textContent.includes("[object Object]"), false);
    if (data === null || typeof data !== "object") assert.ok(el.find("payload__typed").textContent.includes(JSON.stringify(data)));
  }
});

test("homogeneous scalar records use a table, mixed and nested data use fields", () => {
  const table = renderPayload("Result", payload(json([{ name: "a", ok: true }, { name: "b", ok: false }])));
  assert.ok(table.find("payload__table"));
  assert.equal(table.querySelectorAll("th").length, 2);
  const tree = renderPayload("Result", payload(json({ nested: { a: 1 } })));
  assert.ok(tree.find("payload__branch"));
  assert.equal(renderPayload("Result", payload(json([{ x: 1 }, { y: 2 }]))).find("payload__table"), null);
});

test("JSON and unknown content use text nodes, code and Markdown escape scripts", () => {
  const evil = '<script>alert(1)</script><img onerror="evil()">';
  const el = renderPayload("Result", payload(json({ evil }), { kind: "future", data: evil }));
  assert.equal(el.querySelectorAll("script").length, 0);
  assert.ok(el.find("payload__typed").textContent.includes(evil));
  for (const kind of ["code", "markdown", "diff"]) {
    const view = renderPayload("Result", payload({ kind, data: evil }));
    const body = kind === "markdown" ? view.find("payload__body--prose") : view.querySelector("code");
    assert.ok(body.innerHTML.includes("&lt;script&gt;"));
    assert.equal(body.innerHTML.includes("<script>"), false);
  }
});

test("diff uses existing addition/removal highlighter", () => {
  const el = renderPayload("Result", payload({ kind: "diff", data: "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new" }));
  const html = el.querySelector("code").innerHTML;
  assert.match(html, /dl--add/);
  assert.match(html, /dl--del/);
});

test("images only auto-load bounded raster data or safe workspace files", () => {
  for (const data of ["data:image/png;base64,YWJj", "/api/workspace/files/picture.png"]) {
    const el = renderPayload("Result", payload({ kind: "image", data }));
    assert.ok(el.find("payload__image"));
  }
  for (const data of ["https://example.test/track.png?secret=x", "javascript:alert(1)", "data:image/svg+xml;base64,YWJj", "/api/workspace/files/../secret.png", "/api/workspace/files/%2e%2e/a.png", "//evil.test/a.png"]) {
    const el = renderPayload("Result", payload({ kind: "image", data }));
    assert.equal(el.find("payload__image"), null);
    const link = el.find("payload__resource");
    if (data.startsWith("https://")) assert.equal(link.tagName, "A");
    else assert.equal(link.tagName, "SPAN");
  }
});

test("resources link only safe HTTP and workspace URLs; MCP URIs remain visible", () => {
  for (const uri of ["javascript:alert(1)", "file:///src/x", "mcp://thing", "https://user:pass@example.test/a"]) {
    const el = renderPayload("Result", payload({ kind: "resource", uri, name: "artifact" }));
    assert.equal(el.find("payload__resource").tagName, "SPAN");
  }
  const el = renderPayload("Result", payload({ kind: "resource", uri: "/api/workspace/files/report.pdf" }));
  assert.equal(el.find("payload__resource").tagName, "A");
});

test("source toggle works without DOM selectors and persists across updates", () => {
  const state = {};
  let el = renderPayload("Result", payload(json({ x: 1 })), "", { state });
  assert.equal(el.find("payload__raw").hidden, true);
  el.find("payload__source").click();
  assert.equal(el.find("payload__typed").hidden, true);
  assert.equal(el.find("payload__raw").hidden, false);
  el = renderPayload("Result", payload(json({ x: 2 })), "", { state });
  assert.equal(el.find("payload__raw").hidden, false);
});

test("copy uses source rather than rendered text", async () => {
  let copied;
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { clipboard: { writeText: async (value) => { copied = value; } } } });
  const el = renderPayload("Input", { ...payload(json({ x: 1 })), raw_text: '{"x":1}' });
  el.findAll("payload__source")[1].click();
  await Promise.resolve();
  assert.equal(copied, '{"x":1}');
});

test("empty typed results differ from absent results and malformed types fall back", () => {
  assert.equal(renderPayload("Result", null, ""), null);
  assert.match(renderPayload("Result", payload()).textContent, /No content returned/);
  assert.ok(renderPayload("Result", { version: 9 }, "legacy"));
  assert.ok(renderPayload("Result", payload(null)));
  assert.ok(renderPayload("Input", null, '{"a":1}').find("payload__fields"));
});

test("long payloads are visibly bounded and mixed parts are retained", () => {
  const el = renderPayload("Result", { ...payload({ kind: "text", data: "x".repeat(40000) }, json({ x: 1 })), raw_text: "x".repeat(40000), original_size: 40000 });
  assert.match(el.find("payload__notice").textContent, /Truncated/);
  assert.equal(el.findAll("payload__part").length, 2);
  assert.equal(el.find("payload__raw").textContent.length, 32000);
});

test("normal tool and nested agent payloads are lazy, typed and source state survives upsert", () => {
  const panel = createReasoningPanel();
  const step = { id: "tool", kind: "tool", title: "Tool", status: "done", refs: [], result_payload: payload(json({ x: 1 })) };
  panel.upsert(step);
  assert.equal(panel.el.find("payload__typed"), null);
  panel.el.find("step__head").click();
  panel.el.find("payload__source").click();
  panel.upsert({ ...step, result_payload: payload(json({ x: 2 })) });
  assert.equal(panel.el.find("payload__raw").hidden, false);
  panel.upsert({ id: "agent", kind: "agent", title: "Coder", status: "running", input_payload: payload(json({ input: "task" })), refs: [] });
  assert.ok(panel.el.find("agent__task").find("payload__typed"));
  panel.finish(1);
});

test("code parts take the language of their file name when the payload omits it", () => {
  const el = renderPayload("Result", payload({ kind: "code", name: "src/app.py", data: "x = 1" }));
  assert.equal(el.find("payload__code").dataset.language, "python");

  // An explicit label wins, and names the highlighter cannot serve stay plain.
  const labelled = renderPayload("Result", payload({ kind: "code", name: "a.go", language: "python", data: 'x = "hi"  # note' }));
  assert.equal(labelled.find("payload__code").dataset.language, "python");
  const unknown = renderPayload("Result", payload({ kind: "code", name: "a.weird", data: "x" }));
  assert.equal(unknown.find("payload__code").dataset.language, "text");

  for (const [name, language] of [["Dockerfile", "dockerfile"], ["a.ts", "typescript"], ["build.nix", "nix"], ["robots.txt", ""]]) {
    if (!language) continue;
    assert.equal(languageFor({ name }), language);
  }
  assert.equal(languageFor({ name: "settings.toml" }), "ini");
  assert.equal(languageFor({ name: "x.tsx" }), "typescript");
  assert.equal(languageFor({ language: "text" }), "");
});

test("diffs show the +/- line counts and replacement halves get their own tone", () => {
  const diff = renderPayload("Result", payload({ kind: "diff", data: "--- a\n+++ b\n@@\n-one\n+two\n+three\n ctx" }));
  assert.equal(diff.find("payload__stat--add").textContent, "+2");
  assert.equal(diff.find("payload__stat--del").textContent, "−1");
  assert.equal(diff.find("payload__type").textContent, "diff");

  const edit = renderPayload("Result", payload(
    { kind: "code", name: "old_text", data: "old" },
    { kind: "code", name: "new_text", data: "new" },
  ), { tool: "replace_in_file" });
  const parts = edit.findAll("payload__part");
  assert.equal(parts[0].dataset.tone, "del");
  assert.equal(parts[1].dataset.tone, "add");
});

test("the payload card carries the tool family and the error flag for colouring", () => {
  const root = renderPayload("Result", payload({ kind: "code", name: "a.py", data: "x" }), "body", { tool: "read_file" });
  assert.equal(root.dataset.family, "read");
  const edit = renderPayload("Result", payload({ kind: "text", data: "done" }), "", { tool: "some_server.file_edit" });
  assert.equal(edit.dataset.family, "edit");
  const shell = renderPayload("Result", payload({ kind: "text", data: "ok" }), "", { tool: "bash_tool" });
  assert.equal(shell.dataset.family, "shell");
  const failed = renderPayload("Result", { ...payload({ kind: "text", data: "boom" }), is_error: true }, "", { tool: "read_file" });
  assert.equal(failed.dataset.error, "true");
});

test("stderr parts render as an error stream and commands get a shell prompt", () => {
  const el = renderPayload("Result", payload({ kind: "code", name: "command", language: "bash", data: "ls" }, { kind: "code", name: "stderr", data: "boom" }));
  const parts = el.findAll("payload__part");
  assert.equal(parts[0].dataset.tone, "shell");
  assert.equal(parts[1].dataset.tone, "error");
});
