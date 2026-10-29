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
  // The sub-agent's block stays closed while it runs; the reader opens it.
  panel.el.findAll("step__head").at(-1).click();
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

// -- memory operations -------------------------------------------------------

const record = (over = {}) => ({
  id: "r1", layer: "L1", scope: "project", category: "decision", title: "", content: "Use SQLite for memory",
  tags: ["storage"], source_ref: "docs/adr.md", file_path: null, confidence: 1,
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-02T00:00:00Z", stale_status: "fresh", ...over,
});
const memoryView = (data, tool = "memory_recall") => renderPayload("Result", payload(json(data)), JSON.stringify(data), { tool });

test("recall hits become cards that name the layer, owner and score", () => {
  const el = memoryView([{ record: record({ title: "Storage choice" }), score: 0.87, level: "project" }]);
  assert.equal(el.find("payload__memList").childrenWith("payload__memCard").length, 1);
  const card = el.find("payload__memCard");
  assert.equal(card.dataset.layer, "L1");
  assert.equal(card.dataset.stale, "fresh");
  assert.equal(card.find("payload__memTitle").textContent, "Storage choice");
  const tags = card.findAll("payload__memTag").map((tag) => tag.textContent);
  assert.deepEqual(tags, ["L1", "decision", "Project", "FRESH"]);
  assert.equal(card.find("payload__memScore").textContent, "0.87");
  // Source and Copy survive the specialized view: the raw payload is the
  // typed raw_text, untouched by the card built on top of it.
  assert.equal(el.find("payload__raw").textContent, "source");
  assert.equal(el.findAll("payload__source").length, 2);
});

test("recall accepts bare records and says so when nothing matches", () => {
  const bare = memoryView([record()]);
  assert.equal(bare.find("payload__memTitle").textContent, "Use SQLite for memory");
  assert.equal(bare.find("payload__memScore"), null);
  assert.match(memoryView([]).find("payload__memNote").textContent, /No relevant memories found/);
});

test("recall arguments read as a sentence and keep the limit", () => {
  const el = renderPayload("Input", payload(json({ query: "auth tokens", limit: 10 })), "", { tool: "memory_recall" });
  assert.match(el.find("payload__memNote").textContent, /Search memory for auth tokens · up to 10 results/);
});

test("an empty recall query is a listing, not a search for anything", () => {
  for (const query of ["", "   "]) {
    const el = renderPayload("Input", payload(json({ query, limit: 20 })), "", { tool: "memory_recall" });
    const note = el.find("payload__memNote").textContent;
    assert.match(note, /^List memories · up to 20 results$/);
    assert.doesNotMatch(note, /anything|Search/);
  }
});

test("large recall answers show five cards and reveal the rest on demand", () => {
  const hits = Array.from({ length: 80 }, (_, index) => ({ record: record({ id: `r${index}`, content: `fact ${index}` }), score: 0.5 }));
  const el = memoryView(hits);
  assert.match(el.find("payload__memNote").textContent, /^80 related memories$/);
  assert.equal(el.findAll("payload__memCard").length, 5);
  const more = el.find("payload__memMore");
  assert.equal(more.textContent, "Show 45 more");
  more.click();
  assert.equal(el.findAll("payload__memCard").length, 50);
  assert.equal(el.find("payload__memMore"), null);
  // Records beyond the render bound are counted, never silently dropped.
  assert.match(el.find("payload__notice").textContent, /^30 more records · see source$/);
});

test("overview counts every record even past the card bound", () => {
  const records = Array.from({ length: 80 }, (_, index) => record({ id: `r${index}`, content: `fact ${index}` }));
  const el = memoryView({ records, limit: 100 }, "memory_overview");
  assert.match(el.find("payload__memNote").textContent, /^80 records$/);
  assert.equal(el.findAll("payload__memCard").length, 5);
  assert.equal(el.find("payload__memMore").textContent, "Show 45 more");
  assert.match(el.find("payload__notice").textContent, /^30 more records · see source$/);
  el.find("payload__memMore").click();
  assert.equal(el.findAll("payload__memCard").length, 50);
});

test("overview lists records and flags a reached limit; an empty memory says so", () => {
  const bounded = memoryView({ records: [record()], limit: 1 }, "memory_overview");
  assert.match(bounded.find("payload__memNote").textContent, /1 record · limit 1 reached/);
  const small = memoryView({ records: [record()], limit: 100 }, "memory_overview");
  assert.match(small.find("payload__memNote").textContent, /^1 record$/);
  assert.match(memoryView({ records: [], limit: 100 }, "memory_overview").find("payload__memNote").textContent, /Memory is empty/);
});

test("remember shows the record about to be saved and confirms a stored one", () => {
  const input = renderPayload("Input", payload(json({ content: "Prefer tabs", layer: "L3", category: "preference", scope: "user", level: "user" })), "", { tool: "memory_remember" });
  assert.match(input.find("payload__memNote").textContent, /Record to save · You · any project/);
  assert.equal(input.find("payload__memTitle").textContent, "Prefer tabs");

  const saved = memoryView({ ...record({ layer: "L3", category: "preference" }), level: "user" }, "memory_remember");
  assert.equal(saved.find("payload__memNote").dataset.tone, "ok");
  assert.match(saved.find("payload__memNote").textContent, /Saved · Profile · preference · You · any project/);

  // A result without an id confirms nothing: keep the plain JSON view instead
  // of repeating the input's "Record to save" under the Result label.
  const ambiguous = memoryView({ content: "Prefer tabs", layer: "L3", category: "preference" }, "memory_remember");
  assert.equal(ambiguous.find("payload__mem"), null);
  assert.match(ambiguous.find("payload__typed").textContent, /Prefer tabs/);
});

test("forget confirms a deletion, reports a miss and shows the id it will drop", () => {
  assert.match(memoryView({ forgotten: true }, "memory_forget").find("payload__memNote").textContent, /Deleted/);
  assert.match(memoryView({ forgotten: false }, "memory_forget").find("payload__memNote").textContent, /Nothing deleted/);
  const ask = renderPayload("Input", payload(json({ record_id: "abc123" })), "", { tool: "memory_forget" });
  assert.match(ask.find("payload__memNote").textContent, /Delete record abc123/);
  // The same shape as a result is not a deletion intent.
  assert.equal(memoryView({ record_id: "abc123" }, "memory_forget").find("payload__mem"), null);
});

test("memory errors are readable and any unknown shape keeps the JSON view", () => {
  const failed = memoryView({ error: "Memory operation unavailable or invalid arguments" });
  assert.equal(failed.find("payload__memNote").dataset.tone, "error");
  assert.match(failed.find("payload__memNote").textContent, /Memory error — Memory operation unavailable/);
  // Shapes the views do not know fall back instead of rendering an empty card.
  for (const [data, tool] of [[{ strange: 1 }, "memory_recall"], [{}, "memory_overview"], [{ content: 5 }, "memory_remember"], [null, "memory_forget"], [[{ nope: true }], "memory_recall"]]) {
    const el = renderPayload("Result", payload(json(data)), "", { tool });
    assert.equal(el.find("payload__mem"), null);
    assert.ok(el.find("payload__typed").textContent.length > 0);
  }
});

test("memory views never execute record text and legacy JSON strings get them too", () => {
  const evil = '<script>alert(1)</script>';
  const el = memoryView([{ record: record({ title: evil, content: evil }), score: 0.1 }]);
  assert.equal(el.querySelectorAll("script").length, 0);
  assert.match(el.find("payload__memTitle").textContent, /<script>/);
  // A chat stored before typed payloads has the same JSON as a plain string.
  const legacy = renderPayload("Result", null, JSON.stringify([{ record: record(), score: 0.2 }]), { tool: "memory_recall" });
  assert.equal(legacy.find("payload__memCard").dataset.layer, "L1");
  // Non-memory tools keep the generic JSON tree.
  assert.equal(renderPayload("Result", payload(json([record()])), "", { tool: "read_file" }).find("payload__mem"), null);
});

test("a long record body is previewed and expandable, ids and sources stay available", () => {
  const long = "word ".repeat(120);
  const el = memoryView([{ record: record({ content: long, title: "" }), score: 0.3 }]);
  const card = el.find("payload__memCard");
  // The title is the first line, shortened; the rest of that line is the body,
  // and only a body longer than the preview waits in details.
  assert.match(card.find("payload__memTitle").textContent, /…$/);
  assert.ok(card.find("payload__memTitle").textContent.length <= 81);
  const details = card.findAll("payload__memDetails");
  assert.equal(details.length, 2); // full text + identifiers
  const body = long.slice(80).trimStart();
  assert.equal(details[0].find("payload__memBody").textContent, body);
  assert.match(details[1].textContent, /id: r1/);
  assert.match(details[1].textContent, /source: docs\/adr.md/);
  assert.match(details[1].textContent, /tags: #storage/);
});

test("text past the shortened title stays readable without a title or details", () => {
  const content = "x".repeat(200);
  const card = memoryView([record({ title: "", content })]).find("payload__memCard");
  // 80 characters head the card; the remaining 120 are a real body, not lost.
  assert.equal(card.find("payload__memTitle").textContent, `${"x".repeat(80)}…`);
  assert.equal(card.find("payload__memBody").textContent, "x".repeat(120));
  // Only the identifiers remain collapsed; no "Full text" is needed.
  assert.equal(card.findAll("payload__memDetails").length, 1);
  assert.doesNotMatch(card.findAll("payload__memDetails")[0].textContent, /Full text/);
  const titled = memoryView([record({ title: "Tabs", content: "y".repeat(200) })]).find("payload__memCard");
  assert.equal(titled.find("payload__memBody").textContent, "y".repeat(200));
  assert.equal(titled.findAll("payload__memDetails").length, 1);
  assert.doesNotMatch(titled.findAll("payload__memDetails")[0].textContent, /Full text/);
});

test("scope never stands in for a missing memory level", () => {
  const card = memoryView([record({ scope: "project", level: undefined })]).find("payload__memCard");
  assert.deepEqual(card.findAll("payload__memTag").map((tag) => tag.textContent), ["L1", "decision", "FRESH"]);
  const system = memoryView([record({ scope: "system", level: undefined })]).find("payload__memCard");
  assert.deepEqual(system.findAll("payload__memTag").map((tag) => tag.textContent), ["L1", "decision", "FRESH"]);
  const owned = memoryView([{ record: record({ scope: "system" }), level: "user" }]).find("payload__memCard");
  assert.ok(owned.findAll("payload__memTag").some((tag) => tag.textContent === "You · any project"));
});

test("small relevance scores keep their precision and are not probabilities", () => {
  const card = memoryView([{ record: record(), score: 0.0164 }]).find("payload__memCard");
  const score = card.find("payload__memScore");
  assert.equal(score.textContent, "0.0164");
  assert.match(score.getAttribute("title"), /^Relevance score, not a probability$/);
  assert.equal(memoryView([{ record: record(), score: 0 }]).find("payload__memScore").textContent, "0");
  assert.equal(memoryView([{ record: record(), score: 0.87 }]).find("payload__memScore").textContent, "0.87");
});


test("an endless record title cannot bloat the card and keeps its text", () => {
  const title = "T".repeat(200);
  const card = memoryView([record({ title, content: "body" })]).find("payload__memCard");
  assert.equal(card.find("payload__memTitle").textContent, `${"T".repeat(80)}…`);
  // The rest of the title is readable, not dropped, and the record's own text follows.
  assert.equal(card.find("payload__memBody").textContent, `${"T".repeat(120)} body`);
  // Short enough for the preview, so only the identifiers stay folded.
  const details = card.findAll("payload__memDetails");
  assert.equal(details.length, 1);
  assert.match(details[0].textContent, /^Details/);
});

test("overview does not call a memory empty when its records are unreadable", () => {
  const el = memoryView({ records: [{ nope: true }, "text", 5], limit: 10 }, "memory_overview");
  // Entries that are not records fall back to the JSON view; only an empty
  // answer is a statement about the memory itself.
  assert.equal(el.find("payload__mem"), null);
  assert.match(el.find("payload__typed").textContent, /nope/);
  assert.match(memoryView({ records: [], limit: 10 }, "memory_overview").find("payload__memNote").textContent, /Memory is empty/);
});
