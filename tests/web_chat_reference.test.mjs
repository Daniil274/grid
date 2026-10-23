/**
 * The user-facing reference-only migration path: the rail action's dry-run
 * report, the owner's explicit confirmation of the bound token, the target
 * opened afterwards, and the persistent source link on the new empty chat.
 * Pure UI behaviour with a stubbed API - the server's own gates are covered by
 * tests/test_context_migration.py.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { installFakeDom } from "./fake_dom.mjs";

installFakeDom();
document.addEventListener ??= () => {};
document.removeEventListener ??= () => {};
document.body.replaceChildren = function replaceChildren(...nodes) {
  for (const child of [...this.childNodes]) child.remove();
  this.append(...nodes);
};

const { api } = await import("../web_chat/js/net/api.js");
const { ChatController } = await import("../web_chat/js/chat.js");
const { createStore } = await import("../web_chat/js/lib/store.js");
const { createConversationList } = await import("../web_chat/js/ui/sidebar.js");
const { migrationFacts, openMigrationDialog, REFERENCE_SOURCE_PATH, sourceConversationId } = await import(
  "../web_chat/js/ui/migration-dialog.js"
);
const { renderReferenceBanner, sourceTitleOf } = await import("../web_chat/js/ui/reference-banner.js");

const el = (tag) => document.createElement(tag);

const readyReport = () => ({
  action: "context.reference_bootstrap",
  version: 1,
  status: "ready",
  code: "migration.ready",
  reason: "A new empty conversation can be pinned and the old history kept as reference data only.",
  source: {
    context_id: "old-chat",
    provenance: "unknown",
    revision_status: "none",
    messages: 42,
    executions: 7,
  },
  target: { kind: "new-context", context_id: null, title: "Old chat (reference)", reference_only: true, history: "empty" },
  reference: {
    executable: false,
    replay: false,
    history: { selected: true, messages: 42, executions: 7, kept_in: "old-chat" },
    artifacts: { workspace: "referenced-not-copied", copied: false },
    // The server's own keys (core.migration._excluded): the counts the dialog
    // must read, not an older shape.
    excluded: { pending_messages: 2, pending_agent_run: "waiting" },
  },
  confirmation: { required: true, token: "bound-preview-token-123456", imports_executable_state: false },
  limitations: { provenance: "unknown", executable_state: "none", history_imported: false, original_preserved: true },
  effects: { model_calls: 0, tool_calls: 0, replays: 0 },
});

test("the report says the source provenance and what is never imported or restored", () => {
  const facts = migrationFacts(readyReport(), "Old chat");
  const text = facts.map((fact) => `${fact.label}: ${fact.text}`).join("\n");
  assert.match(text, /Source: Old chat — provenance unknown \(legacy chat\)/);
  assert.match(text, /History: referenced only/);
  assert.match(text, /Not imported:/);
  assert.match(text, /No replay:/);
  assert.match(text, /No workspace restore:/);
  assert.match(text, /nothing is replayed or re-run/);
  assert.match(text, /files stay where they are|no workspace files are copied or restored/);
});

test("the facts count queues and a pending run by the server's actual keys", () => {
  const serverReport = readyReport();
  serverReport.reference.excluded = { pending_messages: 3, pending_agent_run: "waiting" };
  const text = migrationFacts(serverReport, "Old chat")
    .map((fact) => `${fact.label}: ${fact.text}`)
    .join("\n");
  assert.match(text, /queues \(3\)/);
  assert.match(text, /pending runs \(1\)/);

  // A missing pending run is none - the dialog never invents one.
  serverReport.reference.excluded = { pending_messages: 0, pending_agent_run: null };
  const none = migrationFacts(serverReport, "Old chat")
    .map((fact) => `${fact.label}: ${fact.text}`)
    .join("\n");
  assert.match(none, /queues \(0\) or pending runs \(0\)/);
});

test("the source of a reference copy is the row's active branch, not the root", () => {
  assert.equal(sourceConversationId({ id: "root", open_id: "branch-2" }), "branch-2");
  assert.equal(sourceConversationId({ id: "root" }), "root");
  assert.equal(sourceConversationId({}), "");
});

test("ready: the owner must tick the consent before the bound token is POSTed, then the target opens", async () => {
  const calls = [];
  let opened = null;
  const dialog = openMigrationDialog({
    report: readyReport(),
    sourceTitle: "Old chat",
    apply: async (token) => {
      calls.push(token);
      return { ...readyReport(), status: "applied", code: "migration.applied", target: { context_id: "new-chat", reference_only: true } };
    },
    onOpened: (id) => { opened = id; },
  });
  assert.ok(dialog);
  const submit = dialog.querySelectorAll("button").find((button) => button.textContent === "Create reference-only chat");
  const checkbox = dialog.querySelector("input");
  assert.ok(submit);
  assert.equal(submit.disabled, true);
  assert.equal(checkbox.checked, undefined);

  checkbox.checked = true;
  checkbox.listeners.change[0]();
  assert.equal(submit.disabled, false);

  const form = dialog.querySelector("form");
  let prevented = false;
  form.listeners.submit[0]({ preventDefault: () => { prevented = true; } });
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.equal(prevented, true);
  assert.deepEqual(calls, ["bound-preview-token-123456"]);
  assert.equal(opened, "new-chat");
  assert.equal(document.body.querySelector("dialog"), null);
});

test("blocked: the reason is shown and there is nothing to confirm", () => {
  const dialog = openMigrationDialog({
    report: {
      status: "blocked",
      code: "migration.source_tampered",
      reason: "Conversation's revision manifest was altered after pinning; refusing to use it even as a reference.",
      source: { context_id: "old-chat", provenance: "pinned" },
    },
    sourceTitle: "Old chat",
  });
  const text = dialog.textContent;
  assert.match(text, /refusing to use it even as a reference/);
  assert.equal(dialog.querySelectorAll("button").some((button) => button.textContent === "Create reference-only chat"), false);
  assert.equal(dialog.querySelector("input"), null);
});

test("completed: an existing target is opened without another POST", () => {
  let opened = null;
  const dialog = openMigrationDialog({
    report: {
      ...readyReport(),
      status: "completed",
      code: "migration.completed",
      target: { context_id: "existing-target", reference_only: true },
      confirmation: { required: false, token: null },
    },
    sourceTitle: "Old chat",
    apply: () => { throw new Error("must not POST"); },
    onOpened: (id) => { opened = id; },
  });
  const open = dialog.querySelectorAll("button").find((button) => button.textContent === "Open reference-only chat");
  assert.ok(open);
  open.click();
  assert.equal(opened, "existing-target");
});

test("a stale token keeps the dialog open with the server's explanation", async () => {
  const dialog = openMigrationDialog({
    report: readyReport(),
    sourceTitle: "Old chat",
    apply: async () => {
      throw Object.assign(new Error("POST failed (409)"), {
        payload: { status: "blocked", code: "migration.confirmation_stale", reason: "The old chat changed since the preview." },
      });
    },
    onOpened: () => { throw new Error("must not open"); },
  });
  const checkbox = dialog.querySelector("input");
  checkbox.checked = true;
  checkbox.listeners.change[0]();
  dialog.querySelector("form").listeners.submit[0]({ preventDefault() {} });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.match(dialog.textContent, /changed since the preview/);
  assert.equal(dialog.parentNode, document.body);
});

test("the target's banner links to the old chat in a new tab and says what was not carried over", () => {
  const container = el("div");
  const migration = {
    action: "context.reference_bootstrap",
    source_context_id: "old-chat",
    source_provenance: "unknown",
    executable_state: "none",
    reference: { history: { messages: 42 } },
  };
  renderReferenceBanner({
    container,
    migration,
    conversations: [{ id: "old-chat", title: "Old chat" }, { id: "new-chat", title: "Old chat (reference)" }],
  });
  assert.equal(container.hidden, false);
  const link = container.querySelector("a");
  assert.equal(link.getAttribute("href"), REFERENCE_SOURCE_PATH("old-chat"));
  assert.equal(link.getAttribute("target"), "_blank");
  assert.match(container.textContent, /Reference-only chat/);
  assert.match(container.textContent, /source provenance: unknown/);
  assert.match(container.textContent, /referenced only/);
  assert.match(container.textContent, /not imported, not replayed/);
  assert.match(container.textContent, /no workspace files are restored/);
  assert.match(container.textContent, /old instructions and tools are not carried over/);

  const other = el("div");
  renderReferenceBanner({ container: other, migration: null, conversations: [] });
  assert.equal(other.hidden, true);
  assert.equal(other.textContent, "");
});

test("the source title follows a branch to its family row", () => {
  assert.equal(sourceTitleOf({ source_context_id: "branch-1" }, [{ id: "root", open_id: "branch-1", title: "Old chat" }]), "Old chat");
  assert.equal(sourceTitleOf({ source_context_id: "gone" }, []), "");
});

test("the chat controller exposes the target's migration record to the banner", async () => {
  const original = api.getConversation;
  api.getConversation = async () => ({
    id: "new-chat",
    root: "new-chat",
    messages: [],
    pending: [],
    metadata: {
      migration: { action: "context.reference_bootstrap", source_context_id: "old-chat", executable_state: "none" },
    },
  });
  try {
    const store = createStore({ contextId: null, conversations: [] });
    const transcript = {
      hasPlan: () => false,
      render() {},
      setLoading() {},
      clear() {},
      adopt: () => false,
      resumable: () => false,
      lastAnswer: () => null,
      retireContinue() {},
      collapsePlans() {},
    };
    const chat = new ChatController({ store, transcript, onConversationsChanged: async () => {} });
    await chat.openConversation("new-chat");
    assert.equal(store.get().reference.action, "context.reference_bootstrap");
    assert.equal(store.get().reference.source_context_id, "old-chat");
  } finally {
    api.getConversation = original;
  }
});

test("the API previews with GET and confirms with the bound token in a POST body", async () => {
  const original = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url, method: options.method ?? "GET", body: options.body });
    return { ok: true, status: 200, json: async () => ({ status: "ready" }) };
  };
  try {
    await api.migrationPreview("old chat");
    await api.migrationApply("old chat", "token-bound-to-source");
  } finally {
    globalThis.fetch = original;
  }
  assert.equal(requests[0].url, "/api/chat/conversations/old%20chat/migration");
  assert.equal(requests[0].method, "GET");
  assert.equal(requests[1].method, "POST");
  assert.deepEqual(JSON.parse(requests[1].body), { plan_token: "token-bound-to-source", confirm: true });
});

test("the rail's chat menu offers the preview action and hands it the conversation", () => {
  const conversation = { id: "old-chat", title: "Old chat", updated_at: "2026-10-08T10:00:00", message_count: 42 };
  const store = createStore({ conversations: [conversation], contextId: null, projects: [] });
  const container = el("div");
  let asked = null;
  createConversationList({
    container,
    searchInput: el("input"),
    store,
    onSelect: () => {},
    onRename: async () => {},
    onDelete: () => {},
    onReferenceCopy: (item) => { asked = item; },
  });
  const more = container.querySelectorAll("button").find(
    (button) => button.getAttribute("aria-label") === "Actions for Old chat",
  );
  assert.ok(more);
  more.click();
  const item = document.body.findAll("railMenu__item").find((node) => node.textContent.includes("Reference-only copy"));
  assert.ok(item);
  item.click();
  assert.equal(asked?.id, "old-chat");
});
