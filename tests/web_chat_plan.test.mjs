import { test } from "node:test";
import assert from "node:assert/strict";

import { installFakeDom } from "./fake_dom.mjs";

installFakeDom();
document.addEventListener = () => {};
const assigned = [];
globalThis.location = { hash: "", pathname: "/", search: "", assign: (url) => assigned.push(url) };
globalThis.history = { replaceState() {} };

const buttonLabelled = (root, label) => root.querySelectorAll("button").find((button) => button.textContent === label);

const { PlanDrawer } = await import("../web_chat/js/accounts/plan.js");
const { api } = await import("../web_chat/js/net/api.js");

const PLAN = {
  tier: "byok",
  label: "Bring your own key",
  models: ["*"],
  limits: { usd_per_day: 3, usd_per_month: null },
  spent: { day_usd: 0.4321, month_usd: 12.5 },
  vault_enabled: true,
  chatgpt_available: true,
  credentials: [
    { kind: "openrouter", label: "OpenRouter", connected: true, hint: "…mnop", status: "ok" },
    { kind: "openai", label: "OpenAI API", connected: false },
    { kind: "chatgpt", label: "ChatGPT subscription", connected: false },
  ],
};

function nodes() {
  const div = () => document.createElement("div");
  return { drawer: div(), backdrop: div(), closeButton: div(), body: div(), status: div(), openButton: Object.assign(div(), { hidden: true }) };
}

test("the drawer shows the plan, what was spent against its budgets and the keys that may be brought", async () => {
  api.plan = async () => PLAN;
  const view = nodes();
  const drawer = new PlanDrawer(view);

  await drawer.open();

  const text = view.body.textContent;
  assert.match(text, /Bring your own key/);
  assert.match(text, /Today: \$0\.4321 of \$3\.00/);
  assert.match(text, /This month: \$12\.50 \(no limit\)/);
  assert.match(text, /…mnop/);
  assert.match(text, /Check and save/);
  assert.match(text, /Sign in with ChatGPT/);
  assert.ok(!text.includes("sk-"), "no secret is ever shown");
});

test("a key is sent once and the list is read again after it", async () => {
  const calls = [];
  api.plan = async () => (calls.push("plan"), PLAN);
  api.storeKey = async (kind, secret) => calls.push(`store ${kind} ${secret}`);
  const view = nodes();
  const drawer = new PlanDrawer(view);
  await drawer.open();

  const input = view.body.querySelector("input");
  assert.ok(input, "the key field is rendered");
  input.value = "  sk-aaaaaaaaaaaaaaaa ";
  buttonLabelled(view.body, "Check and save").click();
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.ok(calls.includes("store openai sk-aaaaaaaaaaaaaaaa"), "the key is trimmed and sent to its provider");
  assert.equal(calls.filter((call) => call === "plan").length, 2);
});

test("signing in with ChatGPT leaves for the address the server gave", async () => {
  api.plan = async () => PLAN;
  api.connectChatGPT = async () => ({ url: "https://auth.openai.com/api/accounts/authorize?x=1" });
  const view = nodes();
  await new PlanDrawer(view).open();
  buttonLabelled(view.body, "Sign in with ChatGPT").click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(assigned, ["https://auth.openai.com/api/accounts/authorize?x=1"]);
});

test("a server without credentials offers no button", async () => {
  api.plan = async () => {
    throw new Error("not found");
  };
  const view = nodes();
  await new PlanDrawer(view).start();
  assert.equal(view.openButton.hidden, true);
});
