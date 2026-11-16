/**
 * "Repeat request" under a message: the same request runs again from the same
 * point. A user message repeats itself; an answer repeats the request behind
 * it (js/ui/transcript.js's requestOf). Both ride the edit flow - the branch
 * cut before the request, the text untouched - so the repeat is a new version
 * of the message, one click away in its version switcher, never a copy of the
 * bubble further down the chat.
 *
 * The API and the websocket are fakes: this is the UI's behaviour, not the
 * server's. The DOM is the shared fake (tests/fake_dom.mjs).
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { installFakeDom } from "./fake_dom.mjs";
import { createMessage } from "../web_chat/js/ui/message.js";

installFakeDom();

// -- the page a ChatController expects ---------------------------------------
globalThis.window = { addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => true };
document.addEventListener = () => {};
document.removeEventListener = () => {};
document.dispatchEvent = () => true;
document.visibilityState = "visible";
globalThis.location = { protocol: "http:", host: "chat.test" };
// Views coalesce repaints into an animation frame; Node has no frames. The
// handle must not hold the runner open.
globalThis.requestAnimationFrame = (fn) => {
  const handle = setTimeout(() => fn(Date.now()), 1);
  handle.unref?.();
  return handle;
};
globalThis.cancelAnimationFrame = (handle) => clearTimeout(handle);

// -- the socket --------------------------------------------------------------
class FakeSocket {
  static OPEN = 1;

  static CLOSED = 3;

  static instances = [];

  constructor(url) {
    this.url = url;
    this.readyState = FakeSocket.OPEN;
    this.sent = [];
    this.listeners = new Map();
    FakeSocket.instances.push(this);
    // A socket opens on its own, after the constructor returned.
    queueMicrotask(() => {
      if (this.readyState !== FakeSocket.OPEN) return;
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

  /** A frame from the server. */
  deliver(payload) {
    this.emit("message", { data: JSON.stringify(payload) });
  }
}

globalThis.WebSocket = FakeSocket;

const { ChatController } = await import("../web_chat/js/chat.js");
const { createTranscript } = await import("../web_chat/js/ui/transcript.js");
const { createStore } = await import("../web_chat/js/lib/store.js");
const { api } = await import("../web_chat/js/net/api.js");

const tick = (ms = 5) => new Promise((resolve) => setTimeout(resolve, ms));
const lastSocket = () => FakeSocket.instances.at(-1);

// -- the button itself -------------------------------------------------------
const retryButton = (message) => message.el.find("msgAction--retry");

test("a user message offers to repeat its request", () => {
  const seen = [];
  const message = createMessage({ role: "user", content: "hello again", onRetry: (target) => seen.push(target) });
  message.adopt({ id: "u1" });

  const button = retryButton(message);
  assert.ok(button, "the button is in the action row");
  assert.equal(button.getAttribute("title"), "Repeat this request (starts a new branch)");
  button.click();

  assert.deepEqual(seen, [{ id: "u1", role: "user" }], "it names the message and its role");
});

test("an answer offers to repeat the request behind it", () => {
  const seen = [];
  const message = createMessage({ role: "assistant", content: "the answer", onRetry: (target) => seen.push(target) });
  message.adopt({ id: "a1" });

  const button = retryButton(message);
  assert.ok(button, "the button is in the action row");
  assert.equal(button.getAttribute("title"), "Repeat the request behind this answer (starts a new branch)");
  button.click();

  assert.deepEqual(seen, [{ id: "a1", role: "assistant" }]);
});

test("a message whose turn is not stored yet repeats nothing", () => {
  const seen = [];
  const message = createMessage({ role: "user", content: "in flight", onRetry: (target) => seen.push(target) });

  const button = retryButton(message);
  assert.ok(button, "the button is there - the turn will be saved");
  button.click();

  assert.deepEqual(seen, [], "no repeat before the message has an id");
});

test("without a handler, or on a system message, there is no button", () => {
  assert.equal(retryButton(createMessage({ role: "user", content: "hi" })), null);
  assert.equal(retryButton(createMessage({ role: "assistant", content: "hi" })), null);
  assert.equal(retryButton(createMessage({ role: "system", content: "hi", onRetry: () => {} })), null);
});

// -- what a repeat does ------------------------------------------------------
/** A stored chat on screen, with the messages the test gives it. */
async function openChat(messages) {
  FakeSocket.instances.length = 0;
  const store = createStore({ contextId: null, systems: [], conversations: [], queue: [] });
  const transcript = createTranscript({
    container: document.createElement("div"),
    emptyState: document.createElement("div"),
    jumpButton: document.createElement("button"),
  });
  const chat = new ChatController({
    store,
    transcript,
    onConversationsChanged: async () => {},
    speech: { stop() {}, speak() {}, isActive: () => false, activeId: null },
  });
  api.getConversation = async (id) => ({ id, root: "c1", messages, metadata: {}, pending: [], active_turn: null });
  api.conversationPlan = async () => ({ tasks: [] });
  api.executionTarget = async () => null;
  await chat.openConversation("c1");
  return { chat, transcript };
}

test("an answer repeats the request behind it", async () => {
  const calls = [];
  const original = api.createBranch;
  api.createBranch = async (id, messageId) => {
    calls.push([id, messageId]);
    return { id: "b1", edit_of: "slot-1" };
  };
  try {
    const { chat } = await openChat([
      { id: "u1", role: "user", content: "hello again", timestamp: 1 },
      { id: "a1", role: "assistant", content: "the answer", timestamp: 2 },
    ]);

    await chat.retryMessage({ id: "a1", role: "assistant" });
    await tick();

    assert.deepEqual(calls, [["c1", "u1"]], "the branch is cut before the request, not the answer");
    const sent = lastSocket().sent.at(-1);
    assert.equal(sent.message, "hello again", "the same text is sent");
    assert.equal(sent.edit_of, "slot-1", "as a new version of the request");
  } finally {
    api.createBranch = original;
  }
});

test("a user message repeats itself", async () => {
  const calls = [];
  const original = api.createBranch;
  api.createBranch = async (id, messageId) => {
    calls.push([id, messageId]);
    return { id: "b1", edit_of: "slot-1" };
  };
  try {
    const { chat } = await openChat([
      { id: "u1", role: "user", content: "hello again", timestamp: 1 },
      { id: "a1", role: "assistant", content: "the answer", timestamp: 2 },
    ]);

    await chat.retryMessage({ id: "u1", role: "user" });
    await tick();

    assert.deepEqual(calls, [["c1", "u1"]]);
    const sent = lastSocket().sent.at(-1);
    assert.equal(sent.message, "hello again");
    assert.equal(sent.edit_of, "slot-1");
  } finally {
    api.createBranch = original;
  }
});

test("an answer without a request of its own repeats nothing", async () => {
  // A turn a Continue resumed, or a context seed: no request of the user's.
  const calls = [];
  const original = api.createBranch;
  api.createBranch = async (id, messageId) => {
    calls.push([id, messageId]);
    return { id: "b1", edit_of: "slot-1" };
  };
  try {
    const { chat } = await openChat([
      { id: "a1", role: "assistant", content: "the resumed answer", timestamp: 2 },
    ]);

    await chat.retryMessage({ id: "a1", role: "assistant" });
    await tick();

    assert.deepEqual(calls, [], "nothing is forked - there is no request to send again");
  } finally {
    api.createBranch = original;
  }
});
