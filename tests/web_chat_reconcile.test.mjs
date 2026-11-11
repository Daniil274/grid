/**
 * The answer on screen and the conversation on the server.
 *
 * While a turn runs, the stored conversation cannot contain its answer:
 * `core/factory/turns.py` writes the answer when the run returns. So every
 * re-read of the conversation - a resync, a post-turn reconcile, a restart -
 * races the live bubble. These tests pin down that the live answer, its trace
 * and its place survive those reads, that an older read cannot draw over a
 * newer turn, and that one reconnect means one read and one attach.
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

document.addEventListener = listen(docListeners);
document.removeEventListener = () => {};
document.dispatchEvent = () => true;
document.visibilityState = "visible";

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

const userMessage = (id, text) => ({ id, role: "user", content: text, timestamp: 1 });
const assistantMessage = (id, text) => ({ id, role: "assistant", content: text, timestamp: 2 });
const runningTurn = () => ({ message: "hi", resumes: false, elapsed_ms: 5200 });

/**
 * A chat on screen. `state` is what the server answers: the messages stored so
 * far and the turn it is still running, if any. `reads` counts the conversation
 * reads, `fetch` overrides one read (a slow answer, a fixed payload).
 */
async function openChat(state, { fetch = null } = {}) {
  FakeSocket.instances.length = 0;
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
  chat.reads = 0;
  api.getConversation = async (id, signal) => {
    chat.reads += 1;
    if (fetch) return fetch(id, signal, state);
    return { id, root: id, messages: state.messages, metadata: {}, pending: [], active_turn: state.active_turn };
  };
  api.conversationPlan = async () => ({ tasks: [] });
  api.createConversation = async () => ({ id: "c-new", workspace: null });
  await chat.openConversation("c1");
  return { chat, store, transcript, state };
}

const payloadFor = (id, state) => ({
  id,
  root: id,
  messages: state.messages,
  metadata: {},
  pending: [],
  active_turn: state.active_turn ?? null,
});

/**
 * The same page, but with more than one chat on the server: `states` are what
 * each chat holds, and `fetch(id, call)` answers the `call`-th read of `id`.
 * `chat.reads` counts the reads of every chat.
 */
async function openChats(states, { fetch = null, open = "c1" } = {}) {
  FakeSocket.instances.length = 0;
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
  chat.reads = new Map();
  api.getConversation = async (id, signal) => {
    const call = (chat.reads.get(id) ?? 0) + 1;
    chat.reads.set(id, call);
    if (fetch) return fetch(id, call, states[id], signal);
    return payloadFor(id, states[id]);
  };
  api.conversationPlan = async () => ({ tasks: [] });
  api.createConversation = async () => ({ id: "c-new", workspace: null });
  await chat.openConversation(open);
  return { chat, store, transcript, states };
}

// -- the live answer survives a read -----------------------------------------
test("a reconcile during a live turn does not draw the stored history over it", async () => {
  const state = { messages: [userMessage("m1", "hi")], active_turn: runningTurn() };
  const { chat, transcript } = await openChat(state);
  lastSocket().deliver({ type: "token", content: "half an ans" });
  const answer = transcript.last();
  assert.equal(answer.text, "half an ans");
  const reads = chat.reads;

  await chat._reconcile("c1");

  assert.equal(chat.reads, reads + 1, "the read happened");
  assert.equal(transcript.count(), 2, "the request and the live answer");
  assert.equal(transcript.last(), answer, "the same bubble, its trace untouched");
  assert.equal(answer.text, "half an ans", "the streamed text is still there");
  assert.equal(chat.isStreaming, true);
  assert.equal(attachFrames(), 1, "the running turn was not attached a second time");

  // The turn goes on into the same bubble.
  lastSocket().deliver({ type: "token", content: "wer" });
  assert.equal(answer.text, "half an answer");
});

test("a resync keeps the answer on screen, socket and all", async () => {
  const state = { messages: [userMessage("m1", "hi")], active_turn: runningTurn() };
  const { chat, transcript } = await openChat(state);
  lastSocket().deliver({ type: "token", content: "part" });
  lastSocket().deliver({ type: "step", step: { id: "s1", kind: "tool", title: "search", status: "ok" } });
  const answer = transcript.last();
  assert.match(answer.el.textContent, /search/, "the trace of the live turn is drawn");
  const connections = sockets().length;

  await chat.resumeLive(0);

  assert.equal(transcript.count(), 2);
  assert.equal(transcript.last(), answer, "the live bubble is not dropped before the snapshot");
  assert.equal(answer.text, "part");
  assert.match(answer.el.textContent, /search/, "and its trace is not lost either");
  assert.equal(chat.isStreaming, true);
  // The socket the turn streams over is open and carries the turn the server
  // runs: reading the conversation again drew nothing over the stream, and
  // nothing was torn down to be attached again - no reconnect, no duplicate.
  assert.equal(sockets().length, connections, "no reconnect: the live socket stays");
  assert.equal(attachFrames(), 1, "one attach - the one the open made");

  // The stream goes on where it was: the frames append, nothing repeats.
  lastSocket().deliver({ type: "token", content: "ial answer" });
  assert.equal(transcript.count(), 2, "one answer bubble, not two");
  assert.equal(transcript.last(), answer);
  assert.equal(answer.text, "partial answer");
});

test("a frame the dropped turn's socket still delivers cannot paint into the answer", async () => {
  const state = { messages: [userMessage("m1", "hi")], active_turn: runningTurn() };
  const { chat, transcript } = await openChat(state);
  const dropped = lastSocket();
  lastSocket().deliver({ type: "token", content: "part" });
  const answer = transcript.last();

  // The turn ended, and its socket was closed for it: what was already in
  // flight still arrives, and none of it may paint into what follows.
  state.active_turn = null;
  dropped.deliver({ type: "done", duration_ms: 1, stopped: false });
  assert.equal(chat.isStreaming, false, "the turn is over");

  // A new turn starts on a socket of its own.
  await chat.send("next question");
  const live = transcript.last();
  assert.equal(chat.isStreaming, true, "the turn that replaced it runs");

  dropped.deliver({ type: "token", content: " STALE" });
  dropped.deliver({ type: "done", duration_ms: 1, stopped: false, detached: true });
  assert.equal(answer.text, "part", "a closed socket's late frame is ignored");
  assert.equal(live.text, "", "and it cannot touch the turn that replaced it");
  assert.equal(chat.isStreaming, true, "the stale done cannot end the new turn");

  lastSocket().deliver({ type: "token", content: "the next answer" });
  assert.equal(live.text, "the next answer", "the new turn streams into its own bubble");
});

// -- reads race each other and the turns on screen ---------------------------
test("a read that started before the next turn cannot draw over it", async () => {
  const state = { messages: [userMessage("m1", "hi"), assistantMessage("m2", "first answer")], active_turn: null };
  const { chat, transcript } = await openChat(state);

  // A reconcile of the finished turn is in flight: the server is slow.
  let release = null;
  const stale = state.messages.map((message) => ({ ...message }));
  api.getConversation = () => new Promise((resolve) => {
    release = () => resolve({ id: "c1", root: "c1", messages: stale, metadata: {}, pending: [], active_turn: null });
  });
  const reconciling = chat._reconcile("c1");
  await tick();

  // The reader sends the next message meanwhile.
  await chat.send("second question");
  const live = transcript.last();
  assert.equal(transcript.count(), 4, "two stored messages, the request and the live answer");
  assert.equal(chat.isStreaming, true);

  // The old read answers now, with a conversation that predates the new turn.
  release();
  await reconciling;

  assert.equal(transcript.count(), 4, "the newer turn is what stays on screen");
  assert.equal(transcript.last(), live);
  assert.equal(chat.isStreaming, true, "the live turn was not stopped by the old read");
});

test("a resync scheduled for one turn does not drop a turn that started after it", async () => {
  const state = { messages: [userMessage("m1", "hi")], active_turn: runningTurn() };
  const { chat, transcript } = await openChat(state);

  // The page woke up and scheduled a resync; before it fires the turn ends and
  // the reader sends the next message.
  const resuming = chat.resumeLive(20);
  state.messages = [userMessage("m1", "hi"), assistantMessage("m2", "first answer")];
  state.active_turn = null;
  lastSocket().deliver({ type: "done", duration_ms: 1000, stopped: false });
  await tick(2);
  await chat.send("next question");
  const live = transcript.last();

  await resuming;

  assert.equal(chat.isStreaming, true, "the new turn keeps its own socket");
  assert.equal(transcript.last(), live, "and its own bubble");
});

// -- one reconnect, one read, one attach -------------------------------------
test("a restart reconnects with one read and one attach", async () => {
  const state = { messages: [userMessage("m1", "hi")], active_turn: runningTurn() };
  const { chat, transcript } = await openChat(state);
  lastSocket().deliver({ type: "token", content: "half" });
  const reads = chat.reads;
  const connections = sockets().length;

  await chat.reconnectAfterRestart();

  assert.equal(chat.reads, reads + 1, "one read, not a racy pair");
  assert.equal(sockets().length, connections + 1, "one new socket");
  assert.equal(attachFrames(), 2, "one attach");
  assert.equal(transcript.count(), 2);
  assert.equal(transcript.last().text, "half", "the answer survived the restart");
  assert.equal(chat.isStreaming, true);
});

// -- a turn that ended on tool calls -----------------------------------------
// `core/run_stream.py:run_output_text` hands a run that produced no text an
// explicit note, and `core/factory/turns.py` stores it as the answer when the
// run returns - so such a turn does have an assistant message on the server.
test("a turn that ended on tool calls shows the note and trace the server stored", async () => {
  const state = { messages: [userMessage("m1", "hi")], active_turn: null };
  const { chat, transcript } = await openChat(state);

  await chat.send("only tools ran");
  // The server stores the request and, when the run returns, the note.
  state.messages = [
    userMessage("m1", "hi"),
    userMessage("m2", "only tools ran"),
    {
      id: "m3",
      role: "assistant",
      content: "[Grid finished without a written report after 1 tool call(s): search. Check its changes yourself or ask it for the report.]",
      timestamp: 2,
      trace: [{ id: "s1", kind: "tool", title: "search", status: "ok" }],
    },
  ];
  lastSocket().deliver({ type: "step", step: { id: "s1", kind: "tool", title: "search", status: "ok" } });
  lastSocket().deliver({ type: "done", duration_ms: 1000, stopped: false });
  await tick(4);

  assert.equal(transcript.count(), 3, "the request, the turn and the answer the server stored");
  assert.equal(transcript.last().role, "assistant");
  assert.match(transcript.last().text, /without a written report/, "the server's note, once");
  assert.match(transcript.last().el.textContent, /search/, "and the trace stored with it");
  assert.equal(chat.isStreaming, false);
});

// -- wake-up bursts and the chat that replaced theirs ------------------------
// A burst is scheduled for the chat the reader is in. Waking up is not a
// reason to read, attach or redraw a chat they have opened since.
test("a resume burst scheduled in one chat does not read or draw the chat opened after it", async () => {
  const states = {
    c1: { messages: [userMessage("m1", "hi")], active_turn: runningTurn() },
    // The chat the reader moves to: no turn of its own running.
    c2: { messages: [userMessage("n1", "older"), assistantMessage("n2", "newest stored")], active_turn: null },
  };
  const { chat, transcript } = await openChats(states, {
    fetch: (id, call, state) => {
      if (id === "c1") return payloadFor(id, state);
      // The open's own read: slow, a mobile network. It carries the
      // conversation as it is.
      if (call === 1) return new Promise((resolve) => setTimeout(() => resolve(payloadFor(id, states.c2)), 60));
      // Any second read can only be a resync of a chat nobody asked to
      // reconcile: it answers with the snapshot the reader has left behind.
      return payloadFor(id, { messages: [userMessage("n1", "older")], active_turn: null });
    },
  });

  const resuming = chat.resumeLive(10);
  const opening = chat.openConversation("c2");
  await tick(30); // the burst fires while c2 is still loading
  await opening;
  await resuming;
  await tick(60);

  assert.equal(chat.reads.get("c2"), 1, "the chat that was opened is read once");
  assert.equal(transcript.count(), 2, "both stored messages, nothing drawn over them");
  assert.equal(transcript.last().text, "newest stored");
  assert.equal(attachFrames(), 1, "only c1's turn was attached, and only when it was opened");
  assert.equal(chat.isStreaming, false, "the chat opened has no turn, and none was invented");
});

test("a resume burst does not read or attach a chat that opened with its own turn", async () => {
  const states = {
    c1: { messages: [userMessage("m1", "hi")], active_turn: runningTurn() },
    c2: { messages: [userMessage("n1", "other")], active_turn: { message: "other", resumes: false, elapsed_ms: 400 } },
  };
  const { chat, transcript } = await openChats(states, {
    fetch: (id, call, state) => {
      if (id === "c1") return payloadFor(id, state);
      if (call === 1) return new Promise((resolve) => setTimeout(() => resolve(payloadFor(id, states.c2)), 60));
      throw new Error("a chat opened with a running turn is not reconciled by a burst from another chat");
    },
  });

  const resuming = chat.resumeLive(10);
  const opening = chat.openConversation("c2");
  await tick(30);
  await opening;
  await resuming;

  assert.equal(chat.reads.get("c2"), 1, "one read: the open's own");
  assert.equal(chat.isStreaming, true, "and the turn it found running is followed");
  assert.equal(attachFrames(), 2, "one attach for c1's open, one for c2's own turn");
  assert.equal(transcript.count(), 2);
});

test("a resume burst does not return with the chat the reader came back to (A -> B -> A)", async () => {
  const states = {
    A: { messages: [userMessage("a1", "A as opened again")], active_turn: null },
    B: { messages: [userMessage("b1", "B")], active_turn: null },
  };
  const { chat, transcript } = await openChats(states, {
    open: "A",
    fetch: (id, call, state) => {
      if (id === "B") return payloadFor(id, state);
      if (call === 2) {
        // The reopen of A: slow.
        return new Promise((resolve) => setTimeout(() => resolve(payloadFor(id, states.A)), 40));
      }
      if (call > 2) {
        // Only the burst's own read gets here - late, and with the chat as it
        // was before the reader left it.
        return new Promise((resolve) => setTimeout(() => resolve(
          payloadFor(id, { messages: [userMessage("a0", "A before")], active_turn: null }),
        ), 80));
      }
      return payloadFor(id, state);
    },
  });

  const resuming = chat.resumeLive(10);
  await chat.openConversation("B");
  const back = chat.openConversation("A");
  await tick(30); // the burst would have fired here
  await back;
  await resuming;
  await tick(120);

  assert.equal(chat.reads.get("A"), 2, "read when opened and when reopened, not by the burst before them");
  assert.equal(transcript.count(), 1);
  assert.equal(transcript.last().text, "A as opened again", "the stale snapshot was not drawn");
});

test("a read from before A -> B -> A cannot draw on the chat that came back", async () => {
  const states = {
    A: { messages: [userMessage("a1", "A as opened again")], active_turn: null },
    B: { messages: [userMessage("b1", "B")], active_turn: null },
  };
  let release = null;
  const { chat, transcript } = await openChats(states, {
    open: "A",
    fetch: (id, call, state) => {
      if (id === "B") return payloadFor(id, state);
      if (call !== 2) return payloadFor(id, state);
      // A reconcile of A, held open across the whole round trip: its answer is
      // A as it was before the reader left.
      return new Promise((resolve) => {
        release = () => resolve(payloadFor(id, { messages: [userMessage("a0", "A before")], active_turn: null }));
      });
    },
  });

  const reconciling = chat._reconcile("A");
  await tick();
  await chat.openConversation("B");
  await chat.openConversation("A");
  release();
  await reconciling;

  assert.equal(transcript.count(), 1);
  assert.equal(transcript.last().text, "A as opened again", "the chat on screen is the one the reader reopened");
});

// -- a refused turn takes no more content ------------------------------------
test("a token between error and done is not appended to the failed answer", async () => {
  const state = { messages: [userMessage("m1", "hi")], active_turn: runningTurn() };
  const { chat, transcript } = await openChat(state);
  lastSocket().deliver({ type: "token", content: "half" });
  const answer = transcript.last();

  lastSocket().deliver({ type: "error", content: "the model failed" });
  // What was already in flight when the refusal was sent.
  lastSocket().deliver({ type: "token", content: " MORE" });
  lastSocket().deliver({ type: "reasoning", id: "r1", delta: " late thought" });
  lastSocket().deliver({ type: "step", step: { id: "s9", kind: "tool", title: "late-step", status: "ok" } });

  assert.equal(answer.text, "half", "the refused answer is not extended");
  assert.doesNotMatch(answer.el.textContent, /late-step/, "and gains no step");

  lastSocket().deliver({ type: "done", duration_ms: 5, stopped: false });
  assert.equal(chat.isStreaming, false, "the turn still ends");
});

// -- a read that lost the race is not a settled screen -----------------------
test("a resync whose read was superseded reads again instead of calling it done", async () => {
  const state = { messages: [userMessage("m1", "hi")], active_turn: runningTurn() };
  const { chat, transcript } = await openChat(state);
  lastSocket().deliver({ type: "token", content: "part" });
  const answer = transcript.last();

  let releaseResync = null;
  let calls = 0;
  api.getConversation = (id) => {
    calls += 1;
    if (calls === 1) {
      // The resync's read: slow. A newer read answers before it.
      return new Promise((resolve) => {
        releaseResync = () => resolve(payloadFor(id, { messages: [userMessage("m1", "hi")], active_turn: runningTurn() }));
      });
    }
    return Promise.resolve(payloadFor(id, { messages: [userMessage("m1", "hi")], active_turn: runningTurn() }));
  };

  const resyncing = chat.resumeLive(0);
  await tick(2);
  const newer = chat._reconcile("c1"); // takes the generation over
  await newer;
  releaseResync();
  await resyncing;

  assert.equal(calls, 3, "the superseded read did not settle the screen: the resync read again");
  assert.equal(transcript.last(), answer, "and the answer on screen is still the one being followed");
  assert.equal(chat.isStreaming, true);
});

// -- an opening and a resync of the same chat --------------------------------
// An opening is a read like any other: it may answer after a resync of the
// same chat has already drawn the newer snapshot, and then it must not put the
// older one back on screen.
test("an opening's own read cannot draw over a newer resync of the same chat", async () => {
  const states = {
    c1: { messages: [userMessage("m1", "hi")], active_turn: null },
    c2: { messages: [userMessage("n1", "older"), assistantMessage("n2", "newest stored")], active_turn: null },
  };
  const { chat, transcript } = await openChats(states, {
    fetch: (id, call, state) => {
      if (id !== "c2") return payloadFor(id, state);
      if (call === 1) {
        // The open's own read: slow, and with the chat as it was before.
        return new Promise((resolve) => setTimeout(() => resolve(
          payloadFor(id, { messages: [userMessage("n1", "older")], active_turn: null }),
        ), 80));
      }
      return payloadFor(id, state);
    },
  });

  const opening = chat.openConversation("c2");
  await tick(5); // the open's read is still in flight
  const resuming = chat.resumeLive(0); // the page woke and resynced the same chat
  await Promise.all([opening, resuming]);
  await tick(100);

  assert.equal(chat.reads.get("c2"), 2, "the open's read and the resync's");
  assert.equal(transcript.count(), 2, "the newer snapshot, not the older answer");
  assert.equal(transcript.last().text, "newest stored");
  assert.equal(chat.isStreaming, false);
});

// One turn per chat: an open that finds a running turn and a resync that found
// the same turn must not each attach a socket of their own to it.
test("a resync during the open of a running chat attaches it once", async () => {
  const states = {
    c1: { messages: [userMessage("m1", "hi")], active_turn: null },
    c2: { messages: [userMessage("n1", "hi")], active_turn: runningTurn() },
  };
  const { chat, transcript } = await openChats(states, {
    fetch: (id, call, state) => {
      if (id !== "c2") return payloadFor(id, state);
      // Both reads carry the same running turn; only their timing differs.
      return new Promise((resolve) => setTimeout(() => resolve(payloadFor(id, state)), call === 1 ? 80 : 5));
    },
  });

  const opening = chat.openConversation("c2");
  await tick(5);
  const resuming = chat.resumeLive(0);
  await Promise.all([opening, resuming]);
  await tick(20);

  assert.equal(attachFrames(), 1, "one attach for the one running turn");
  assert.equal(sockets().length, 1, "and one socket");
  assert.equal(chat.isStreaming, true);
  assert.equal(transcript.count(), 2, "the stored request and the replayed answer");

  states.c2 = { messages: [userMessage("n1", "hi"), assistantMessage("n2", "answer")], active_turn: null };
  lastSocket().deliver({ type: "done", duration_ms: 1, stopped: false });
  await tick(10);
  assert.equal(chat.isStreaming, false);
  assert.ok(sockets().every((socket) => socket.readyState === FakeSocket.CLOSED), "no socket is left open");
});

// -- a wake is about the turn on screen now ---------------------------------
// A burst scheduled while one turn ran is reused by a later wake, but it must
// be about the turn that runs now: otherwise the wake reconciles nothing.
test("a wake reusing a pending burst resyncs the turn on screen now", async () => {
  const state = { messages: [userMessage("m1", "hi")], active_turn: runningTurn() };
  const { chat, transcript } = await openChat(state);
  const burst = chat.resumeLive(200); // the page went away during the first turn
  lastSocket().deliver({ type: "done", duration_ms: 10, stopped: false });
  await tick(2);
  await chat.send("second"); // a second turn runs, with a socket of its own
  const live = transcript.last();
  const before = chat.reads;

  const wake = chat.resumeLive(0); // the page wakes again, now for the second turn
  await Promise.all([burst, wake]);

  assert.equal(chat.reads, before + 1, "the wake read the chat instead of settling for the old turn");
  assert.equal(chat.isStreaming, true, "and follows the running turn again");
  assert.equal(transcript.last().role, "assistant", "rebuilt from the server's snapshot");
});
