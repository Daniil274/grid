/**
 * The rail's project tree and chats (web_chat/js/ui/sidebar.js) and the project
 * a new chat starts in (web_chat/js/chat.js). The projects API is stubbed - this is the
 * UI's behaviour against the spec's contract, before the server routes exist.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { installFakeDom } from "./fake_dom.mjs";

installFakeDom();

const { createStore } = await import("../web_chat/js/lib/store.js");
const { createConversationList, PROJECT_PREVIEW } = await import("../web_chat/js/ui/sidebar.js");
const { ChatController } = await import("../web_chat/js/chat.js");
const { api } = await import("../web_chat/js/net/api.js");

const el = (tag) => document.createElement(tag);
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

const PROJECTS = [
  { id: "p1", title: "Grid", parent_id: null },
  { id: "p2", title: "Backend", parent_id: "p1" },
  { id: "p3", title: "Frontend", parent_id: "p1" },
];

const CONVERSATIONS = [
  { id: "c1", title: "Chat one", project: "p2", updated_at: "2026-10-09T10:00:00", message_count: 2 },
  { id: "c2", title: "Chat two", project: null, updated_at: "2026-10-08T10:00:00", message_count: 2 },
  { id: "c3", title: "Chat three", project: "p1", updated_at: "2026-10-07T10:00:00", message_count: 2 },
];

function mount(store, hooks = {}) {
  const container = el("div");
  const searchInput = el("input");
  createConversationList({
    container,
    searchInput,
    store,
    onSelect: () => {},
    onRename: async () => {},
    onDelete: () => {},
    onMoveToProject: async () => {},
    ...hooks,
  });
  return { container, searchInput };
}

const buttonByText = (root, text) =>
  root.querySelectorAll("button").find((button) => button.textContent.trim() === text);

const buttonByLabel = (root, label) =>
  root.querySelectorAll("button").find((button) => button.getAttribute("aria-label") === label);

const rowByTitle = (root, title) =>
  root.findAll("chatRow").find((row) => row.find("chatRow__title")?.textContent === title) ?? null;

const names = (root) => root.findAll("railProject__name").map((node) => node.textContent);

const project = (root, title) =>
  root.findAll("railProject").find((node) => node.find("railProject__name")?.textContent === title);

const menu = () => document.body.find("railMenu");
const menuItem = (text) => menu()?.findAll("railMenu__item").find((item) => item.textContent.includes(text));

function baseStore(extra = {}) {
  return createStore({
    conversations: CONVERSATIONS.map((c) => ({ ...c })),
    contextId: "c2",
    rootId: "c2",
    projects: PROJECTS.map((p) => ({ ...p })),
    activeProject: null,
    ...extra,
  });
}

const search = (searchInput, text) => {
  searchInput.value = text;
  for (const handler of searchInput.listeners.input ?? []) handler();
};

test("projects come first as a tree, the chats of no project after them", () => {
  globalThis.localStorage?.clear?.();
  const store = baseStore();
  const { container } = mount(store);

  // Folded at first: the roots only, each with every chat under it counted.
  assert.deepEqual(names(container), ["Grid"]);
  assert.equal(project(container, "Grid").find("railProject__count").textContent, "2");
  const chats = container.find("railSection--chats");
  assert.ok(rowByTitle(chats, "Chat two"));
  assert.equal(rowByTitle(container, "Chat one"), null);
});

test("opening a project shows its sub-projects and chats; the chat on screen opens its branch", () => {
  const store = baseStore();
  const { container } = mount(store);

  buttonByLabel(container, "Expand Grid").click();
  assert.deepEqual(names(container), ["Grid", "Backend", "Frontend"]);
  assert.ok(rowByTitle(project(container, "Grid"), "Chat three"));
  buttonByLabel(container, "Collapse Grid").click();
  assert.deepEqual(names(container), ["Grid"]);

  // The chat on screen lives in Backend: its branch opens by itself.
  store.set({ contextId: "c1", rootId: "c1" });
  assert.ok(rowByTitle(project(container, "Backend"), "Chat one").classList.contains("is-active"));
});

test("picking a project makes it the one new chats start in, and opening a chat follows its project", () => {
  const store = baseStore();
  const selected = [];
  const { container } = mount(store, { onSelect: (id) => selected.push(id) });

  project(container, "Grid").find("railProject__main").click();
  assert.equal(store.get().activeProject, "p1");
  assert.ok(project(container, "Grid").find("railProject__row").classList.contains("is-active"));

  rowByTitle(container, "Chat two").find("chatRow__main").click();
  assert.deepEqual(selected, ["c2"]);
  assert.equal(store.get().activeProject, null);
});

test("+ on a project starts a chat in it", () => {
  const store = baseStore();
  const started = [];
  const { container } = mount(store, { onNewChat: (id) => started.push(id) });

  buttonByLabel(container, "New chat in Grid").click();

  assert.equal(store.get().activeProject, "p1");
  assert.deepEqual(started, ["p1"]);
});

test("a chat's menu moves it to a project of the tree", async () => {
  const store = baseStore();
  const moved = [];
  const { container } = mount(store, { onMoveToProject: async (id, projectId) => moved.push([id, projectId]) });

  buttonByLabel(rowByTitle(container, "Chat two"), "Actions for Chat two").click();
  menuItem("Move to project").click();
  assert.deepEqual(
    menu().findAll("railMenu__label").map((label) => label.textContent.trim()),
    ["Grid", "Backend", "Frontend"],
  );
  menuItem("Backend").click();
  await tick();

  assert.deepEqual(moved, [["c2", "p2"]]);
  assert.equal(menu(), null);
});

test("a chat's menu can make a project and move the chat into it", async () => {
  const store = baseStore();
  const moved = [];
  const created = [];
  const { container } = mount(store, {
    onCreateProject: async (title) => { created.push(title); return { id: "p9", title }; },
    onMoveToProject: async (id, projectId) => moved.push([id, projectId]),
  });

  buttonByLabel(rowByTitle(container, "Chat two"), "Actions for Chat two").click();
  menuItem("Move to project").click();
  menu().find("railMenu__input").value = "  Ideas ";
  buttonByText(menu(), "Create").click();
  await tick();
  await tick();

  assert.deepEqual(created, ["Ideas"]);
  assert.deepEqual(moved, [["c2", "p9"]]);
});

test("a chat dropped on a project moves there; dropped on Chats it leaves its project", async () => {
  const store = baseStore();
  const moved = [];
  const { container } = mount(store, { onMoveToProject: async (id, projectId) => moved.push([id, projectId]) });
  const transfer = (id) => ({ types: ["application/x-grid-chat"], getData: () => id, dropEffect: "" });
  const fire = (node, type, dataTransfer) => {
    for (const handler of node.listeners[type] ?? []) handler({ dataTransfer, preventDefault() {} });
  };

  fire(project(container, "Grid").find("railProject__row"), "drop", transfer("c2"));
  fire(container.find("railSection--chats").find("railSection__head"), "drop", transfer("c3"));
  await tick();

  assert.deepEqual(moved, [["c2", "p1"], ["c3", null]]);
});

test("search lists matching projects and chats flat, each chat with its project", () => {
  const store = baseStore();
  const { container, searchInput } = mount(store);

  search(searchInput, "backend");

  assert.deepEqual(container.findAll("railFound__name").map((node) => node.textContent), ["Backend"]);
  assert.ok(rowByTitle(container, "Chat one"));
  assert.equal(rowByTitle(container, "Chat one").find("chatRow__where").textContent, "Backend");
  assert.equal(rowByTitle(container, "Chat two"), null);

  search(searchInput, "zzz");
  assert.ok(container.find("railHint").textContent.includes("Nothing matches"));
});

test("a long project shows its latest chats and the rest on demand", () => {
  const conversations = Array.from({ length: 9 }, (_, index) => ({
    id: `x${index}`, title: `Chat ${index}`, project: "p1", updated_at: `2026-10-0${index + 1}T10:00:00`, message_count: 1,
  }));
  const store = baseStore({ conversations, contextId: null, rootId: null });
  const { container } = mount(store);
  buttonByLabel(container, "Expand Grid").click();

  const shown = () => project(container, "Grid").childrenWith("railProject__branch")[0].childrenWith("chatRow").length;
  assert.equal(shown(), PROJECT_PREVIEW);
  assert.ok(rowByTitle(container, "Chat 8"));  // the newest first
  buttonByText(container, "Show 4 more").click();
  assert.equal(shown(), 9);
});

test("archived projects wait folded at the end", () => {
  const projects = [...PROJECTS, { id: "p4", title: "Old", parent_id: null, archived: true }];
  const store = baseStore({ projects });
  const { container } = mount(store);

  assert.deepEqual(names(container), ["Grid"]);
  buttonByText(container, "Archived (1)").click();
  assert.deepEqual(names(container), ["Grid", "Old"]);
});

test("a poll that brings the same chats back leaves the rail as it is", () => {
  const store = baseStore();
  const { container } = mount(store);
  const before = container.childNodes[0];

  // The 4-second poll while an agent works: new objects, the same chats.
  store.set({ conversations: CONVERSATIONS.map((c) => ({ ...c })) });
  assert.equal(container.childNodes[0], before);

  store.set({ conversations: CONVERSATIONS.map((c) => ({ ...c, title: c.id === "c2" ? "Renamed" : c.title })) });
  assert.notEqual(container.childNodes[0], before);
  assert.ok(rowByTitle(container, "Renamed"));
});

test("an empty chat is not listed unless it is the one on screen", () => {
  const conversations = [
    ...CONVERSATIONS.map((c) => ({ ...c })),
    { id: "e1", title: "New chat", project: null, updated_at: "2026-10-10T10:00:00", message_count: 0 },
    { id: "e2", title: "New chat", project: null, updated_at: "2026-10-10T11:00:00", message_count: 0 },
  ];
  const store = baseStore({ conversations, contextId: "e2", rootId: "e2" });
  const { container } = mount(store);

  const chats = container.find("railSection--chats");
  assert.deepEqual(chats.findAll("chatRow").map((row) => row.dataset.id), ["e2", "c2"]);
});

test("New chat on an empty chat already on screen makes no other one", async () => {
  const store = createStore({
    systemKey: null, agentKey: null, activeProject: null, contextId: "e1", rootId: "e1",
    conversations: [{ id: "e1", title: "New chat", project: null, message_count: 0 }],
  });
  const transcript = { render() {}, setLoading() {}, clear() {} };
  const chat = new ChatController({ store, transcript, onConversationsChanged: async () => {} });
  const original = api.createConversation;
  let created = 0;
  api.createConversation = async () => { created += 1; return { id: "new", workspace: null }; };
  try {
    await chat.startConversation();
    assert.equal(created, 0);
    assert.equal(store.get().contextId, "e1");
    // In another project, a new chat is made there.
    store.set({ activeProject: "p1" });
    await chat.startConversation();
    assert.equal(created, 1);
  } finally {
    api.createConversation = original;
  }
});

test("a new chat is created in the active project", async () => {
  const store = createStore({
    systemKey: "sys", agentKey: "agent", activeProject: "p2",
    contextId: null, rootId: null, conversations: [],
  });
  const transcript = { render() {}, setLoading() {}, clear() {} };
  const chat = new ChatController({ store, transcript, onConversationsChanged: async () => {} });

  const original = api.createConversation;
  let selection = null;
  api.createConversation = async (payload) => { selection = payload; return { id: "new1", workspace: null }; };
  try {
    await chat.startConversation();
  } finally {
    api.createConversation = original;
  }

  assert.equal(selection.project_id, "p2");
  assert.equal(store.get().contextId, "new1");
});

test("a new chat outside any project sends a null project_id", async () => {
  const store = createStore({
    systemKey: "sys", agentKey: "agent", activeProject: null,
    contextId: null, rootId: null, conversations: [],
  });
  const transcript = { render() {}, setLoading() {}, clear() {} };
  const chat = new ChatController({ store, transcript, onConversationsChanged: async () => {} });

  const original = api.createConversation;
  let selection = null;
  api.createConversation = async (payload) => { selection = payload; return { id: "new2", workspace: null }; };
  try {
    await chat.startConversation();
  } finally {
    api.createConversation = original;
  }

  assert.equal(selection.project_id, null);
});
