/**
 * Manual and automatic context compaction in the chat: what the server's
 * answer carries, what the transcript shows and when, and how a failure is
 * told to the reader. The API and the websocket are fakes - this is the
 * UI's behaviour, not the server's; the DOM is the shared fake
 * (tests/fake_dom.mjs). The server's own routes are tests/test_web_compact.py.
 */

import test from "node:test";
import assert from "node:assert/strict";

import { ChatController } from "../web_chat/js/chat.js";
import { createStore } from "../web_chat/js/lib/store.js";
import { api } from "../web_chat/js/net/api.js";
import { installFakeDom } from "./fake_dom.mjs";
import { createMessage } from "../web_chat/js/ui/message.js";

installFakeDom();

// -- the page: visibility, pageshow, online ---------------------------------
const listen = () => {
  const listeners = new Map();
  return (type, fn) => {
    if (!listeners.has(type)) listeners.set(type, []);
    listeners.get(type).push(fn);
  };
};

document.addEventListener = listen();
document.removeEventListener = () => {};
document.dispatchEvent = () => true;
document.visibilityState = "visible";
globalThis.window = { addEventListener: listen(), removeEventListener: () => {}, dispatchEvent: () => true };

// -- the socket --------------------------------------------------------------
class FakeSocket {
  static CONNECTING = 0;

  static OPEN = 1;

  static CLOSING = 2;

  static CLOSED = 3;

  static instances = [];

  constructor(url) {
    this.url = url;
    this.readyState = FakeSocket.CONNECTING;
    this.sent = [];
    this.listeners = new Map();
    FakeSocket.instances.push(this);
    queueMicrotask(() => {
      if (this.readyState !== FakeSocket.CONNECTING) return;
      this.readyState = FakeSocket.OPEN;
      this.emit("open", {});
    });
  }

  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }

  emit(type, event = {}) {
    for (const fn of [...(this.listeners.get(type) ?? [])]) fn({ type, ...event });
  }

  send(raw) {
    this.sent.push(JSON.parse(raw));
  }

  close() {
    if (this.readyState === FakeSocket.CLOSED) return;
    this.readyState = FakeSocket.CLOSED;
    this.emit("close", {});
  }

  deliver(payload) {
    this.emit("message", { data: JSON.stringify(payload) });
  }
}

globalThis.WebSocket = FakeSocket;
globalThis.location = { protocol: "http:", host: "chat.test" };
globalThis.requestAnimationFrame = (fn) => {
  const handle = setTimeout(() => fn(Date.now()), 1);
  handle.unref?.();
  return handle;
};
globalThis.cancelAnimationFrame = (handle) => clearTimeout(handle);

const { createTranscript } = await import("../web_chat/js/ui/transcript.js");

// -- helpers -----------------------------------------------------------------
const lastSocket = () => FakeSocket.instances.at(-1);
const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));
const userMessage = (id, text) => ({ id, role: "user", content: text, timestamp: 1 });
const compactionMarker = (id, before, after) => ({
  id,
  role: "assistant",
  kind: "compaction",
  compaction: { tokens_before: before, tokens_after: after },
  timestamp: 2,
});
const payloadFor = (id, state) => ({
  id,
  root: id,
  messages: state.messages,
  metadata: {},
  pending: [],
  active_turn: state.active_turn ?? null,
});

/**
 * A chat on screen, open and rendered. `state` is what the server answers:
 * the messages stored so far and the turn it is still running, if any.
 */
async function openChat(state) {
  FakeSocket.instances.length = 0;
  const store = createStore({ contextId: null, rootId: null, systems: [], conversations: [], queue: [] });
  const container = document.createElement("div");
  // The shared fake DOM has no insertBefore; the transcript places a
  // compaction marker before the turn's live bubbles with it.
  container.insertBefore = (node, anchor) => {
    const siblings = container.childNodes;
    const index = siblings.indexOf(anchor);
    node.remove();
    node.parentNode = container;
    if (index >= 0) siblings.splice(index, 0, node);
    else siblings.push(node);
    return node;
  };
  const transcript = createTranscript({
    container,
    emptyState: document.createElement("div"),
    jumpButton: document.createElement("button"),
  });
  const chat = new ChatController({
    store,
    transcript,
    onConversationsChanged: async () => {},
    speech: { stop() {}, speak() {}, isActive: () => false, activeId: null },
  });
  api.getConversation = async (id, signal) =>
    ({ id, root: id, messages: state.messages, metadata: {}, pending: [], active_turn: state.active_turn ?? null });
  api.conversationPlan = async () => ({ tasks: [] });
  api.createConversation = async () => ({ id: "c-new", workspace: null });
  await chat.openConversation("c1");
  return { chat, store, transcript, container, state };
}

// -- the button: what a manual compaction reserves ---------------------------
test("manual compaction reserves the UI and blocks duplicate calls and sending", async () => {
  const original = api.compactConversation;
  let complete;
  const calls = [];
  api.compactConversation = (id) => { calls.push(id); return new Promise(resolve => { complete = resolve; }); };
  try {
    const store = createStore({ contextId: "ctx", streaming: false, resumable: true });
    const chat = new ChatController({ store });
    const pending = chat.compactContext();
    assert.equal(store.get().compacting, true);
    await assert.rejects(chat.compactContext(), /already in progress/);
    await chat.send("must not begin a turn");
    await chat.continueTurn();
    assert.equal(store.get().resumable, true);
    assert.deepEqual(calls, ["ctx"]);
    complete({ tokens_before: 12000, tokens_after: 800 });
    assert.deepEqual(await pending, { tokens_before: 12000, tokens_after: 800 });
    assert.equal(store.get().compacting, false);
  } finally { api.compactConversation = original; }
});

test("compaction failures unlock the UI and allow retry", async () => {
  const original = api.compactConversation;
  api.compactConversation = async () => { throw new Error("summary failed"); };
  try {
    const store = createStore({ contextId: "ctx", streaming: false });
    const chat = new ChatController({ store });
    await assert.rejects(chat.compactContext(), /summary failed/);
    assert.equal(store.get().compacting, false);
    api.compactConversation = async () => ({ tokens_before: 100, tokens_after: 10 });
    assert.equal((await chat.compactContext()).tokens_after, 10);
    assert.equal(store.get().compacting, false);
  } finally { api.compactConversation = original; }
});

test("compaction refuses an active turn, empty chat, or server restart before calling the API", async () => {
  const original = api.compactConversation;
  api.compactConversation = () => { assert.fail("must not call API"); };
  try {
    for (const state of [{}, { contextId: "ctx", streaming: true }, { contextId: "ctx", restartPending: true }]) {
      await assert.rejects(new ChatController({ store: createStore(state) }).compactContext());
    }
  } finally { api.compactConversation = original; }
});

// -- the marker a manual compaction draws ------------------------------------
test("a manual compaction shows its marker at once, before any read returns", async () => {
  const { chat, store, container, state } = await openChat({ messages: [userMessage("m1", "hi")], active_turn: null });
  // Every read the compact schedules hangs: only the answer could draw it.
  let answer = null;
  let reads = 0;
  api.getConversation = () => new Promise((resolve) => { reads += 1; answer = resolve; });
  const outcome = {
    tokens_before: 12000,
    tokens_after: 800,
    compacted: { tokens_before: 12000, tokens_after: 800, timestamp: "2026-01-02T03:04:05Z" },
  };
  api.compactConversation = async () => outcome;

  const compacting = chat.compactContext();
  assert.equal(store.get().compacting, true, "the button is reserved while the request runs");
  await tick(2);

  assert.equal(reads, 1, "the read the compact schedules is still in flight");
  assert.equal(container.findAll("msg--compaction").length, 1, "the marker is drawn from the answer itself");
  assert.match(container.find("msg__marker").textContent, /Context compacted/);
  assert.match(container.find("msg__marker").textContent, /~12k → ~800 tokens/);

  // The read answers with the thread's own copy of the marker - the server
  // wrote it beside the visible conversation - and the screen keeps one.
  state.messages.push(compactionMarker("mk1", 12000, 800));
  answer(payloadFor("c1", state));
  assert.deepEqual(await compacting, outcome);
  assert.equal(container.findAll("msg--compaction").length, 1, "the thread's copy, not a second marker");
  assert.equal(store.get().compacting, false);
});

test("a newer read cannot leave the manual compaction's marker unseen", async () => {
  const { chat, container, state } = await openChat({ messages: [userMessage("m1", "hi")], active_turn: null });
  const reads = [];
  api.getConversation = () => new Promise((resolve) => { reads.push(resolve); });
  api.compactConversation = async () => ({
    tokens_before: 12000,
    tokens_after: 800,
    compacted: { tokens_before: 12000, tokens_after: 800, timestamp: "2026-01-02T03:04:05Z" },
  });
  state.messages.push(compactionMarker("mk1", 12000, 800));

  const compacting = chat.compactContext();
  await tick(2);
  assert.equal(container.findAll("msg--compaction").length, 1, "drawn before any read could answer");

  // A concurrent read - a resync, a reconcile - claims the screen after the
  // compact's own read (`_claimRead`/`_ownsRead`): the compact's read is dropped.
  const superseding = chat._reconcile("c1");
  await tick();
  assert.equal(reads.length, 2, "the compact's read and the newer one");

  reads[1](payloadFor("c1", state)); // the newer read settles the screen
  await superseding;
  reads[0](payloadFor("c1", state)); // the compact's read owns nothing now
  assert.equal((await compacting).tokens_after, 800);
  assert.equal(container.findAll("msg--compaction").length, 1, "shown once, not duplicated by the dropped read");
});

// -- the marker around a failed turn -----------------------------------------
test("a compaction written before a failed turn still reaches the screen", async () => {
  const { chat, transcript, container, state } = await openChat({ messages: [userMessage("m1", "hi")], active_turn: null });

  await chat.send("hello");
  const failed = transcript.last();
  // The session was compacted before the turn started; then the turn failed.
  state.messages.push(compactionMarker("mk1", 9000, 500));
  lastSocket().deliver({ type: "error", content: "the model is unreachable" });
  lastSocket().deliver({ type: "done", duration_ms: 10, stopped: false });
  await tick(4);

  assert.equal(chat.isStreaming, false);
  const marker = container.find("msg--compaction");
  assert.ok(marker, "the compaction marker shows despite the failed turn");
  assert.match(container.find("msg__marker").textContent, /~9k → ~500 tokens/);
  // A failed turn is not reconciled - a full render would leave only the two
  // messages the server stored - and the marker is inserted in place, before
  // the turn's own bubbles, which stay exactly where they were.
  const order = container.querySelectorAll(".msg");
  assert.equal(order.length, 4, "the stored message, the marker, the request and the failed answer");
  assert.ok(order.indexOf(marker) < order.indexOf(failed.el), "the marker sits before the failed answer");
  assert.equal(order.at(-1), failed.el, "the error is not wiped off the screen");
  assert.match(container.textContent, /model is unreachable/);
});

// -- a failure the reader can see --------------------------------------------
test("a failed manual compaction is announced in the chat and marked as told", async () => {
  const original = api.compactConversation;
  api.compactConversation = async () => { throw new Error("the summary model refused"); };
  try {
    const store = createStore({ contextId: "ctx", streaming: false });
    const chat = new ChatController({ store });
    const error = await chat.compactContext().then(() => null, (caught) => caught);

    assert.ok(error, "the failure reaches the caller");
    assert.equal(error.notified, true, "the caller knows the reader was already told");
    const notices = document.body.findAll("toast")
      .filter((node) => node.textContent.includes("the summary model refused"));
    assert.equal(notices.length, 1, "one error toast in the chat, not one per caller");
    assert.equal(notices[0].dataset.tone, "error");
    assert.equal(store.get().compacting, false, "the UI unlocks for a retry");
  } finally { api.compactConversation = original; }
});

// -- the marker itself --------------------------------------------------------
test("a compaction marker shows where the context was rewritten", () => {
  const message = createMessage({
    role: "assistant",
    kind: "compaction",
    timestamp: "2026-01-02T03:04:05Z",
    compaction: { tokens_before: 12000, tokens_after: 800 },
  });

  assert.equal(message.role, "marker");
  assert.ok(message.el.classList.contains("msg--compaction"));
  const label = message.el.find("msg__marker");
  assert.ok(label, "expected a marker label");
  assert.match(label.textContent, /Context compacted/);
  assert.match(label.textContent, /~12k → ~800 tokens/);
});

test("a compaction marker without counts omits the token label", () => {
  const message = createMessage({ role: "assistant", kind: "compaction" });

  const label = message.el.find("msg__marker");
  assert.match(label.textContent, /Context compacted/);
  assert.doesNotMatch(label.textContent, /tokens/);
});
