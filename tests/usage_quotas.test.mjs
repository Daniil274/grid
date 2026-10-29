/**
 * Subscriptions and keys on the Usage page (web_chat/js/usage/quotas.js):
 * every configured allowance is a card, its windows or key limits are shown
 * with when they were read, and a source that cannot be read says why instead
 * of showing invented numbers.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { installFakeDom } from "./fake_dom.mjs";

installFakeDom();

const { renderQuotas } = await import("../web_chat/js/usage/quotas.js");

const el = (tag) => document.createElement(tag);
const NOW = 1_800_000_000;

const live = {
  id: "chatgpt",
  kind: "subscription",
  label: "ChatGPT plan",
  detail: "The ChatGPT plan of this account",
  status: "live",
  connected: true,
  observed_at: NOW - 60,
  windows: [
    { name: "five_hour", label: "Five hours", used: 12, remaining: 88, resets_at: NOW + 3600 },
    { name: "seven_day", label: "Seven days", used: 70, remaining: 30, resets_at: NOW + 86400 },
  ],
  limits: null,
  note: "Read from OpenAI's plan usage endpoint. Reading it spends no tokens.",
  error: null,
  action: { source: "chatgpt", kind: "fetch", label: "Refresh", costs_quota: false },
};

const unavailable = {
  id: "key:openai",
  kind: "key",
  label: "OpenAI",
  detail: "Your own OpenAI key",
  status: "unavailable",
  connected: true,
  observed_at: null,
  windows: [],
  limits: null,
  hint: "…1234",
  note: "OpenAI does not report a key's remaining quota to this server.",
  error: "OpenAI answered 500 when asked about the key.",
  action: null,
};

const keyed = {
  id: "key:openrouter",
  kind: "key",
  label: "OpenRouter",
  detail: "Your own OpenRouter key",
  status: "live",
  connected: true,
  observed_at: NOW,
  windows: [],
  limits: {
    limit_usd: null,
    remaining_usd: 7.5,
    usage_usd: 2.5,
    usage_daily_usd: 0.5,
    usage_weekly_usd: 1,
    usage_monthly_usd: 2,
    limit_reset: "monthly",
    observed_at: NOW,
  },
  hint: "…1234",
  note: "Read from OpenRouter's key endpoint.",
  error: null,
  action: { source: "key:openrouter", kind: "fetch", label: "Refresh", costs_quota: false },
};

function render(state, options) {
  const container = el("div");
  renderQuotas(container, state, options);
  return container;
}

test("a live subscription shows each window with its share and its reset", () => {
  const container = render({ now: NOW, sources: [live] });
  const card = container.find("usageQuotaCard");
  assert.equal(card.dataset.status, "live");
  assert.match(card.textContent, /ChatGPT plan/);
  assert.match(card.textContent, /Live/);
  assert.match(card.textContent, /Five hours/);
  assert.match(card.textContent, /12\.0%/);
  assert.match(card.textContent, /Resets .* in 1h/);
  // The bar is the used share, bounded to the track.
  assert.deepEqual(card.findAll("usageQuotaFill").map((fill) => fill.style.width), ["12%", "70%"]);
});

test("an unreadable source shows no numbers and says why", () => {
  const container = render({ now: NOW, sources: [unavailable] });
  const card = container.find("usageQuotaCard");
  assert.equal(card.dataset.status, "unavailable");
  assert.match(card.textContent, /does not report a key's remaining quota/);
  assert.match(card.textContent, /answered 500/);
  assert.equal(card.findAll("usageQuotaFill").length, 0);
  assert.equal(card.find("usageQuotaLimits"), null);
  assert.doesNotMatch(card.textContent, /\d%|\$\d/);
});

test("an unlimited key says so instead of printing a number for no limit", () => {
  const container = render({ now: NOW, sources: [keyed] });
  const card = container.find("usageQuotaCard");
  assert.match(card.textContent, /Unlimited/);
  assert.match(card.textContent, /\$7\.50/);
  assert.match(card.textContent, /monthly/);
});

test("the action button asks the page to read that source and waits while it does", () => {
  const asked = [];
  const container = render({ now: NOW, sources: [live] }, { onRefresh: (source) => asked.push(source.id) });
  const button = container.find("usageQuotaCard").querySelector("button");
  assert.equal(button.textContent, "Refresh");
  button.click();
  assert.deepEqual(asked, ["chatgpt"]);
  const busy = render({ now: NOW, sources: [live] }, { onRefresh: () => {}, busy: "chatgpt" });
  const waiting = busy.find("usageQuotaCard").querySelector("button");
  assert.equal(waiting.textContent, "Reading…");
  assert.equal(waiting.getAttribute("disabled"), "");
});

test("a source with nothing to read has no button, and no sources says so", () => {
  const container = render({ now: NOW, sources: [unavailable] });
  assert.equal(container.find("usageQuotaCard").querySelector("button"), null);
  const empty = render({ now: NOW, sources: [] });
  assert.match(empty.textContent, /No subscription or key is configured/);
});

test("the plan's own policy and spend are shown next to provider quotas", () => {
  const plan = {
    tier: "byok",
    label: "Bring your own key",
    limits: {
      usd_per_day: null,
      usd_per_month: 20,
      turns_per_day: 50,
      tokens_per_day: 1000000,
      max_tokens_per_turn: null,
      usd_per_turn: 0.5,
      running_turns: 2,
    },
    spent: { day_usd: 0.25, month_usd: 3.5 },
  };
  const container = render({ now: NOW, plan, sources: [live] });
  const cards = container.findAll("usageQuotaCard");
  assert.equal(cards.length, 2);
  const planCard = cards[0];
  assert.match(planCard.textContent, /Your plan/);
  assert.match(planCard.textContent, /Bring your own key/);
  assert.match(planCard.textContent, /\$0\.2500 · no limit/);
  assert.match(planCard.textContent, /\$3\.50 of \$20\.00/);
  assert.match(planCard.textContent, /Turns per day/);
  assert.match(planCard.textContent, /50/);
  assert.match(planCard.textContent, /1,000,000/);
  assert.match(planCard.textContent, /Cost per turn/);
  // Provider quotas still follow their own card.
  assert.match(cards[1].textContent, /ChatGPT plan/);
});

test("a plan with no daily or monthly limits says so instead of inventing numbers", () => {
  const container = render({
    now: NOW,
    plan: { tier: "default", label: "Default", limits: {}, spent: {} },
    sources: [],
  });
  const planCard = container.findAll("usageQuotaCard")[0];
  assert.match(planCard.textContent, /no daily or monthly limits/i);
  assert.doesNotMatch(planCard.textContent, /\$\d/);
});
