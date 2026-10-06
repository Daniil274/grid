import test from "node:test";
import assert from "node:assert/strict";
import { ChatController } from "../web_chat/js/chat.js";
import { createStore } from "../web_chat/js/lib/store.js";
import { api } from "../web_chat/js/net/api.js";

/** A transcript that records what was drawn and whether it said "loading". */
function fakeTranscript() {
  return {
    rendered: [],
    loading: [],
    cleared: 0,
    render(messages) { this.rendered.push(messages[0]?.content); },
    setLoading(value) { this.loading.push(value); },
    clear() { this.cleared++; this.rendered.push(null); },
    adopt: () => false,
    resumable: () => false,
  };
}

/** getConversation answers only when the test says so, in any order. */
function deferredApi() {
  const pending = new Map();
  api.getConversation = (id, signal) =>
    new Promise((resolve, reject) => {
      pending.set(id, (content) => resolve({ id, messages: [{ content }], metadata: {}, pending: [] }));
      signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    });
  return pending;
}

test("the chat picked last is the one drawn, whatever answers first", async () => {
  const original = api.getConversation;
  const answer = deferredApi();
  try {
    const store = createStore({ contextId: null, systems: [], conversations: [] });
    const transcript = fakeTranscript();
    const chat = new ChatController({ store, transcript });
    const first = chat.openConversation("a");
    // The pick shows at once: the row is selected before the chat arrives.
    assert.equal(store.get().contextId, "a");
    const second = chat.openConversation("b");
    answer.get("b")("chat b");
    await second;
    answer.get("a")?.("chat a"); // aborted, or late: never drawn
    await first;

    assert.equal(store.get().contextId, "b");
    assert.deepEqual(transcript.rendered, ["chat b"]);
    assert.equal(transcript.loading.at(-1), false);
  } finally { api.getConversation = original; }
});

test("a failed current load clears the old transcript and can be retried", async () => {
  const original = api.getConversation;
  const pending = new Map();
  api.getConversation = (id) => new Promise((resolve, reject) => pending.set(id, {
    resolve: (content) => resolve({ id, messages: [{ content }], metadata: {}, pending: [] }), reject,
  }));
  try {
    const store = createStore({ contextId: null, systems: [], conversations: [] });
    const transcript = fakeTranscript();
    const chat = new ChatController({ store, transcript });
    const first = chat.openConversation("a"); pending.get("a").resolve("chat a"); await first;
    store.set({ resumable: true });
    const failed = chat.openConversation("b");
    pending.get("b").reject(new Error("offline"));
    await assert.rejects(failed, /offline/);
    assert.equal(store.get().contextId, "b");
    assert.deepEqual(transcript.rendered, ["chat a", null]);
    assert.equal(transcript.cleared, 1);
    assert.equal(store.get().resumable, false);
    assert.equal(transcript.loading.at(-1), false);
    const retry = chat.openConversation("b"); pending.get("b").resolve("chat b"); await retry;
    assert.deepEqual(transcript.rendered, ["chat a", null, "chat b"]);
    assert.equal(transcript.loading.at(-1), false);
  } finally { api.getConversation = original; }
});

test("an obsolete failed load cannot clear the newly selected transcript", async () => {
  const original = api.getConversation;
  const pending = new Map();
  api.getConversation = (id) => new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
  try {
    const store = createStore({ contextId: null, systems: [], conversations: [] });
    const transcript = fakeTranscript();
    const chat = new ChatController({ store, transcript });
    const old = chat.openConversation("b");
    const current = chat.openConversation("c");
    pending.get("c").resolve({ id: "c", messages: [{ content: "chat c" }], metadata: {}, pending: [] });
    await current;
    pending.get("b").reject(new Error("late failure"));
    await old;
    assert.equal(store.get().contextId, "c");
    assert.deepEqual(transcript.rendered, ["chat c"]);
    assert.equal(transcript.cleared, 0);
    assert.equal(transcript.loading.at(-1), false);
  } finally { api.getConversation = original; }
});

test("an old same-ID failure after A-B-A success cannot clear the new transcript", async () => {
  const original = api.getConversation;
  const requests = [];
  api.getConversation = (id) => new Promise((resolve, reject) => requests.push({ id, resolve, reject })); // deliberately ignores abort
  try {
    const store = createStore({ contextId: null, systems: [], conversations: [] });
    const transcript = fakeTranscript();
    const chat = new ChatController({ store, transcript });
    const a1 = chat.openConversation("A");
    const b = chat.openConversation("B");
    const a2 = chat.openConversation("A");
    requests[2].resolve({ id: "A", messages: [{ content: "A2" }], metadata: {}, pending: [] });
    await a2;
    store.set({ resumable: true });
    requests[0].reject(new Error("late A1 failure"));
    await a1;
    assert.equal(store.get().contextId, "A");
    assert.equal(store.get().resumable, true);
    assert.deepEqual(transcript.rendered, ["A2"]);
    assert.equal(transcript.loading.at(-1), false);
    requests[1].reject(new Error("obsolete B"));
    await b;
  } finally { api.getConversation = original; }
});

test("an old same-ID failure while A2 is pending leaves loading and resumable state alone", async () => {
  const original = api.getConversation;
  const requests = [];
  api.getConversation = (id) => new Promise((resolve, reject) => requests.push({ id, resolve, reject })); // deliberately ignores abort
  try {
    const store = createStore({ contextId: null, systems: [], conversations: [] });
    store.set({ resumable: true });
    const transcript = fakeTranscript();
    const chat = new ChatController({ store, transcript });
    const a1 = chat.openConversation("A");
    const b = chat.openConversation("B");
    const a2 = chat.openConversation("A");
    requests[0].reject(new Error("late A1 failure"));
    await a1;
    assert.equal(store.get().resumable, true);
    assert.equal(transcript.cleared, 0);
    assert.equal(transcript.loading.at(-1), true);
    requests[2].resolve({ id: "A", messages: [{ content: "A2" }], metadata: {}, pending: [] });
    await a2;
    requests[1].reject(new Error("obsolete B"));
    await b;
  } finally { api.getConversation = original; }
});

test("a reconcile that lands after the reader moved on leaves the new chat alone", async () => {
  const original = api.getConversation;
  const answer = deferredApi();
  try {
    const store = createStore({ contextId: "a", systems: [], conversations: [] });
    const transcript = fakeTranscript();
    const chat = new ChatController({ store, transcript });
    const reconcile = chat._reconcile("a", { force: true });
    store.set({ contextId: "b" });
    answer.get("a")("old chat a");
    await reconcile;

    assert.deepEqual(transcript.rendered, []);
  } finally { api.getConversation = original; }
});

test("after a delete the next chat opens on the branch last worked on", async () => {
  const originals = { get: api.getConversation, remove: api.deleteConversation };
  const answer = deferredApi();
  try {
    const store = createStore({ contextId: "gone", systems: [], conversations: [] });
    const chat = new ChatController({
      store,
      transcript: { ...fakeTranscript(), clear() {} },
      onConversationsChanged: async () => store.set({ conversations: [{ id: "root", open_id: "branch" }] }),
    });
    api.deleteConversation = async () => ({});
    const removed = chat.deleteConversation("gone");
    await new Promise((resolve) => setTimeout(resolve));
    answer.get("branch")("the branch");
    await removed;

    assert.equal(store.get().contextId, "branch");
  } finally {
    api.getConversation = originals.get;
    api.deleteConversation = originals.remove;
  }
});
