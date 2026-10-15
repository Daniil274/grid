import { test } from "node:test";
import assert from "node:assert/strict";
import { createStore } from "../web_chat/js/lib/store.js";
import { createApprovalAlerts } from "../web_chat/js/ui/approval-alerts.js";

function setup({ permission = "default", contextId = "chat-a", visible = true, focused = true } = {}) {
  const store = createStore({ contextId, conversations: [] });
  const doc = {
    title: "Grid Chat",
    visibilityState: visible ? "visible" : "hidden",
    hasFocus: () => focused,
    querySelector: () => null,
  };
  const notices = [];
  class Notifier {
    static permission = permission;
    static requested = 0;
    static async requestPermission() { Notifier.requested++; return "granted"; }
    constructor(title, options) {
      this.title = title;
      this.options = options;
      this.closed = false;
      notices.push(this);
    }
    close() { this.closed = true; }
  }
  const toasts = [];
  const say = (message, options) => {
    const shown = { message, options, dismissed: false };
    toasts.push(shown);
    return () => { shown.dismissed = true; };
  };
  const opened = [];
  let focusedWindow = 0;
  const stop = createApprovalAlerts({
    store, doc, Notifier, say,
    onOpen: (id) => opened.push(id),
    focusWindow: () => focusedWindow++,
  });
  return { store, doc, Notifier, notices, toasts, opened, stop, focusedWindow: () => focusedWindow };
}

const chat = (id, awaiting = [], extra = {}) => ({ id, title: `Chat ${id}`, active: true, awaiting_approval: awaiting, ...extra });
const request = (approval_id, tool = "bash_tool") => ({ approval_id, tool, created_at: 1 });

test("a request in another chat is announced once, offers to open it and to notify", () => {
  const { store, doc, toasts, opened, Notifier } = setup();
  store.set({ conversations: [chat("chat-a"), chat("chat-b", [request("r1")], { open_id: "chat-b2" })] });
  store.set({ conversations: [chat("chat-a"), chat("chat-b", [request("r1")], { open_id: "chat-b2" })] });

  assert.equal(toasts.length, 1);
  const [shown] = toasts;
  assert.match(shown.message, /“Chat chat-b” waits for your approval: bash_tool/);
  assert.equal(shown.options.timeout, 0);
  assert.deepEqual(shown.options.actions.map((action) => action.label), ["Open chat", "Notify me"]);
  shown.options.actions[0].run();
  assert.deepEqual(opened, ["chat-b2"]);
  shown.options.actions[1].run();
  assert.equal(Notifier.requested, 1);
  assert.equal(doc.title, "(1) Approval needed · Grid Chat");

  // The offer to turn notifications on is made once.
  store.set({ conversations: [chat("chat-b", [request("r1"), request("r2", "file_write")])] });
  assert.deepEqual(toasts[1].options.actions.map((action) => action.label), ["Open chat"]);
  assert.equal(doc.title, "(2) Approval needed · Grid Chat");
});

test("the system notifies when the page is not what the user looks at", () => {
  const away = setup({ permission: "granted", visible: false });
  away.store.set({ conversations: [chat("chat-a", [request("r1")])] });
  assert.equal(away.notices.length, 1);
  const [notice] = away.notices;
  assert.equal(notice.options.body, "Chat chat-a: bash_tool");
  assert.equal(notice.options.tag, "approval-r1");
  notice.onclick();
  assert.equal(away.focusedWindow(), 1);
  assert.deepEqual(away.opened, ["chat-a"]);
  assert.equal(notice.closed, true);

  // On screen, in this very chat: the toast and the row are enough.
  const here = setup({ permission: "granted" });
  here.store.set({ conversations: [chat("chat-a", [request("r1")])] });
  assert.equal(here.notices.length, 0);
  assert.deepEqual(here.toasts[0].options.actions.map((action) => action.label), ["Show"]);

  // Another chat while the user looks at this one: notified too.
  here.store.set({ conversations: [chat("chat-a", [request("r1")]), chat("chat-b", [request("r2")])] });
  assert.equal(here.notices.length, 1);
});

test("an answered or expired request takes its toast, notification and title mark away", () => {
  const { store, doc, toasts, notices, stop } = setup({ permission: "granted", visible: false });
  store.set({ conversations: [chat("chat-a", [request("r1")]), chat("chat-b", [request("r2")])] });
  store.set({ conversations: [chat("chat-a"), chat("chat-b", [request("r2")])] });
  assert.equal(toasts[0].dismissed, true);
  assert.equal(notices[0].closed, true);
  assert.equal(toasts[1].dismissed, false);
  assert.equal(doc.title, "(1) Approval needed · Grid Chat");
  stop();
  assert.equal(toasts[1].dismissed, true);
  assert.equal(doc.title, "Grid Chat");
});

test("a browser without notifications still gets the toast", () => {
  const store = createStore({ contextId: null, conversations: [] });
  const toasts = [];
  createApprovalAlerts({
    store,
    doc: { title: "Grid Chat", visibilityState: "hidden" },
    Notifier: undefined,
    say: (message, options) => { toasts.push(options.actions.map((a) => a.label)); return () => {}; },
    onOpen: () => {},
  });
  store.set({ conversations: [chat("chat-a", [request("r1")])] });
  assert.deepEqual(toasts, [["Open chat"]]);
});
