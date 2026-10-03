import { test } from "node:test";
import assert from "node:assert/strict";
import { installFakeDom } from "./fake_dom.mjs";
import { createStore } from "../web_chat/js/lib/store.js";
import { createComposer } from "../web_chat/js/ui/composer.js";
import { fileSize, withFiles } from "../web_chat/js/lib/files.js";
import { renderMarkdown } from "../web_chat/js/lib/markdown.js";

installFakeDom();
Node.TEXT_NODE = 3;
const tick = () => new Promise((resolve) => setImmediate(resolve));

function composer(uploads = { enabled: true, max_file_mb: 25, max_files: 10 }) {
  const root = document.createElement("div");
  const nodes = Object.fromEntries(["input", "sendButton", "stopButton", "deliverySelect", "continueButton",
    "attachButton", "fileInput", "tray"].map((key) => [key, document.createElement("div")]));
  nodes.input.closest = () => root;
  nodes.input.value = "";
  nodes.input.scrollHeight = 30;
  const label = document.createTextNode("Stop");
  label.nodeType = Node.TEXT_NODE;
  nodes.stopButton.append(label);
  const sent = [];
  const store = createStore({ streaming: false, uploads });
  createComposer({ ...nodes, store,
    onSend: (...args) => sent.push(args), onStop: () => {}, onContinue: () => {} });
  const drop = (files) => root.listeners.drop[0]({ dataTransfer: { files }, preventDefault() {} });
  return { ...nodes, sent, drop, store };
}

for (const blockingState of ["restartPending", "compacting"]) test(`${blockingState} preserves the unsent draft and blocks sending until ready`, () => {
  const ui = composer();
  ui.input.value = "keep this draft";
  ui.store.set({ [blockingState]: true, resumable: true });
  assert.equal(ui.sendButton.disabled, true);
  assert.equal(ui.continueButton.disabled, true);
  ui.sendButton.click();
  assert.equal(ui.input.value, "keep this draft");
  assert.deepEqual(ui.sent, []);
  ui.store.set({ [blockingState]: false });
  assert.equal(ui.sendButton.disabled, false);
  ui.sendButton.click();
  assert.equal(ui.sent[0][0], "keep this draft");
});

test("uploaded files wait for the server and use its actual name and path in the message", async () => {
  const originalFetch = globalThis.fetch;
  let finish;
  let request;
  globalThis.fetch = (url, options) => {
    request = { url, options };
    return new Promise((resolve) => { finish = resolve; });
  };
  try {
    const ui = composer();
    ui.drop([new File(["data"], "report.csv", { type: "text/csv" })]);
    assert.equal(request.url, "/api/workspace/uploads");
    assert.equal(request.options.body.get("files").name, "report.csv");
    assert.equal(request.options.headers, undefined); // browser adds multipart boundary
    assert.equal(ui.sendButton.disabled, true);
    assert.match(ui.tray.textContent, /Preparing/);
    const uploaded = { name: "report (2).csv", path: "uploads/report (2).csv", bytes: 4,
      url: "/api/workspace/files/uploads/report%20%282%29.csv" };
    finish({ ok: true, status: 200, json: async () => ({ files: [uploaded] }) });
    await tick();
    assert.equal(ui.sendButton.disabled, false);
    assert.match(ui.tray.textContent, /report \(2\)\.csv/);
    ui.sendButton.click();
    assert.equal(ui.sent[0][0], withFiles("", [uploaded]));
    assert.deepEqual(ui.sent[0][1], []);
    assert.equal(ui.tray.hidden, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("removing a pending upload keeps it out of the next message", async () => {
  const originalFetch = globalThis.fetch;
  let finish;
  globalThis.fetch = () => new Promise((resolve) => { finish = resolve; });
  try {
    const ui = composer();
    ui.drop([new File(["data"], "a.txt")]);
    ui.tray.find("attachment__remove").click();
    finish({ ok: true, status: 200, json: async () => ({ files: [{ name: "a.txt", path: "uploads/a.txt", bytes: 4,
      url: "/api/workspace/files/uploads/a.txt" }] }) });
    await tick();
    assert.equal(ui.tray.hidden, true);
    ui.input.value = "hello";
    ui.sendButton.click();
    assert.equal(ui.sent[0][0], "hello");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("links survive saved message rendering and cannot inject HTML", () => {
  const text = withFiles("Read this", [{ name: 'данные [1] <img src=x>.csv', path: 'uploads/данные [1] <img src=x>.csv',
    url: "/api/workspace/files/uploads/%D0%B4%20%5B1%5D%20%3Cimg%20src%3Dx%3E.csv" }]);
  const html = renderMarkdown(text);
  assert.ok(html.includes('href="/api/workspace/files/uploads/'));
  assert.ok(!html.includes("<img"));
  assert.ok(text.includes('workspace path: "uploads/данные [1] <img src=x>.csv"'));
  assert.equal(withFiles("hello", []), "hello");
  assert.equal(fileSize(12), "12 B");
  assert.equal(fileSize(1024), "1.0 KB");
});
