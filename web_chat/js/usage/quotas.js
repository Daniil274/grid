/**
 * Subscriptions and keys on the Usage page (web_chat.usage_snapshot).
 *
 * Every configured allowance is a card: its windows or its key limits, when
 * they were read, and an explicit button to read them now. A source that
 * cannot be read says why and shows no numbers.
 */
import { h } from "../lib/dom.js";

const STATUS = {
  live: ["ok", "Live"],
  stale: ["warn", "Stale"],
  estimate: ["muted", "Estimated"],
  unknown: ["muted", "Not read yet"],
  unavailable: ["muted", "Unavailable"],
  error: ["danger", "Cannot read"],
};
const KIND = { subscription: "Subscription", key: "API key", server: "Server key" };

const money = (value) => (value == null ? "Unlimited" : `$${Number(value).toFixed(Number(value) < 1 ? 4 : 2)}`);
const points = (value) => `${Number(value ?? 0).toFixed(1)}%`;

function moment(epoch) {
  return epoch ? new Date(epoch * 1000).toLocaleString() : "unknown";
}

function relative(epoch, now) {
  if (!epoch) return "unknown";
  const seconds = Math.round(epoch - now);
  const ahead = Math.abs(seconds);
  const text = ahead >= 86400 ? `${Math.round(ahead / 86400)}d` : ahead >= 3600 ? `${Math.round(ahead / 3600)}h` : `${Math.max(1, Math.round(ahead / 60))}m`;
  return seconds >= 0 ? `in ${text}` : `${text} ago`;
}

function windowCard(window, now) {
  const used = Math.max(0, Math.min(100, Number(window.used ?? 0)));
  return h("div.usageQuotaWindow", {},
    h("div.usageQuotaWindowHead", {}, h("span", { text: window.label }), h("strong", { text: points(window.used) })),
    h("div.usageQuotaTrack", {}, h("span.usageQuotaFill", { style: { width: `${used}%` } })),
    h("small", {
      text: window.resets_at
        ? `Resets ${moment(window.resets_at)} · ${relative(window.resets_at, now)}`
        : "Opens with the next use",
    }),
  );
}

function limitsGrid(limits) {
  const rows = [
    ["Limit", money(limits.limit_usd)],
    ["Remaining", limits.remaining_usd == null ? "Not reported" : money(limits.remaining_usd)],
    ["Used", money(limits.usage_usd)],
    ["Today", money(limits.usage_daily_usd)],
    ["This week", money(limits.usage_weekly_usd)],
    ["This month", money(limits.usage_monthly_usd)],
  ];
  if (limits.limit_reset) rows.push(["Resets", String(limits.limit_reset)]);
  return h("dl.usageQuotaLimits", {}, ...rows.flatMap(([term, value]) => [h("dt", { text: term }), h("dd", { text: value })]));
}

function meteredGrid(metered) {
  const rows = [];
  for (const [period, label] of [["day", "Today"], ["week", "Last 7 days"], ["month", "This month"]]) {
    const bucket = metered?.[period];
    if (!bucket) continue;
    const counts = `${Number(bucket.total_tokens ?? 0).toLocaleString()} tokens · ${Number(bucket.responses ?? 0).toLocaleString()} responses`;
    rows.push([label, bucket.cost_usd == null ? counts : `${counts} · ${money(bucket.cost_usd)}`]);
  }
  return rows.length ? h("div", {},
    h("h4", { text: "Recorded usage" }),
    h("dl.usageQuotaLimits", {}, ...rows.flatMap(([term, value]) => [h("dt", { text: term }), h("dd", { text: value })])),
  ) : null;
}

/** The plan's own policy (web_chat.credentials_api's /api/plan), next to provider quotas. */
function planCard(plan) {
  const limits = plan.limits || {};
  const spent = plan.spent || {};
  const rows = [];
  if (spent.day_usd != null) {
    rows.push(["Today", limits.usd_per_day == null ? `${money(spent.day_usd)} · no limit` : `${money(spent.day_usd)} of ${money(limits.usd_per_day)}`]);
  }
  if (spent.month_usd != null) {
    rows.push(["This month", limits.usd_per_month == null ? `${money(spent.month_usd)} · no limit` : `${money(spent.month_usd)} of ${money(limits.usd_per_month)}`]);
  }
  if (limits.turns_per_day != null) rows.push(["Turns per day", String(limits.turns_per_day)]);
  if (limits.tokens_per_day != null) rows.push(["Tokens per day", Number(limits.tokens_per_day).toLocaleString()]);
  if (limits.max_tokens_per_turn != null) rows.push(["Tokens per turn", Number(limits.max_tokens_per_turn).toLocaleString()]);
  if (limits.usd_per_turn != null) rows.push(["Cost per turn", money(limits.usd_per_turn)]);
  if (limits.running_turns != null) rows.push(["Turns at once", String(limits.running_turns)]);
  return h("article.usageQuotaCard", { dataset: { status: "live" } },
    h("header.usageQuotaHead", {},
      h("div", {}, h("h3", { text: "Your plan" }), h("small", { text: plan.label || plan.tier || "" })),
      h("div.usageQuotaHeadSide", {}, h("span.usageQuotaStatus", { dataset: { tone: "ok" }, text: "Policy" }))),
    rows.length
      ? h("dl.usageQuotaLimits", {}, ...rows.flatMap(([term, value]) => [h("dt", { text: term }), h("dd", { text: value })]))
      : h("p.usageQuotaNote", { text: "This plan sets no daily or monthly limits." }),
    h("p.usageQuotaNote", { text: "Counts only calls billed to the server's keys; your own keys and plans cost it nothing." }),
  );
}

function sourceCard(source, now, onRefresh, busy) {
  const [tone, label] = STATUS[source.status] ?? STATUS.unknown;
  const action = source.action
    ? h("button.btn.btn--ghost", {
        type: "button",
        text: busy === source.id ? "Reading…" : source.action.label,
        disabled: busy === source.id,
        on: { click: () => onRefresh(source) },
      })
    : null;
  return h("article.usageQuotaCard", { dataset: { status: source.status } },
    h("header.usageQuotaHead", {},
      h("div", {}, h("h3", { text: source.label }), h("small", { text: source.detail || KIND[source.kind] || "" })),
      h("div.usageQuotaHeadSide", {}, h("span.usageQuotaStatus", { dataset: { tone }, text: label }), action)),
    source.windows.length ? h("div.usageQuotaWindows", {}, ...source.windows.map((window) => windowCard(window, now))) : null,
    source.limits ? limitsGrid(source.limits) : null,
    source.metered ? meteredGrid(source.metered) : null,
    source.note ? h("p.usageQuotaNote", { text: source.note }) : null,
    source.hint ? h("p.usageQuotaNote", { text: `Key ${source.hint}` }) : null,
    source.error ? h("p.usageQuotaNote", { dataset: { tone: "danger" }, text: source.error }) : null,
  );
}

export function renderQuotas(container, state, options = {}) {
  const now = state?.now ?? Date.now() / 1000;
  const sources = state?.sources ?? [];
  const cards = sources.map((source) => sourceCard(source, now, options.onRefresh ?? (() => {}), options.busy));
  container.replaceChildren(...[
    h("div.usageQuotaTitle", {},
      h("div", {},
        h("h2", { text: "Subscriptions and keys" }),
        h("p", { text: "Every allowance this account has configured. A source that cannot be read says why instead of guessing." }))),
    options.error ? h("p.usageQuotaNote", { dataset: { tone: "danger" }, text: options.error }) : null,
    h("div.usageQuotaGrid", {}, ...(state?.plan ? [planCard(state.plan)] : []),
      ...(cards.length ? cards : [h("p.usageEmpty", {}, "No subscription or key is configured for this account.")])),
  ].filter(Boolean));
}
