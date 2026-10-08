/**
 * The projects rail (web_chat/js/ui/sidebar.js) and the project a new chat
 * starts in (web_chat/js/chat.js). The projects API is stubbed - this is the
 * UI's behaviour against the spec's contract, before the server routes exist.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { installFakeDom } from "./fake_dom.mjs";

installFakeDom();

const { createStore } = await import("../web_chat/js/lib/store.js");
const { createConversationList } = await import("../web_chat/js/ui/sidebar.js");
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
  { id: "c1", title: "Chat one", project: "p2", updated_at: "2026-10-09T10:00:00" },
  { id: "c2", title: "Chat two", project: null, updated_at: "2026-10-08T10:00:00" },
  { id: "c3", title: "Chat three", project: "p1", updated_at: "2026-10-07T10:00:00" },
];

function mount(store, hooks = {}) {
  const container = el("div");
  const searchInput = el("input");
  const modeToggle = el("div");
  createConversationList({
    container,
    searchInput,
    modeToggle,
    store,
    onSelect: () => {},
    onRename: async () => {},
    onDelete: () => {},
    onMoveToProject: async () => {},
    ...hooks,
  });
  return { container, searchInput, modeToggle };
}

const buttonByText = (root, text) =>
  root.querySelectorAll("button").find((button) => button.textContent.trim() === text);

const buttonByLabel = (root, label) =>
  root.querySelectorAll("button").find((button) => button.getAttribute("aria-label") === label);

const rowByTitle = (root, title) =>
  root.findAll("chatRow").find((row) => row.find("chatRow__title")?.textContent === title) ?? null;

const names = (root) => root.findAll("projectGroup__name").map((node) => node.textContent);

const project = (root, title) =>
  root.findAll("projectGroup").find((group) => group.find("projectGroup__name")?.textContent === title);

function baseStore() {
  return createStore({
    conversations: CONVERSATIONS.map((c) => ({ ...c })),
    contextId: "c1",
    rootId: "c1",
    projects: PROJECTS.map((p) => ({ ...p })),
    activeProject: null,
  });
}

test("the rail groups by date until the Projects mode is chosen", () => {
  const store = baseStore();
  const { container, modeToggle } = mount(store);

  // Default: date buckets, no project tree.
  assert.ok(container.find("chatGroup__label"));
  assert.equal(container.find("projectGroup"), null);

  buttonByText(modeToggle, "Projects").click();

  assert.deepEqual(names(container), ["Grid", "Backend", "Frontend"]);
  // "No project" sits last and holds the unfiled chat.
  const none = container.find("projectGroup--none");
  assert.ok(none);
  assert.equal(none.find("chatGroup__label").textContent, "No project");
  assert.ok(rowByTitle(none, "Chat two"));
  // Each project shows its own chats.
  assert.ok(rowByTitle(container, "Chat one"));
  assert.ok(rowByTitle(container, "Chat three"));
});

test("collapsing a project hides its chats and its subprojects", () => {
  const store = baseStore();
  const { container, modeToggle } = mount(store);
  buttonByText(modeToggle, "Projects").click();

  project(container, "Grid").find("projectGroup__chevron").click();

  assert.deepEqual(names(container), ["Grid"]);
  assert.equal(rowByTitle(container, "Chat three"), null);
  assert.equal(rowByTitle(container, "Chat one"), null);
});

test("clicking a project makes it the active one for new chats", () => {
  const store = baseStore();
  const { container, modeToggle } = mount(store);
  buttonByText(modeToggle, "Projects").click();

  project(container, "Backend").find("projectGroup__title").click();

  assert.equal(store.get().activeProject, "p2");
  assert.ok(project(container, "Backend").find("projectGroup__head").classList.contains("is-active"));
});

test("the No project group makes new chats start outside any project", () => {
  const store = baseStore();
  store.set({ activeProject: "p2" });
  const { container, modeToggle } = mount(store);
  buttonByText(modeToggle, "Projects").click();

  const label = container.find("projectGroup--none").find("chatGroup__label");
  assert.equal(label.textContent, "No project");
  label.click();

  assert.equal(store.get().activeProject, null);
  // It now reads as the active bucket, like a project head does when chosen.
  assert.ok(
    container.find("projectGroup--none").find("chatGroup__label").classList.contains("is-active"),
  );
});

test("the move popover lists projects and moves the chat to one", async () => {
  const store = baseStore();
  const moved = [];
  const { container } = mount(store, { onMoveToProject: async (id, projectId) => moved.push([id, projectId]) });

  // The row action opens the popover.
  buttonByLabel(rowByTitle(container, "Chat one"), "Move to project").click();
  const pop = container.find("movePop");
  assert.ok(pop);
  assert.deepEqual(
    pop.findAll("movePop__item").map((item) => item.textContent),
    ["No project", "Grid", "Backend", "Frontend"],
  );

  buttonByText(pop, "Backend").click();
  await tick();

  assert.deepEqual(moved, [["c1", "p2"]]);
  assert.equal(container.find("movePop"), null);
});

test("the move popover can create a project and move the chat into it", async () => {
  const store = baseStore();
  const moved = [];
  const created = [];
  const { container } = mount(store, {
    onCreateProject: async (title) => { created.push(title); return { id: "p9", title }; },
    onMoveToProject: async (id, projectId) => moved.push([id, projectId]),
  });

  buttonByLabel(rowByTitle(container, "Chat one"), "Move to project").click();
  const pop = container.find("movePop");
  pop.find("movePop__new").value = "  Ideas ";
  buttonByText(pop, "Create").click();
  await tick();

  assert.deepEqual(created, ["Ideas"]);
  assert.deepEqual(moved, [["c1", "p9"]]);
});

test("search matches a project name as well as a chat title", () => {
  const store = baseStore();
  const { container, searchInput, modeToggle } = mount(store);
  buttonByText(modeToggle, "Projects").click();

  searchInput.value = "backend";
  for (const handler of searchInput.listeners.input ?? []) handler();

  assert.deepEqual(names(container), ["Grid", "Backend"]);
  assert.ok(rowByTitle(container, "Chat one"));
  assert.equal(rowByTitle(container, "Chat two"), null);
  assert.equal(rowByTitle(container, "Chat three"), null);
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
