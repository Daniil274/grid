import { test } from "node:test";
import assert from "node:assert/strict";
import { monitorServer } from "../web_chat/js/net/server-monitor.js";

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
