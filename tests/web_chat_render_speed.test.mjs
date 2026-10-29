/**
 * The frame budget of a streaming answer, and the lazy step contract.
 *
 * The message painter used to rebuild its whole body on every frame: at 42k
 * characters with three long code blocks that was tens of milliseconds a frame
 * (measured: 54 ms mean before, 0.9 ms after the block painter). These tests
 * pin both halves of that fix - a frame costs the tail it still renders, and a
 * closed fence is highlighted once, never again - plus the contract that a
 * lazy trace step is fetched when its card opens.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

import { renderMarkdownBlocks } from "../web_chat/js/lib/markdown.js";
import { installFakeDom } from "./fake_dom.mjs";

// The vendored highlight.js is plain UMD in the browser; require() it here and
// publish it as the global the adapter reads at call time.
const require_ = createRequire(import.meta.url);
globalThis.hljs ??= require_("../web_chat/js/lib/highlight.lib.js");
require_("../web_chat/js/lib/highlight.extra.js");

// Every call the adapter makes into the library, with what it was handed.
const calls = [];
const realHighlight = globalThis.hljs.highlight.bind(globalThis.hljs);
globalThis.hljs.highlight = (code, options) => {
  calls.push({ code, language: options?.language ?? null });
  return realHighlight(code, options);
};

const js = Array.from({ length: 215 }, (_, i) => `const value${i} = compute(${i}, "text ${i}") + old${i}; // line ${i}`).join("\n");
const py = Array.from({ length: 215 }, (_, i) => `def step_${i}(value):\n    return value * ${i} + ${i}  # step ${i}`).join("\n");
const css = Array.from({ length: 215 }, (_, i) => `.rule-${i} { color: #${((i * 7919) % 0xffffff).toString(16).padStart(6, "0")}; margin: ${i}px; }`).join("\n");
const prose = (n) => Array.from({ length: n }, (_, i) =>
  `Paragraph ${i}: the answer explains the change, names the file and the reason, **bold** and \`code ${i}\` and a [link](https://example.com/${i}).`).join("\n\n");

const ANSWER = [
  prose(6),
  "```js\n" + js + "\n```",
  prose(8),
  "```python\n" + py + "\n```",
  prose(8),
  "```css\n" + css + "\n```",
  prose(20),
].join("\n\n");

/** Stream *source* through the block painter, one frame per 40 characters. */
function stream(source, key) {
  const frames = [];
  for (let at = 0; at < source.length; at += 40) {
    const before = calls.length;
    const started = performance.now();
    const blocks = renderMarkdownBlocks(source.slice(0, at + 40), key);
    frames.push({ ms: performance.now() - started, highlights: calls.length - before, blocks: blocks.length });
  }
  return frames;
}

const mean = (values) => values.reduce((sum, value) => sum + value, 0) / values.length;
const percentile = (values, at) => [...values].sort((a, b) => a - b)[Math.floor(values.length * at)];

test("a 42k-character answer paints inside the frame budget", () => {
  assert.ok(ANSWER.length > 40_000, `the fixture is ${ANSWER.length} characters`);
  const frames = stream(ANSWER, "speed-test-budget");
  const times = frames.map((frame) => frame.ms);

  assert.ok(mean(times) < 5, `mean ${mean(times).toFixed(2)} ms per frame`);
  assert.ok(percentile(times, 0.95) < 15, `p95 ${percentile(times, 0.95).toFixed(2)} ms per frame`);
  // The last frames carry the trailing prose only: a closed answer is cheap.
  assert.ok(mean(frames.slice(-20).map((frame) => frame.ms)) < 5);
});

test("a closed fence is highlighted once and never again", () => {
  calls.length = 0;
  stream(ANSWER, "speed-test-fences");

  for (const [language, fence] of [["js", js], ["python", py], ["css", css]]) {
    const seen = calls.filter((call) => call.code === fence && call.language === language);
    assert.equal(seen.length, 1, `${language}: highlighted ${seen.length} times`);
  }
  // Nothing in the last frames re-highlights a closed block: the answer's tail
  // is plain prose and every fence above it is settled.
  const last = stream(ANSWER, "speed-test-fences").slice(-20);
  assert.equal(last.reduce((sum, frame) => sum + frame.highlights, 0), 0);

  // A repaint of the same answer reuses the cached markup of every fence.
  const before = calls.length;
  const blocks = renderMarkdownBlocks(ANSWER, "speed-test-fences");
  assert.ok(blocks.length >= 6, "the answer keeps its top-level blocks");
  assert.equal(calls.length - before, 0);
});

test("a lazy step's record is fetched only when its card opens", async () => {
  installFakeDom();
  const { createReasoningPanel } = await import("../web_chat/js/ui/reasoning.js");
  const asked = [];
  const full = {
    id: "call-1",
    kind: "tool",
    title: "Read the file",
    status: "done",
    result_payload: { version: 1, parts: [{ kind: "text", data: "file contents" }], raw_text: "file contents" },
  };
  const panel = createReasoningPanel();
  // The frame of a live step carries its identity and status alone.
  panel.upsert({ id: "call-1", kind: "tool", title: "Read the file", status: "running", refs: [], lazy: true });
  assert.equal(panel.el.find("payload__typed"), null);

  panel.setStepSource(() => (stepId) => {
    asked.push(stepId);
    return Promise.resolve(full);
  });
  // Nothing was asked while the card was closed.
  assert.deepEqual(asked, []);

  panel.el.find("step__head").click();
  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(asked, ["call-1"]);
  assert.ok(panel.el.find("payload__typed"), "the loaded record is shown");
  assert.match(panel.el.find("payload__typed").textContent, /file contents/);
});

test("a card opened before its source exists loads once the source arrives", async () => {
  installFakeDom();
  const { createReasoningPanel } = await import("../web_chat/js/ui/reasoning.js");
  const asked = [];
  const panel = createReasoningPanel();
  panel.upsert({ id: "call-2", kind: "tool", title: "Run the tests", status: "running", refs: [], lazy: true });
  panel.el.find("step__head").click();
  await Promise.resolve();
  // No source yet: the card waits instead of showing an empty record.
  assert.equal(panel.el.find("payload__typed"), null);

  panel.setStepSource(() => (stepId) => {
    asked.push(stepId);
    return Promise.resolve({ ...fullRecord("call-2"), result_payload: { version: 1, parts: [{ kind: "text", data: "all green" }] } });
  });
  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(asked, ["call-2"]);
  assert.match(panel.el.find("payload__typed").textContent, /all green/);
});

function fullRecord(id) {
  return { id, kind: "tool", title: "Run the tests", status: "done" };
}
