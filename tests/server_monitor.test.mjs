import { test } from "node:test";
import assert from "node:assert/strict";
import { monitorServer } from "../web_chat/js/net/server-monitor.js";
import { monitorPageLifecycle } from "../web_chat/js/net/server-monitor.js";

test("reconnects once to a new server instance across downtime without reloading the page", async () => {
  const results = [
    { instance_id: "old", phase: "running" },
    { instance_id: "old", phase: "preparing" },
    new Error("connection refused"),
    { instance_id: "new", phase: "running" },
    { instance_id: "new", phase: "running" },
  ];
  const seen = [], restarted = [];
  let unavailable = 0, calls = 0, stop;
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => { stop(); reject(new Error("monitor timed out")); }, 2000);
    stop = monitorServer({
      interval: 1,
      read: async () => {
        calls++;
        const next = results.shift();
        if (next instanceof Error) throw next;
        return next;
      },
      onStatus: (status) => {
        seen.push(status.phase);
        if (!results.length) { stop(); clearTimeout(timeout); resolve(); }
      },
      onRestart: (status) => restarted.push(status.instance_id),
      onUnavailable: () => unavailable++,
    });
  });
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(calls, 5);
  assert.equal(unavailable, 1);
  assert.deepEqual(seen, ["running", "preparing", "running", "running"]);
  assert.deepEqual(restarted, ["new"]);
});

test("stopping the monitor suppresses a response that is already in flight", async () => {
  let finish;
  const seen = [];
  const stop = monitorServer({
    read: () => new Promise((resolve) => { finish = resolve; }),
    onStatus: (status) => seen.push(status),
    onRestart: () => assert.fail("must not reconnect after stop"),
    interval: 1,
  });
  stop();
  finish({ instance_id: "old", phase: "running" });
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.deepEqual(seen, []);
});

/** A page that only says when it is hidden and shown again. */
function fakePage() {
  const listeners = new Map();
  return {
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    removeEventListener(type, fn) {
      const list = listeners.get(type) ?? [];
      if (list.includes(fn)) list.splice(list.indexOf(fn), 1);
    },
    dispatch(type, event = {}) {
      for (const fn of [...(listeners.get(type) ?? [])]) fn({ type, ...event });
    },
    count: (type) => (listeners.get(type) ?? []).length,
  };
}

/** Records how many monitors run, and how often they were stopped. */
function countingMonitor() {
  const state = { starts: 0, stops: 0 };
  state.start = () => {
    state.starts += 1;
    let stopped = false;
    return () => {
      if (stopped) return;
      stopped = true;
      state.stops += 1;
    };
  };
  return state;
}

test("a page kept in the back/forward cache keeps its monitor across pagehide and pageshow", () => {
  const page = fakePage();
  const monitor = countingMonitor();
  monitorPageLifecycle(monitor.start, page);
  assert.equal(monitor.starts, 1);

  // Put away for the back button: the page comes back as it was, so the
  // monitor that was running must not be stopped for nothing.
  page.dispatch("pagehide", { persisted: true });
  assert.equal(monitor.stops, 0);
  page.dispatch("pageshow", { persisted: true });
  assert.equal(monitor.starts, 1, "not restarted: it never stopped");
});

test("a page that is really gone stops its monitor and starts one again when restored", () => {
  const page = fakePage();
  const monitor = countingMonitor();
  const stopWatching = monitorPageLifecycle(monitor.start, page);
  assert.equal(monitor.starts, 1);

  page.dispatch("pagehide", { persisted: false });
  assert.equal(monitor.stops, 1, "a page that is gone has nothing to watch for");

  // A restored page arrives with `persisted`; a fresh load does not (it has
  // just started a monitor of its own).
  page.dispatch("pageshow", { persisted: false });
  assert.equal(monitor.starts, 1);
  page.dispatch("pageshow", { persisted: true });
  assert.equal(monitor.starts, 2, "the monitor runs again on the restored page");

  // Leaving the page for good stops the monitor it restarted, and the page's
  // own listeners go with it.
  stopWatching();
  assert.equal(monitor.stops, 2);
  assert.equal(page.count("pagehide"), 0);
  assert.equal(page.count("pageshow"), 0);
});
