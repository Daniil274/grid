import test from "node:test";
import assert from "node:assert/strict";
import { ChatController } from "../web_chat/js/chat.js";
import { createStore } from "../web_chat/js/lib/store.js";
import { api } from "../web_chat/js/net/api.js";

const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
function setup() {
  const store = createStore({ contextId: "home", conversations: [] });
  const transcript = { rendered: [], render(messages) { this.rendered.push(messages[0].content); }, setLoading() {}, clear() {}, resumable: () => false };
  const chat = new ChatController({ store, transcript, onConversationsChanged: async () => {}, speech: { stop() {} } });
  const loads = new Map();
  api.getConversation = (id) => { const d = deferred(); loads.set(id, d); return d.promise; };
  const payload = (id) => ({ id, messages: [{ content: id }], metadata: {}, pending: [] });
  return { chat, store, transcript, loads, payload };
}

test("ordinary open wins over pending switch activation (X switch then Y open)", async () => {
  const old = { activate: api.activateBranch, get: api.getConversation };
  const env = setup(), activation = deferred(); api.activateBranch = () => activation.promise;
  try {
    const switching = env.chat.switchVersion("X");
    const opening = env.chat.openConversation("Y");
    env.loads.get("Y").resolve(env.payload("Y")); await opening;
    activation.resolve({}); await switching;
    assert.equal(env.store.get().contextId, "Y"); assert.deepEqual(env.transcript.rendered, ["Y"]);
  } finally { api.activateBranch = old.activate; api.getConversation = old.get; }
});

test("latest of two switch activations navigates despite reversed completion", async () => {
  const old = { activate: api.activateBranch, get: api.getConversation };
  const env = setup(), x = deferred(), y = deferred();
  api.activateBranch = (id) => id === "X" ? x.promise : y.promise;
  try {
    const sx = env.chat.switchVersion("X"), sy = env.chat.switchVersion("Y");
    y.resolve({}); await new Promise((r) => setImmediate(r)); env.loads.get("Y").resolve(env.payload("Y")); await sy;
    // let the Y transcript finish before completing the obsolete activation
    await new Promise((r) => setImmediate(r));
    x.resolve({}); await sx;
    assert.equal(env.store.get().contextId, "Y"); assert.deepEqual(env.transcript.rendered, ["Y"]);
  } finally { api.activateBranch = old.activate; api.getConversation = old.get; }
});

test("switch opens normally and current activation rejection is handled", async () => {
  const old = { activate: api.activateBranch, get: api.getConversation };
  const oldDocument = globalThis.document;
  const oldNode = globalThis.Node;
  globalThis.Node = class {};
  const node = () => ({ classList: { add() {} }, dataset: {}, style: {}, append() {}, setAttribute() {}, addEventListener() {} });
  globalThis.document = { createElement: node, createElementNS: node, createTextNode: node, body: { append() {} } };
  const env = setup(); api.activateBranch = async () => ({});
  try {
    const switched = env.chat.switchVersion("B"); await new Promise((r) => setImmediate(r));
    env.loads.get("B").resolve(env.payload("B")); await switched;
    assert.equal(env.store.get().contextId, "B");
    api.activateBranch = async () => { throw new Error("denied"); };
    await env.chat.switchVersion("C");
    assert.equal(env.store.get().contextId, "B");
  } finally { api.activateBranch = old.activate; api.getConversation = old.get; globalThis.document = oldDocument; globalThis.Node = oldNode; }
});

test("A to B to A opens remain latest by intent, not context identity", async () => {
  const old = api.getConversation; const env = setup();
  try {
    const a1 = env.chat.openConversation("A"), aFirst = env.loads.get("A");
    const b = env.chat.openConversation("B"), bLoad = env.loads.get("B");
    const a2 = env.chat.openConversation("A"), aLast = env.loads.get("A");
    aFirst.resolve(env.payload("A")); await a1;
    bLoad.resolve(env.payload("B")); await b;
    aLast.resolve(env.payload("A")); await a2;
    assert.deepEqual(env.transcript.rendered, ["A"]);
  } finally { api.getConversation = old; }
});

test("new open invalidates stale switch rejection", async () => {
  const old = { activate: api.activateBranch, get: api.getConversation };
  const env = setup(), activation = deferred(); api.activateBranch = () => activation.promise;
  try {
    const switching = env.chat.switchVersion("X"), opening = env.chat.openConversation("Y");
    env.loads.get("Y").resolve(env.payload("Y")); await opening;
    activation.reject(new Error("stale failure")); await switching;
    assert.equal(env.store.get().contextId, "Y");
  } finally { api.activateBranch = old.activate; api.getConversation = old.get; }
});

test("starting a chat invalidates a pending switch activation", async () => {
  const old = { activate: api.activateBranch, create: api.createConversation, get: api.getConversation };
  const env = setup(), activation = deferred(), creation = deferred();
  api.activateBranch = () => activation.promise;
  api.createConversation = () => creation.promise;
  try {
    const switching = env.chat.switchVersion("X");
    const starting = env.chat.startConversation();
    creation.resolve({ id: "new-chat" }); await starting;
    activation.resolve({}); await switching;
    assert.equal(env.store.get().contextId, "new-chat");
    assert.deepEqual(env.transcript.rendered, []);
  } finally { api.activateBranch = old.activate; api.createConversation = old.create; api.getConversation = old.get; }
});

test("a newer open wins while startConversation is waiting for its API", async () => {
  const old = { create: api.createConversation, get: api.getConversation };
  const env = setup(), creation = deferred(); api.createConversation = () => creation.promise;
  try {
    const starting = env.chat.startConversation();
    const opening = env.chat.openConversation("Y");
    env.loads.get("Y").resolve(env.payload("Y")); await opening;
    creation.resolve({ id: "late-new-chat" }); await starting;
    assert.equal(env.store.get().contextId, "Y");
    assert.deepEqual(env.transcript.rendered, ["Y"]);
  } finally { api.createConversation = old.create; api.getConversation = old.get; }
});

test("a newer open quietly supersedes an edit branch API failure", async () => {
  const old = { create: api.createBranch, get: api.getConversation };
  const env = setup(), branch = deferred(); api.createBranch = () => branch.promise;
  try {
    const editing = env.chat.editMessage({ id: "message", text: "edited" });
    const opening = env.chat.openConversation("Y");
    env.loads.get("Y").resolve(env.payload("Y")); await opening;
    branch.reject(new Error("obsolete edit failure")); await editing;
    assert.equal(env.store.get().contextId, "Y");
    assert.deepEqual(env.transcript.rendered, ["Y"]);
  } finally { api.createBranch = old.create; api.getConversation = old.get; }
});