import { test } from "node:test";
import assert from "node:assert/strict";
import { installFakeDom } from "./fake_dom.mjs";
import { createComposer } from "../web_chat/js/ui/composer.js";
import { createStore } from "../web_chat/js/lib/store.js";
import { createAudioRecorder } from "../web_chat/js/ui/audio-recorder.js";

function setup({ menu = false } = {}) {
  installFakeDom();
  Node.TEXT_NODE = 3;
  const documentEvents = {};
  document.addEventListener = (kind, listener) => (documentEvents[kind] ??= []).push(listener);
  const root = document.createElement("div");
  const nodes = Object.fromEntries(["input", "sendButton", "stopButton", "deliverySelect", "continueButton", "attachButton", "audioButton", "fileInput", "tray"].map(key => [key, document.createElement("div")]));
  nodes.input.closest = () => root;
  nodes.input.value = "";
  nodes.input.scrollHeight = 30;
  const label = document.createTextNode("Stop");
  label.nodeType = Node.TEXT_NODE;
  nodes.stopButton.append(label);
  const tools = document.createElement("details");
  const toggle = document.createElement("summary");
  tools.append(toggle);
  const store = createStore({ streaming: false });
  const ui = createComposer({ ...nodes, tools: menu ? tools : null, store, onSend() {}, onStop() {}, onContinue() {} });
  return { ...nodes, root, store, ui, tools, toggle, documentEvents };
}

test("empty composer has a microphone instead of an inactive Send", () => {
  const c = setup();
  assert.equal(c.sendButton.hidden, true);
  assert.equal(c.audioButton.hidden, false);
  assert.equal(c.audioButton.disabled, false);
  c.input.value = "a draft";
  c.ui.refresh();
  assert.equal(c.sendButton.hidden, false);
  assert.equal(c.audioButton.hidden, true);
  c.input.value = "";
  c.ui.refresh();
  assert.equal(c.sendButton.hidden, true);
  assert.equal(c.audioButton.hidden, false);
});

test("recording preserves draft and has one primary action, not Send plus mic", () => {
  const c = setup();
  c.input.value = "keep my draft";
  c.root.classList.add("is-recording");
  c.store.set({ resumable: true });
  assert.equal(c.sendButton.hidden, true);
  assert.equal(c.audioButton.hidden, false);
  assert.equal(c.continueButton.hidden, true);
  assert.equal(c.input.value, "keep my draft");
  c.root.classList.remove("is-recording");
  c.ui.refresh();
  assert.equal(c.sendButton.hidden, false);
  assert.equal(c.input.value, "keep my draft");
});

test("Stop stays reachable, recording is disabled during a turn or restart", () => {
  const c = setup();
  c.store.set({ streaming: true });
  assert.equal(c.stopButton.hidden, false);
  assert.equal(c.audioButton.disabled, true);
  assert.equal(c.deliverySelect.hidden, false);
  c.store.set({ stopping: true });
  assert.equal(c.stopButton.getAttribute("aria-label"), "Stop now");
  c.store.set({ streaming: false, restartPending: true });
  assert.equal(c.stopButton.hidden, true);
  assert.equal(c.deliverySelect.hidden, true);
  assert.equal(c.audioButton.disabled, true);
});

test("tools close with Escape, outside pointer, input focus and an action", () => {
  const c = setup({ menu: true });
  c.tools.open = true;
  let prevented = false;
  c.documentEvents.keydown[0]({ key: "Escape", preventDefault() { prevented = true; } });
  assert.equal(c.tools.open, false);
  assert.equal(c.toggle.focused, true);
  assert.equal(c.toggle.getAttribute("aria-expanded"), "false");
  assert.equal(prevented, true);
  c.tools.open = true;
  c.documentEvents.pointerdown[0]({ target: document.body });
  assert.equal(c.tools.open, false);
  c.tools.open = true;
  c.input.listeners.focus[0]();
  assert.equal(c.tools.open, false);
  c.tools.open = true;
  c.tools.listeners.click[0]({ target: { closest: () => ({}) } });
  assert.equal(c.tools.open, false);
});

test("selecting an option does not dismiss tools prematurely", () => {
  const c = setup({ menu: true });
  c.tools.open = true;
  c.tools.listeners.click[0]({ target: { closest: () => null } });
  assert.equal(c.tools.open, true);
  c.tools.listeners.toggle[0]();
  assert.equal(c.toggle.getAttribute("aria-expanded"), "true");
});

test("stopping while microphone permission is pending releases late tracks", async () => {
  let grant, stopped = 0, sent = 0;
  const recorder = createAudioRecorder({
    getUserMedia: () => new Promise(resolve => { grant = resolve; }),
    onSend: () => { sent++; },
  });
  const pending = recorder.start();
  await recorder.stop();
  grant({ getTracks: () => [{ stop() { stopped++; } }] });
  await pending;
  assert.equal(recorder.recording, false);
  assert.equal(stopped, 1);
  assert.equal(sent, 0);
});
