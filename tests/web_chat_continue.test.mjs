/**
 * Carrying context between chats: the seed card a chat shows when it was seeded
 * from another chat (web_chat/js/ui/message.js kind="context"), the "Continue in
 * new chat" action on the newest answer (web_chat/js/ui/transcript.js), and the
 * controller call that opens the new chat (web_chat/js/chat.js). The API is
 * stubbed: this is the UI's behaviour, not the server's.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { installFakeDom } from "./fake_dom.mjs";

installFakeDom();
// The transcript wires page-level listeners the fake DOM does not model.
document.addEventListener ??= () => {};
document.removeEventListener ??= () => {};

const { createMessage } = await import("../web_chat/js/ui/message.js");
const { createTranscript } = await import("../web_chat/js/ui/transcript.js");
const { ChatController } = await import("../web_chat/js/chat.js");
const { createStore } = await import("../web_chat/js/lib/store.js");
const { api } = await import("../web_chat/js/net/api.js");

const el = (tag) => document.createElement(tag);

function transcript(hooks = {}) {
  const container = el("div");
  const emptyState = el("div");
  const jumpButton = el("button");
  return { container, t: createTranscript({ container, emptyState, jumpButton, ...hooks }) };
}

/** A transcript stand-in for the controller tests (see web_chat_open.test.mjs). */
const fakeTranscript = () => ({
  lastAnswer: () => null,
  render() {},
  setLoading() {},
  clear() {},
  adopt: () => false,
  resumable: () => false,
  refreshContinueInNewChat() {},
});

test("a carried-context seed is a collapsed, uneditable card that opens on click", () => {
  const message = createMessage({
    role: "user",
    kind: "context",
    content: "## Plan\n\n- do a thing",
    context: { source_title: "Chat about billing" },
    messageId: "m1",
  });
  const root = message.el;
  assert.match(root.find("msg__contextLabel").textContent, /Context carried from Chat about billing/);
  const body = root.find("msg__contextBody");
  assert.equal(body.hidden, true);
  // Read-only: no editor, no textarea, and never a tool result.
  assert.equal(root.find("msg__editor"), null);
  assert.equal(root.querySelectorAll("textarea").length, 0);

  root.find("msg__contextHead").click();
  assert.equal(body.hidden, false);
  assert.equal(root.find("msg__contextHead").getAttribute("aria-expanded"), "true");
  root.find("msg__contextHead").click();
  assert.equal(body.hidden, true);
});

test("the seed's source falls back to the project or a generic chat", () => {
  const project = createMessage({ role: "user", kind: "context", content: "x", context: { project: "p1" } });
  assert.match(project.el.find("msg__contextLabel").textContent, /the project p1/);
  const bare = createMessage({ role: "user", kind: "context", content: "x" });
  assert.match(bare.el.find("msg__contextLabel").textContent, /an earlier chat/);
});

test("only the newest answer offers Continue in new chat", () => {
  const { t } = transcript({ onContinueInNewChat: () => {} });
  const first = t.add({ role: "assistant", content: "first", messageId: "m1" });
  const second = t.add({ role: "assistant", content: "second", messageId: "m2" });
  const boxOf = (handle) => handle.el.find("msg__newChat");
  assert.equal(boxOf(first).hidden, true);
  assert.equal(boxOf(second).hidden, false);

  // A newer user message does not take it away; a newer answer would.
  t.add({ role: "user", content: "again", messageId: "m3" });
  assert.equal(boxOf(second).hidden, false);
});

test("clicking Continue in new chat hands the stored answer to the hook", () => {
  const calls = [];
  const { t } = transcript({ onContinueInNewChat: (id) => calls.push(id) });
  const answer = t.add({ role: "assistant", content: "the plan", messageId: "m9" });
  answer.el.find("msg__continueNew").click();
  assert.deepEqual(calls, ["m9"]);
});

test("continueInNewChat asks the API and opens the new chat", async () => {
  const store = createStore({ contextId: "c1", rootId: "c1", conversations: [] });
  const seen = [];
  const saved = { continueConversation: api.continueConversation, getConversation: api.getConversation, conversationPlan: api.conversationPlan };
  api.continueConversation = async (id, strategy) => {
    seen.push([id, strategy]);
    return { id: "c-new", project_id: null };
  };
  api.getConversation = async (id) => ({ id, root: id, messages: [], metadata: {}, pending: [] });
  api.conversationPlan = async () => ({ tasks: [] });
  try {
    const chat = new ChatController({ store, transcript: fakeTranscript(), onConversationsChanged: async () => {} });
    await chat.continueInNewChat();
    assert.deepEqual(seen, [["c1", "plan"]]);
    assert.equal(store.get().contextId, "c-new");
  } finally {
    api.continueConversation = saved.continueConversation;
    api.getConversation = saved.getConversation;
    api.conversationPlan = saved.conversationPlan;
  }
});

test("continueInNewChat refuses while a turn is streaming", async () => {
  const store = createStore({ contextId: "c1", streaming: true, conversations: [] });
  const original = api.continueConversation;
  let called = 0;
  api.continueConversation = async () => {
    called += 1;
    return { id: "x" };
  };
  try {
    const chat = new ChatController({ store, transcript: fakeTranscript(), onConversationsChanged: async () => {} });
    await chat.continueInNewChat();
    assert.equal(called, 0);
  } finally {
    api.continueConversation = original;
  }
});
