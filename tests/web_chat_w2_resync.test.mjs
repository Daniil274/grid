/**
 * The windows around a turn's registration, and the resyncs that meet them.
 *
 * The server registers a turn only at its claim (`web_chat/session.py`) and
 * writes the user's message later still (`core/factory/turns.py`): a snapshot
 * read in between can predate the turn entirely (W1) or carry it without its
 * request (W2). These tests hold the page to account through both - the
 * reader's own message must not vanish under a resync, a live socket must not
 * be torn down on a guess, a stopped turn must keep its partial answer, and
 * the queue's next turn must be picked up when it starts.
 *
 * The API and the websocket are fakes; the DOM is the shared fake
 * (tests/fake_dom.mjs), which keeps `innerHTML` unparsed - so the text a
 * message shows is read off its handle, and the count of bubbles off the
 * transcript, not off the elements' text.
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
globalThis.requestAnimationFrame = (fn) => {
  const handle = setTimeout(() => fn(Date.now()), 1);
  handle.unref?.();
  return handle;
};
globalThis.cancelAnimationFrame = (handle) => clearTimeout(handle);

const { ChatController } = await import("../web_chat/js/chat.js");
const { createTranscript } = await import("../web_chat/js/ui/transcript.js");
const { createStore } = await import("../web_chat/js/lib/store.js");
const { api } = await import("../web_chat/js/net/api.js");

// -- helpers -----------------------------------------------------------------
const sockets = () => FakeSocket.instances;
const lastSocket = () => sockets().at(-1);
const attachFrames = () => sockets().flatMap((socket) => socket.sent).filter((frame) => frame.action === "attach").length;
const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

const userMessage = (id, content) => ({ id, role: "user", content, timestamp: 1 });
const assistantMessage = (id, content) => ({ id, role: "assistant", content, timestamp: 2, trace: null });
const storedUser = (text = "hi") => [userMessage("m1", text)];
const runningTurn = (message = "hi") => ({ message, resumes: false, elapsed_ms: 5200 });
/** A typed public event the way `say_public` records one (`web_chat/observer.py`). */
const publicEvent = (id, content) => ({
  v: 1,
  id,
  kind: "message",
  visibility: "public",
  author: "Grid",
  author_key: "grid",
  content,
  status: "done",
  at: null,
  at_ms: 5,
  turn_id: "t1",
  message_id: null,
  parent_id: null,
  step_id: `${id}-step`,
});

/**
 * A chat on screen: `states[id]` is what the server answers for that chat,
 * and `chat.reads` counts every read of the API.
 */
async function openPage(states, id = "c1") {
  FakeSocket.instances.length = 0; // each case counts its own connections
  const container = document.createElement("div");
  const store = createStore({ contextId: null, rootId: null, systems: [], conversations: [], queue: [] });
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
  chat.reads = 0;
  api.getConversation = async (contextId) => {
    chat.reads += 1;
    const state = states[contextId];
    return {
      id: contextId,
      root: contextId,
      messages: state.messages,
      metadata: {},
      pending: state.pending ?? [],
      active_turn: state.active_turn,
    };
  };
  api.conversationPlan = async () => ({ tasks: [] });
  await chat.openConversation(id);
  return { chat, store, transcript, container };
}

/** The bubble elements on screen, in their order in the thread. */
const bubbles = (container) => [...container.querySelectorAll(".msg")];

// -- a wake over a live socket ------------------------------------------------
test("a resync over a socket that still streams changes nothing", async () => {
  const state = { messages: storedUser(), active_turn: runningTurn() };
  const { chat, transcript, container } = await openPage({ c1: state });
  const answer = transcript.last();
  const request = transcript.requestOf(answer);
  lastSocket().deliver({ type: "token", content: "half" });
  const before = sockets().length;

  // The page hides and comes back: every wake-up event at once.
  await chat.resumeLive(0);

  assert.equal(sockets().length, before, "a socket that is open is not torn down on a guess");
  assert.equal(attachFrames(), 1, "the same socket still streams the turn: no re-attach");
  assert.equal(chat.isStreaming, true);
  assert.equal(transcript.count(), 2, "the request and the answer are still on screen");
  assert.equal(bubbles(container).length, 2, "nothing was rebuilt underneath them");
  assert.equal(answer.text, "half", "the streamed text survived the wake");
  assert.equal(transcript.requestOf(answer), request, "the request is the same bubble, not a new one");

  // The stream carries on where it was.
  lastSocket().deliver({ type: "token", content: " an answer" });
  assert.equal(answer.text, "half an answer");
});

// -- window W2: the turn runs, its request is not written yet ------------------
test("a resync in W2 puts the request back in front of the kept answer", async () => {
  // The thread holds a finished turn; the running one ("hi") has not had its
  // request written into the thread yet - window W2.
  const state = {
    messages: [userMessage("m0", "earlier question"), assistantMessage("m1", "earlier answer")],
    active_turn: runningTurn("hi"),
  };
  const { chat, transcript, container } = await openPage({ c1: state });
  const answer = transcript.last();
  const request = transcript.requestOf(answer);
  assert.equal(request.text, "hi", "the attach had drawn the turn's request");
  lastSocket().deliver({ type: "token", content: "partial" });

  // The socket died without a close event: the page wakes to a W2 snapshot.
  lastSocket().die();
  await chat.resumeLive(0);

  const onScreen = bubbles(container);
  assert.equal(onScreen.length, 4, "the earlier thread, the request and the answer");
  assert.equal(onScreen[2], request.el, "the request is in front of the answer again");
  assert.equal(onScreen[3], answer.el, "the same answer bubble, not a second one");
  assert.equal(transcript.requestOf(answer), request, "exactly one request ahead of it");
  assert.equal(answer.text, "partial", "the streamed text survived the round trip");
  assert.equal(chat.isStreaming, true, "the turn is followed again");

  // The snapshot states the whole answer so far: it replaces, it does not repeat.
  lastSocket().deliver({ type: "token", content: "partial answer" });
  assert.equal(answer.text, "partial answer");
  assert.equal(bubbles(container).length, 4, "still one answer");
});

test("a rebuild of a running turn restores its request ahead of the answer", async () => {
  const stored = [userMessage("m0", "earlier question"), assistantMessage("m1", "earlier answer")];
  const state = { messages: stored, active_turn: null };
  const { chat, transcript, container } = await openPage({ c1: state });

  // The page was streaming this answer when it went away: a bubble the stored
  // thread does not hold, behind a request it does not hold either.
  const answer = transcript.add({ role: "assistant", author: "Grid", timestamp: 2 });
  answer.setText("partial");
  const turn = runningTurn("hi");
  const earlier = bubbles(container);

  await chat._attachToRunningTurn("c1", turn, { reveal: false, stored });

  const onScreen = bubbles(container);
  assert.equal(onScreen.length, 4, "the request is part of the thread again");
  assert.equal(onScreen[0], earlier[0], "the earlier thread is untouched");
  assert.equal(onScreen[1], earlier[1], "the earlier answer keeps its place");
  // A new bubble between the thread and the answer: the turn's own request.
  assert.equal(onScreen[3], answer.el, "the answer now trails the request");
  assert.equal(answer.text, "partial", "attaching does not blank the answer");

  // Attaching again must not duplicate the request it just restored.
  await chat._attachToRunningTurn("c1", turn, { reveal: false, stored });
  assert.equal(bubbles(container).length, 4, "no second request bubble");
  assert.equal(bubbles(container)[3], answer.el, "and the answer still trails it");

  // And once the thread stores the request, the rebuild draws nothing either.
  await chat._attachToRunningTurn("c1", turn, {
    reveal: false,
    stored: [...stored, userMessage("m2", "hi")],
  });
  assert.equal(bubbles(container).length, 4, "the stored request is the one shown, once");
  assert.equal(bubbles(container)[3], answer.el, "still one answer");
});

// -- window W1: the send is not registered yet ---------------------------------
test("a resync in the send window keeps the optimistic message", async () => {
  const state = { messages: [], active_turn: null };
  const { chat, transcript } = await openPage({ c1: state });

  // The reader sends a message; the server has not registered the turn yet
  // (window W1), but the snapshot read back already says the turn runs.
  const sending = chat.send("hi");
  await tick(2);
  state.active_turn = runningTurn("hi");
  await chat.resumeLive(0);
  await sending;

  assert.equal(sockets().length, 1, "the socket of the send is the socket of the stream");
  assert.equal(attachFrames(), 0, "nothing was torn down and rebuilt");
  assert.equal(chat.isStreaming, true);
  const answer = transcript.last();
  assert.equal(transcript.requestOf(answer)?.text, "hi", "the optimistic request is on screen");
  assert.equal(chat._liveAnswer, null, "the live turn was never orphaned into a kept one");

  // The send the page made streams into its own bubbles.
  lastSocket().deliver({ type: "token", content: "on its way" });
  assert.equal(answer.text, "on its way");
});

test("a resync in the send window waits for the turn to be registered", async () => {
  const state = { messages: [], active_turn: null };
  const { chat, transcript } = await openPage({ c1: state });

  const sending = chat.send("hi");
  await tick(2);
  const reads = chat.reads;
  const socketsBefore = sockets().length;

  // The first read of the resync predates the registration (window W1): the
  // turn is still only this page's own - the resync reads again instead of
  // dropping the send as if the server had refused it.
  let readsWhenRegistered = 0;
  api.getConversation = async (id) => {
    chat.reads += 1;
    if (chat.reads <= reads + 1) return { id, root: id, messages: [], metadata: {}, pending: [], active_turn: null };
    readsWhenRegistered = chat.reads;
    state.active_turn = runningTurn("hi"); // the server registers it between the reads
    return {
      id, root: id, messages: state.messages, metadata: {}, pending: [], active_turn: state.active_turn,
    };
  };
  await chat.resumeLive(0);
  await sending;

  assert.ok(readsWhenRegistered > reads + 1, "the resync read again until the snapshot held the turn");
  assert.equal(sockets().length, socketsBefore, "the socket of the send was never closed");
  assert.equal(chat.isStreaming, true, "the turn this page sent is still followed");
  const answer = transcript.last();
  assert.equal(transcript.requestOf(answer)?.text, "hi", "the optimistic request stayed on screen");
  lastSocket().deliver({ type: "token", content: "registered" });
  assert.equal(answer.text, "registered");
});

// -- a stopped turn keeps its partial answer -----------------------------------
test("a stopped turn keeps its partial answer until the thread holds it", async () => {
  const state = { messages: storedUser("hi"), active_turn: runningTurn("hi") };
  const { chat, transcript, container } = await openPage({ c1: state });
  const answer = transcript.last();
  lastSocket().deliver({ type: "token", content: "partial answer" });

  // Stop: the server records the interruption, and the answer is not
  // written to the thread yet - only the frame stream ever held it.
  state.active_turn = null;
  lastSocket().deliver({ type: "interrupted", content: "stopped by the reader", interruption: { reason: "user_stopped", resumable: true } });
  lastSocket().deliver({ type: "done", duration_ms: 900, stopped: true });
  await tick(2);

  assert.equal(chat.isStreaming, false);
  assert.equal(transcript.count(), 2, "the request and the partial answer stayed");
  assert.equal(answer.text, "partial answer", "the partial answer was not reset to the history");
  assert.match(answer.el.textContent, /stopped|interrupted/i, "the answer reads as stopped");
  assert.equal(chat._liveAnswer?.message, answer, "the bubble is held until the snapshot can replace it");

  // The server kept the partial answer after all: the stored thread shows it,
  // exactly once, and the held bubble is let go.
  state.messages = [userMessage("m1", "hi"), assistantMessage("m2", "partial answer")];
  await chat._reconcile("c1");

  assert.equal(transcript.count(), 2, "one answer, not the held bubble and its stored copy");
  assert.equal(bubbles(container).length, 2, "no duplicate answer on screen");
  assert.equal(transcript.last().messageId, "m2", "the stored answer is the one shown");
  assert.equal(transcript.last().text, "partial answer");
  assert.equal(chat._liveAnswer, null, "the held bubble was let go");
});

test("say_public words of a stopped turn survive the resync without duplicates", async () => {
  const state = { messages: storedUser("hi"), active_turn: runningTurn("hi") };
  const { chat, transcript, container } = await openPage({ c1: state });
  const answer = transcript.last();
  lastSocket().deliver({ type: "token", content: "partial answer" });
  lastSocket().deliver({ type: "event", event: publicEvent("e1", "I spoke to the people") });
  assert.equal(answer.activity.size, 1, "the words are on screen");

  // Stop, and the socket that carried the words goes quiet without a close.
  state.active_turn = null;
  lastSocket().deliver({ type: "interrupted", content: "stopped by the reader", interruption: { reason: "user_stopped", resumable: false } });
  lastSocket().deliver({ type: "done", duration_ms: 900, stopped: true });
  await tick(2);
  lastSocket().die();

  // The resync first reads a thread that has not written the stopped turn
  // yet, then one that has - the answer and its public words are drawn once
  // each, never the held bubble beside its stored copy.
  const stored = [
    userMessage("m1", "hi"),
    {
      id: "m2",
      role: "assistant",
      content: "partial answer",
      timestamp: 2,
      events: [publicEvent("e1", "I spoke to the people")],
      trace: null,
    },
  ];
  let reads = 0;
  api.getConversation = async (id) => {
    reads += 1;
    return {
      id, root: id, messages: reads > 1 ? stored : state.messages, metadata: {}, pending: [], active_turn: null,
    };
  };
  await chat.resumeLive(0);

  assert.equal(transcript.count(), 2, "the stopped turn is on screen");
  assert.equal(bubbles(container).length, 2, "one answer bubble, not the held one beside its stored copy");
  const storedAnswer = transcript.last();
  assert.equal(storedAnswer.messageId, "m2", "the stored answer is the one shown");
  assert.equal(storedAnswer.text, "partial answer");
  assert.equal(storedAnswer.activity.size, 1, "the words are drawn once");
  assert.equal(chat._liveAnswer, null, "the held bubble was let go");
});

// -- the queue's next turn ------------------------------------------------------
test("a turn that ends while the queue starts the next message is handed over", async () => {
  const state = { messages: storedUser("hi"), active_turn: runningTurn("hi") };
  const { chat, transcript, container } = await openPage({ c1: state });
  lastSocket().deliver({ type: "token", content: "first answer" });

  // The queue moves on the moment the first turn answers.
  state.active_turn = runningTurn("second");
  state.messages = [userMessage("m1", "hi"), assistantMessage("m2", "first answer")];
  lastSocket().deliver({ type: "done", duration_ms: 900, stopped: false });
  await tick(2);

  assert.equal(chat.isStreaming, true, "the queue's turn is followed at once");
  assert.equal(bubbles(container).length, 4, "the finished answer is drawn once - not the streamed bubble and its stored copy");
  const answer = transcript.last();
  assert.equal(answer.messageId, null, "a fresh bubble for the new turn");
  assert.equal(transcript.requestOf(answer)?.text, "second", "the next message is drawn ahead of its answer");

  // The new turn streams into its own bubble.
  lastSocket().deliver({ type: "token", content: "second answer" });
  assert.equal(answer.text, "second answer");
  assert.equal(bubbles(container).length, 4, "request, answer, request, answer");
});

test("a resync picks up the queue's turn when the frames were eaten", async () => {
  const state = { messages: storedUser("hi"), active_turn: runningTurn("hi") };
  const { chat, transcript, container } = await openPage({ c1: state });
  lastSocket().deliver({ type: "token", content: "first answer" });

  // The turn finished and the queue started the next message while the page
  // could not hear it; the socket looks open but says nothing.
  state.active_turn = runningTurn("second");
  state.messages = [userMessage("m1", "hi"), assistantMessage("m2", "first answer")];
  await chat.resumeLive(0);

  assert.equal(chat.isStreaming, true, "the queue's turn is followed");
  assert.equal(bubbles(container).length, 4, "the first answer appears once, and so does its request");
  const answer = transcript.last();
  assert.equal(transcript.requestOf(answer)?.text, "second", "the queue's message leads the stream");

  lastSocket().deliver({ type: "token", content: "second answer" });
  assert.equal(answer.text, "second answer");
  assert.equal(bubbles(container).length, 4);
});

// -- switching chats during a stream -------------------------------------------
test("waking in one chat does not disturb the stream of another (A -> B -> A)", async () => {
  const states = {
    c1: { messages: storedUser("hi"), active_turn: runningTurn("hi") },
    c2: { messages: [userMessage("n1", "older question")], active_turn: null },
  };
  const { chat, transcript, container } = await openPage(states, "c1");
  lastSocket().deliver({ type: "token", content: "partial" });

  // A wake is requested for c1; the reader moves to c2 before it fires.
  const burst = chat.resumeLive(200);
  await chat.openConversation("c2");
  const reads = chat.reads;
  await burst;
  assert.equal(chat.reads, reads, "a burst bound to c1 reads nothing for c2");
  assert.equal(chat.isStreaming, false, "c2 has no turn to follow");

  // Back to c1: the running turn is picked up again, and its own request is
  // the one on screen - once.
  await chat.openConversation("c1");
  assert.equal(chat.isStreaming, true);
  const answer = transcript.last();
  assert.equal(answer.messageId, null, "a fresh answer bubble for the running turn");
  assert.equal(transcript.requestOf(answer)?.text, "hi");
  assert.equal(bubbles(container).length, 2, "the request is drawn once");

  lastSocket().deliver({ type: "token", content: "partial answer" });
  assert.equal(answer.text, "partial answer", "the fresh bubble streams what the server has");
});