/**
 * Revisions in the chat UI: a conversation that merely drifted is re-pinned on
 * the server and the turn goes on, so the page shows one quiet line. A real
 * incompatibility still refuses the turn; the page then shows the reason and
 * offers to carry the conversation into a new chat on the current setup.
 *
 * Every decision keys on the typed `code` (`revision.*`), never on the text.
 * The server frames are the ones web_chat/session.py sends:
 *   {type:"revision_notice", code, content, context_id, details}
 *   {type:"error", code, content, context_id, details}
 *
 * The API and the websocket are fakes: this is the UI's behaviour, not the
 * server's. The DOM is the shared fake (tests/fake_dom.mjs).
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { installFakeDom } from "./fake_dom.mjs";

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

/** A chat on screen; `send` starts a turn and returns its answer and socket. */
async function startTurn(messages = []) {
  FakeSocket.instances.length = 0;
  const store = createStore({ contextId: "c1", rootId: "c1", systems: [], conversations: [], queue: [] });
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
    messages,
    metadata: {},
    pending: [],
    active_turn: null,
  });
  api.conversationPlan = async () => ({ tasks: [] });
  await chat.send("hello");
  return { chat, store, transcript, answer: transcript.last(), socket: lastSocket() };
}

const notices = (answer) => answer.el.querySelectorAll(".msg__revision");
const refusalButton = (answer) => answer.el.querySelector(".msg__continue");

// -- a re-pin is a line, not an error ---------------------------------------
test("a revision notice is one quiet line and the turn goes on", async () => {
  const { answer, socket, chat } = await startTurn();

  socket.deliver({
    type: "revision_notice",
    code: "revision.system_config_changed",
    content: "The system configuration changed since this chat started.",
    context_id: "c1",
    details: { changes: ["system_config"], previous_digest: "old", grid_version: "1.2.3" },
  });

  const lines = notices(answer);
  assert.equal(lines.length, 1);
  assert.equal(lines[0].textContent, "The system configuration changed since this chat started.");
  assert.equal(lines[0].dataset.code, "revision.system_config_changed");
  assert.equal(answer.el.classList.contains("is-failed"), false, "nothing failed");
  assert.equal(chat.isStreaming, true, "the same turn is still running");
  assert.equal(refusalButton(answer), null, "and offers no way out - there is nothing to leave");

  socket.deliver({ type: "token", content: "The answer." });
  assert.equal(answer.text, "The answer.");
});

test("the same notice is not drawn twice", async () => {
  const { answer, socket } = await startTurn();
  const notice = {
    type: "revision_notice",
    code: "revision.grid_version_changed",
    content: "Grid was updated since this chat started.",
    context_id: "c1",
    details: {},
  };
  socket.deliver(notice);
  socket.deliver(notice);
  assert.equal(notices(answer).length, 1);
});

// -- the refusal ------------------------------------------------------------
test("a real incompatibility shows the reason and a way out", async () => {
  const calls = [];
  api.continueConversation = async (id, strategy) => {
    calls.push([id, strategy]);
    return { id: "c2", continued_from: id, strategy };
  };
  const { answer, socket, chat, store } = await startTurn([
    { id: "m1", role: "user", content: "hello", timestamp: 1 },
  ]);

  socket.deliver({
    type: "error",
    code: "revision.contract_changed",
    content: "This chat was written for a contract this runtime no longer speaks.",
    context_id: "c1",
    details: { changes: ["contract"] },
  });
  socket.deliver({ type: "done", duration_ms: 10, stopped: false });
  await tick();

  const box = answer.el.querySelector(".msg__error");
  assert.ok(box, "the refusal is shown as an error");
  assert.equal(box.dataset.code, "revision.contract_changed");
  assert.match(box.textContent, /no longer speaks/);
  assert.equal(answer.el.classList.contains("is-failed"), true);

  const button = refusalButton(answer);
  assert.ok(button, "the page offers a way out");
  assert.equal(button.textContent, "Continue in new configuration");

  button.click();
  await tick(10);

  assert.deepEqual(calls, [["c1", "tail"]], "a new chat seeded with this one's history");
  assert.equal(store.get().contextId, "c2", "and it is open");
  assert.equal(chat.isStreaming, false);
});

test("an error is dispatched by its code, not by its wording", async () => {
  // The text is the very sentence a refusal carries; without the typed code it
  // is an ordinary failure and must not offer the revision way out.
  const { answer, socket } = await startTurn();
  socket.deliver({
    type: "error",
    content: "The system configuration changed since this chat started.",
    context_id: "c1",
  });
  socket.deliver({ type: "done", duration_ms: 10, stopped: false });
  await tick();

  assert.ok(answer.el.querySelector(".msg__error"), "shown as a plain error");
  assert.equal(refusalButton(answer), null);
  assert.equal(notices(answer).length, 0, "and no revision line");
});

test("a notice whose wording says nothing is still shown, keyed on its code", async () => {
  // Deliberately unhelpful text: the client only needs the code to know this is
  // a re-pin, and the text is the server's to choose.
  const { answer, socket } = await startTurn();
  socket.deliver({ type: "revision_notice", code: "revision.runtime_changed", content: "Runtime moved on.", context_id: "c1" });
  assert.equal(notices(answer).length, 1);
  assert.equal(notices(answer)[0].dataset.code, "revision.runtime_changed");
});
