import test from "node:test";
import assert from "node:assert/strict";
import { ChatController } from "../web_chat/js/chat.js";
import { createStore } from "../web_chat/js/lib/store.js";
import { api } from "../web_chat/js/net/api.js";
import { installFakeDom } from "./fake_dom.mjs";
import { createMessage } from "../web_chat/js/ui/message.js";

installFakeDom();

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
