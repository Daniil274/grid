/**
 * Coming back to the page mid-turn: a phone that locked its screen, a tab put
 * in the back/forward cache, a network that moved. The socket may be dead
 * without ever saying so, so the page drops the turn *without finalising it*
 * and attaches to the conversation again - the server answers with a snapshot
 * of the turn it still runs (web_chat/session.py, AgentTurn.attach).
 *
 * The API and the websocket are fakes: this is the UI's behaviour, not the
 * server's. The DOM is the shared fake (tests/fake_dom.mjs).
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { installFakeDom } from "./fake_dom.mjs";

installFakeDom();

// -- the page: visibility, pageshow, online ---------------------------------
const docListeners = new Map();
const pageListeners = new Map();
const listen = (map) => (type, fn) => {
  if (!map.has(type)) map.set(type, []);
  map.get(type).push(fn);
};
const fire = (map, type, detail = {}) => {
  for (const fn of [...(map.get(type) ?? [])]) fn({ type, ...detail });
};

document.addEventListener = listen(docListeners);
document.removeEventListener = () => {};
document.dispatchEvent = () => true;
document.visibilityState = "visible";

// chat.js wakes on the page's own events; a page has `window`, Node does not.
globalThis.window = {
  addEventListener: listen(pageListeners),
  removeEventListener: () => {},
  dispatchEvent: () => true,
};

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
    // A socket opens on its own, after the constructor returned.
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

  /** The socket is gone and nobody heard about it - what a lock screen does. */
  die() {
    this.readyState = FakeSocket.CLOSED;
  }

  /** A frame from the server. */
  deliver(payload) {
    this.emit("message", { data: JSON.stringify(payload) });
  }
}

globalThis.WebSocket = FakeSocket;
globalThis.location = { protocol: "http:", host: "chat.test" };
// The views coalesce repaints into an animation frame; Node has no frames.
// A frame must not hold the test runner open, unlike a browser's own frames.
globalThis.requestAnimationFrame = (fn) => {
  const handle = setTimeout(() => fn(Date.now()), 1);
  handle.unref?.();
  return handle;
};
globalThis.cancelAnimationFrame = (handle) => clearTimeout(handle);

const { ChatController } = await import("../web_chat/js/chat.js");
const { ChatConnection } = await import("../web_chat/js/net/chat-socket.js");
const { createTranscript } = await import("../web_chat/js/ui/transcript.js");
const { createStore } = await import("../web_chat/js/lib/store.js");
const { api } = await import("../web_chat/js/net/api.js");

// -- helpers -----------------------------------------------------------------
const sockets = () => FakeSocket.instances;
const lastSocket = () => sockets().at(-1);
const attachFrames = () => sockets().flatMap((socket) => socket.sent).filter((frame) => frame.action === "attach").length;
const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

const storedUser = (text = "hi") => [{ id: "m1", role: "user", content: text, timestamp: 1 }];

/**
 * A chat on screen with a turn running on the server, attached to it.
 * `state` is what the server answers: the transcript stored so far and the
 * turn it is still running, if any.
 */
async function openChat(state) {
  FakeSocket.instances.length = 0; // each case counts its own connections
  const store = createStore({ contextId: null, rootId: null, systems: [], conversations: [], queue: [] });
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
  api.getConversation = async (id) => ({
    id,
    root: id,
    messages: state.messages,
    metadata: {},
    pending: [],
    active_turn: state.active_turn,
  });
  api.conversationPlan = async () => ({ tasks: [] });
  await chat.openConversation("c1");
  return { chat, store, transcript, state };
}

const runningTurn = (resumes = false) => ({ message: resumes ? "" : "hi", resumes, elapsed_ms: 5200 });

// -- tests -------------------------------------------------------------------
test("hide and show sends exactly one attach, however many wake-up events arrive", async () => {
  const state = { messages: storedUser(), active_turn: runningTurn() };
  const { chat } = await openChat(state);
  assert.equal(attachFrames(), 1); // the open chat attached to its running turn
  const before = sockets().length;

  // The phone locks: the page hides. Then everything that says "back" fires
  // at once - visible, the page restored from the cache, the network returning.
  document.visibilityState = "hidden";
  fire(docListeners, "visibilitychange");
  document.visibilityState = "visible";
  fire(docListeners, "visibilitychange");
  fire(pageListeners, "pageshow", { persisted: true });
  fire(pageListeners, "online");
  await chat.resumeLive();

  assert.equal(sockets().length, before + 1, "one new connection");
  assert.equal(attachFrames(), 2, "exactly one attach for the whole burst");
  assert.equal(lastSocket().sent.filter((frame) => frame.action === "attach").length, 1);
  assert.equal(chat.isStreaming, true, "the running turn is followed again");

  // A done frame is what ends it.
  state.active_turn = null;
  lastSocket().deliver({ type: "done", duration_ms: 6000, stopped: false });
  assert.equal(chat.isStreaming, false);
});

test("a socket that died without a close event still resyncs", async () => {
  const state = { messages: storedUser(), active_turn: runningTurn() };
  const { chat } = await openChat(state);
  const before = sockets().length;
  const dead = lastSocket();
  dead.die(); // readyState is CLOSED; no "close" frame ever arrives

  assert.equal(chat.isStreaming, true, "nothing on the page noticed the drop");
  assert.equal(chat._connection.isOpen, false);

  await chat.resumeLive();

  assert.equal(sockets().length, before + 1);
  assert.equal(attachFrames(), 2);
  assert.deepEqual(lastSocket().sent, [{ action: "attach" }]);
  assert.equal(chat.isStreaming, true);

  state.active_turn = null;
  lastSocket().deliver({ type: "done", duration_ms: 6000, stopped: false });
  assert.equal(chat.isStreaming, false);
});

test("a frame a closed socket cannot carry is reported, not dropped", async () => {
  const socket = new FakeSocket("ws://chat.test/api/chat/ws/c1");
  const connection = new ChatConnection(socket);
  assert.equal(connection.isOpen, false);
  assert.equal(connection.attach(), false);
  assert.equal(connection.send("hello", { system_key: null, agent_key: null }), false);
  assert.deepEqual(socket.sent, []);

  await tick(); // the socket opens
  assert.equal(connection.isOpen, true);
  assert.equal(connection.attach(), true);
  assert.deepEqual(socket.sent, [{ action: "attach" }]);
  connection.close();
});

test("a turn found still running is rebuilt in place, without a duplicate bubble", async () => {
  const state = { messages: storedUser(), active_turn: runningTurn() };
  const { chat, transcript } = await openChat(state);
  assert.equal(transcript.count(), 2, "the request and the answer following it");
  lastSocket().deliver({ type: "token", content: "half an ans" });
  assert.equal(transcript.last().text, "half an ans");

  // The lock screen came and went while the turn ran on.
  await chat.resumeLive();

  // The server's snapshot: the answer it has so far, once, into the same bubble.
  lastSocket().deliver({ type: "token", content: "half an answer" });
  assert.equal(transcript.count(), 2, "one answer bubble, not two");
  assert.equal(transcript.last().role, "assistant");
  assert.equal(transcript.last().text, "half an answer");
  assert.equal(chat.isStreaming, true);

  state.active_turn = null;
  lastSocket().deliver({ type: "done", duration_ms: 7000, stopped: false });
  assert.equal(chat.isStreaming, false);
});

test("a stream that ends without done is not finalised", async () => {
  const state = { messages: storedUser(), active_turn: runningTurn() };
  const { chat, transcript } = await openChat(state);
  const answer = transcript.last();
  const finishes = [];
  const finish = answer.reasoning.finish.bind(answer.reasoning);
  answer.reasoning.finish = (...args) => {
    finishes.push(args[0]);
    finish(...args);
  };

  lastSocket().emit("close"); // the socket dropped, no done frame
  await tick();

  assert.deepEqual(finishes, [], "the trace of a turn that may still run is not closed");
  // The turn is still on the server: the page picks it up again rather than
  // calling it finished on the evidence of a dropped socket.
  assert.equal(chat.isStreaming, true);
  assert.deepEqual(lastSocket().sent, [{ action: "attach" }]);

  // ... and a done frame ends it.
  state.active_turn = null;
  lastSocket().deliver({ type: "done", duration_ms: 9000, stopped: false });
  assert.equal(chat.isStreaming, false);
});

test("done is what finalises a turn", async () => {
  const state = { messages: storedUser(), active_turn: runningTurn() };
  const { chat, transcript } = await openChat(state);
  const answer = transcript.last();
  const finishes = [];
  const finish = answer.reasoning.finish.bind(answer.reasoning);
  answer.reasoning.finish = (...args) => {
    finishes.push(args[0]);
    finish(...args);
  };

  state.active_turn = null; // the turn is over: nothing is running any more
  lastSocket().deliver({ type: "done", duration_ms: 8100, stopped: false, tokens_in: 10, tokens_out: 20 });
  await tick();

  assert.equal(finishes.length, 1);
  assert.equal(chat.isStreaming, false);
});
