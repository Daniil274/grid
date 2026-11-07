/**
 * The reader's place in a chat that redraws itself underneath them.
 *
 * A lifecycle resync (a locked phone, a restored page, a network that moved)
 * fetches the conversation again and rebuilds the transcript. That rebuild must
 * not scroll: only opening a chat or sending a message moves the reader. The
 * reader's own scroll during a slow fetch counts too - the place is read when
 * the DOM is replaced, not when the fetch started.
 *
 * The DOM is the shared fake (tests/fake_dom.mjs) plus a small layout model:
 * the fake has no layout, and this behaviour is about layout, so messages get a
 * height and the container a viewport. The API and the socket are fakes.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { installFakeDom } from "./fake_dom.mjs";

installFakeDom();

// The transcript watches its host for the two things the fake DOM cannot do on
// its own: a container that gains a box (it was hidden when the chat opened)
// and content that changed after the render. Real engines fire these; here the
// test pulls them.
const resizeObservers = [];
const mutationObservers = [];
globalThis.ResizeObserver = class {
  constructor(callback) { this.callback = callback; resizeObservers.push(this); }
  observe(node) { this.node = node; }
  unobserve() {}
  disconnect() {}
};
globalThis.MutationObserver = class {
  constructor(callback) { this.callback = callback; mutationObservers.push(this); }
  observe(node) { this.node = node; }
  disconnect() {}
  takeRecords() { return []; }
};
const layoutChanged = () => { for (const observer of resizeObservers) observer.callback([]); };
const contentChanged = () => { for (const observer of mutationObservers) observer.callback([]); };

// -- layout model ------------------------------------------------------------
const VIEWPORT = 300;
const MESSAGE_BASE_HEIGHT = 90;
const MESSAGE_STEP = 10;
/** Heights differ per message: a restored place must be measured, not guessed. */
const messageHeight = (index) => MESSAGE_BASE_HEIGHT + index * MESSAGE_STEP;

const isMessage = (node) => Boolean(node?.classList?.contains?.("msg"));
const scrollerOf = (el) => {
  for (let node = el.parentNode; node; node = node.parentNode) if (node.isScrollHost) return node;
  return null;
};
const messagesOf = (host) => host.childNodes.filter(isMessage);
const contentHeight = (host) => messagesOf(host).reduce((sum, _node, index) => sum + messageHeight(index), 0);

const createElement = document.createElement;
document.createElement = (tag) => {
  const el = createElement(tag);
  el.getBoundingClientRect = () => {
    const host = scrollerOf(el);
    if (!host) return { top: 0, bottom: 0, height: 0 };
    if (!isMessage(el)) return { top: 0, bottom: host.clientHeight, height: host.clientHeight };
    const siblings = messagesOf(host);
    let offset = 0;
    for (let index = 0; index < siblings.length; index += 1) {
      if (siblings[index] === el) {
        const top = offset - host.scrollTop;
        return { top, bottom: top + messageHeight(index), height: messageHeight(index) };
      }
      offset += messageHeight(index);
    }
    return { top: 0, bottom: 0, height: 0 };
  };
  return el;
};

/** The message list: a fixed viewport over messages of varying heights. */
function scrollHost() {
  const el = document.createElement("div");
  el.isScrollHost = true;
  el.clientHeight = VIEWPORT;
  let top = 0;
  Object.defineProperty(el, "scrollHeight", { get: () => contentHeight(el) });
  Object.defineProperty(el, "scrollTop", {
    get: () => top,
    set: (value) => {
      const max = Math.max(0, contentHeight(el) - el.clientHeight);
      top = Math.min(Math.max(0, value), max);
    },
  });
  return el;
}

const bottomOf = (host) => Math.max(0, host.scrollHeight - host.clientHeight);

const fire = (el, type, event = {}) => {
  for (const fn of [...(el.listeners[type] ?? [])]) fn({ type, target: el, ...event });
};

// -- page lifecycle ----------------------------------------------------------
const docListeners = new Map();
const pageListeners = new Map();
const listen = (map) => (type, fn) => {
  if (!map.has(type)) map.set(type, []);
  map.get(type).push(fn);
};
const wake = (map, type, detail = {}) => {
  for (const fn of [...(map.get(type) ?? [])]) fn({ type, ...detail });
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

// -- the socket (as in tests/web_chat_resume.test.mjs) -----------------------
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
const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

/** A stored conversation of *count* messages, alternating user and answer. */
const storedThread = (count) => Array.from({ length: count }, (_value, index) => ({
  id: `m${index + 1}`,
  role: index % 2 === 0 ? "user" : "assistant",
  content: `message ${index + 1}`,
  timestamp: index + 1,
  trace: null,
}));

const runningTurn = (resumes = false) => ({ message: resumes ? "" : "hi", resumes, elapsed_ms: 5200 });

async function openChat(state) {
  FakeSocket.instances.length = 0; // each case counts its own connections
  const container = scrollHost();
  const jumpButton = document.createElement("button");
  const transcript = createTranscript({
    container,
    emptyState: document.createElement("div"),
    jumpButton,
  });
  const store = createStore({ contextId: null, rootId: null, systems: [], conversations: [], queue: [] });
  const chat = new ChatController({
    store,
    transcript,
    onConversationsChanged: async () => {},
    speech: { stop() {}, speak() {}, isActive: () => false, activeId: null },
  });
  chat._load = api.getConversation;
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
  return { chat, store, transcript, container, jumpButton, state };
}

/** The reader scrolls up into the history: the stream is no longer followed. */
function scrollUp(container, top) {
  container.scrollTop = top;
  fire(container, "wheel", { deltaY: -120 });
  fire(container, "scroll");
}

// -- tests -------------------------------------------------------------------
test("opening a chat shows its newest message", async () => {
  const state = { messages: storedThread(8), active_turn: null };
  const { container } = await openChat(state);
  assert.ok(bottomOf(container) > 0, "the history is longer than the viewport");
  assert.equal(container.scrollTop, bottomOf(container), "an opening reveals the end");
});

test("a resync keeps a reader who scrolled up exactly where they were", async () => {
  const state = { messages: storedThread(8), active_turn: runningTurn() };
  const { chat, transcript, container, jumpButton } = await openChat(state);
  scrollUp(container, 175);
  assert.equal(transcript.isFollowing(), false);
  assert.equal(jumpButton.classList.contains("is-visible"), true, "the jump button offers the way back");

  await chat.resumeLive(0);

  assert.equal(container.scrollTop, 175, "the reader did not move");
  assert.equal(transcript.isFollowing(), false, "and is still not following");
  assert.equal(jumpButton.classList.contains("is-visible"), true);
});

test("a turn that finished while the page was away also keeps the place", async () => {
  const state = { messages: storedThread(8), active_turn: runningTurn() };
  const { chat, transcript, container } = await openChat(state);
  scrollUp(container, 210);

  // The turn ran to its end on the server: the conversation grew by an answer.
  state.active_turn = null;
  state.messages = storedThread(10);
  await chat.resumeLive(0);

  assert.equal(container.scrollTop, 210, "the place is kept");
  assert.equal(transcript.count(), 10, "the whole stored conversation is shown");
  assert.equal(transcript.isFollowing(), false);
});

test("scrolling during a slow fetch decides the place that is restored", async () => {
  const state = { messages: storedThread(8), active_turn: runningTurn() };
  const { chat, transcript, container } = await openChat(state);
  scrollUp(container, 120);

  // Hold the resync's read open, and let the reader move meanwhile.
  let answer;
  api.getConversation = async (id) => new Promise((resolve) => {
    answer = () => resolve({
      id, root: id, messages: state.messages, metadata: {}, pending: [], active_turn: state.active_turn,
    });
  });
  const resync = chat.resumeLive(0);
  await tick(2);
  assert.equal(typeof answer, "function", "the fetch is in flight");
  scrollUp(container, 260);
  answer();
  await resync;

  assert.equal(container.scrollTop, 260, "the place read at render, not at fetch");
  assert.equal(transcript.isFollowing(), false);
});

test("a following reader stays at the end across a resync", async () => {
  const state = { messages: storedThread(8), active_turn: runningTurn() };
  const { chat, transcript, container } = await openChat(state);
  assert.equal(transcript.isFollowing(), true);

  await chat.resumeLive(0);

  assert.equal(transcript.isFollowing(), true);
  assert.equal(container.scrollTop, bottomOf(container), "still pinned to the newest text");

  lastSocket().deliver({ type: "token", content: "more words" });
  await tick(2);
  assert.equal(container.scrollTop, bottomOf(container), "and new tokens keep it there");
});

test("attaching does not blank the answer it is about to replace", async () => {
  const state = { messages: storedThread(1), active_turn: null };
  const { chat, transcript } = await openChat(state);
  // The page was streaming this answer when it went away.
  const answer = transcript.add({ role: "assistant", author: "Grid", timestamp: 2 });
  answer.setText("half an ans");
  assert.equal(transcript.count(), 2);

  const attaching = chat._attachToRunningTurn("c1", runningTurn(), { reveal: false });
  assert.equal(transcript.last().text, "half an ans", "the text on screen survives the attach");
  await attaching;

  // The snapshot states the whole answer so far: it replaces, it does not repeat.
  lastSocket().deliver({ type: "token", content: "half an answer" });
  assert.equal(transcript.count(), 2, "one answer bubble");
  assert.equal(transcript.last().text, "half an answer");
});

// -- switching chats ---------------------------------------------------------
/**
 * A page with several chats in it. Each chat is a stored thread; the reader
 * moves between them the way the sidebar does.
 */
async function openInbox(threads) {
  FakeSocket.instances.length = 0;
  const container = scrollHost();
  const jumpButton = document.createElement("button");
  const transcript = createTranscript({ container, emptyState: document.createElement("div"), jumpButton });
  const store = createStore({ contextId: null, rootId: null, systems: [], conversations: [], queue: [] });
  const chat = new ChatController({
    store,
    transcript,
    onConversationsChanged: async () => {},
    speech: { stop() {}, speak() {}, isActive: () => false, activeId: null },
  });
  api.getConversation = async (id) => ({
    id, root: id, messages: threads[id], metadata: {}, pending: [], active_turn: threads.active_turn ?? null,
  });
  api.conversationPlan = async () => ({ tasks: [] });
  api.createConversation = async () => ({ id: "new", workspace: null });
  const open = async (id) => { await chat.openConversation(id); await tick(8); };
  return { chat, transcript, container, jumpButton, open };
}

test("each chat keeps the place the reader left it at", async () => {
  const box = await openInbox({ c1: storedThread(8), c2: storedThread(12) });

  await box.open("c1");
  scrollUp(box.container, 175);
  await box.open("c2");
  // A chat never read before is an opening: its newest message shows.
  assert.equal(box.container.scrollTop, bottomOf(box.container), "a new chat shows its end");
  scrollUp(box.container, 90);

  await box.open("c1");
  assert.equal(box.container.scrollTop, 175, "c1 opens where it was left");
  await box.open("c2");
  assert.equal(box.container.scrollTop, 90, "and c2 keeps its own place");
});

test("a chat left at its end opens at its end and keeps following", async () => {
  const box = await openInbox({ c1: storedThread(8), c2: storedThread(12) });

  await box.open("c1");
  assert.equal(box.container.scrollTop, bottomOf(box.container));
  await box.open("c2");
  scrollUp(box.container, 60);
  await box.open("c1");

  assert.equal(box.container.scrollTop, bottomOf(box.container), "the end of c1, not a place inside it");
  assert.equal(box.transcript.isFollowing(), true, "a chat left at the end is still followed");
});

test("a chat opened while the transcript has no layout ends at its newest message", async () => {
  const box = await openInbox({ c1: storedThread(8), c2: storedThread(12) });
  await box.open("c1");

  // A hidden pane: the container has no box, so there is nothing to scroll yet.
  box.container.clientHeight = 0;
  await box.open("c2");
  box.container.clientHeight = VIEWPORT;
  layoutChanged();
  await tick(8);

  assert.equal(box.container.scrollTop, bottomOf(box.container), "the reveal waited for a layout");
});

test("content that arrives after the render keeps a following reader at the end", async () => {
  const box = await openInbox({ c1: storedThread(8) });
  await box.open("c1");
  const before = bottomOf(box.container);

  // The chat's stored plan, a lazy step, a picture: the list grows after the render.
  box.transcript.add({ role: "assistant", content: "a late card", timestamp: 99 }, { reveal: false });
  contentChanged();
  await tick(8);

  assert.ok(bottomOf(box.container) > before, "the list really grew");
  assert.equal(box.container.scrollTop, bottomOf(box.container), "the reader at the end keeps up");
});

test("late content does not move a reader who is reading further up", async () => {
  const box = await openInbox({ c1: storedThread(8) });
  await box.open("c1");
  scrollUp(box.container, 140);

  box.transcript.add({ role: "assistant", content: "a late card", timestamp: 99 }, { reveal: false });
  contentChanged();
  await tick(8);

  assert.equal(box.container.scrollTop, 140, "the reader was not pulled down");
  assert.equal(box.transcript.isFollowing(), false);
});

test("starting another chat does not forget the place of the one it replaced", async () => {
  const box = await openInbox({ c1: storedThread(8) });
  await box.open("c1");
  scrollUp(box.container, 175);

  await box.chat.startConversation();
  await box.open("c1");

  assert.equal(box.container.scrollTop, 175, "the chat opens where it was left");
});

test("a chat that is still loading does not take the place of the chat on screen", async () => {
  const box = await openInbox({ c1: storedThread(8), c2: storedThread(12) });
  await box.open("c1");
  scrollUp(box.container, 175);

  // c2 is picked, but its read has not answered: c1 is still the chat on screen,
  // and its place must not become c2's place.
  const held = new Map();
  const reading = (id) => {
    const conversation = {
      id, root: id, messages: id === "c2" ? storedThread(12) : storedThread(8), metadata: {}, pending: [], active_turn: null,
    };
    return conversation;
  };
  api.getConversation = async (id) => {
    if (id !== "c2") return reading(id);
    return new Promise((resolve) => held.set("c2", () => resolve(reading(id))));
  };
  const toC2 = box.chat.openConversation("c2");
  await tick(2);

  // The reader changes their mind before c2 is drawn; c1 comes back.
  await box.chat.openConversation("c1");
  await tick(4);
  held.get("c2")?.(); // the late answer to a chat that is no longer open
  await toC2.catch(() => {});
  await tick(4);

  api.getConversation = async (id) => reading(id);
  await box.open("c2");
  assert.equal(box.container.scrollTop, bottomOf(box.container),
    "a chat that was never drawn opens at its own newest message");
});

test("changing chats mid-restore leaves each one its own place", async () => {
  const box = await openInbox({ c1: storedThread(8), c2: storedThread(12) });
  await box.open("c1");
  scrollUp(box.container, 175);
  await box.open("c2");
  scrollUp(box.container, 90);

  // Going back to c1, then changing one's mind before its place has landed.
  const toC1 = box.chat.openConversation("c1");
  await tick(1);
  await box.open("c2");
  await toC1.catch(() => {});
  await tick(8);

  assert.equal(box.container.scrollTop, 90, "the chat that took over is not dragged to the other's place");
  await box.open("c1");
  assert.equal(box.container.scrollTop, 175, "and the interrupted chat still opens at its own");
});
