/**
 * The lazy agents page (web_chat/lazy/http.py): your subscription's windows
 * now, the plan of the next days, and each task with where it stands, why it
 * waits and what would save a deadline it misses.
 *
 * The server plans; the page only shows the plan and sends what the user
 * decides. Every change answers with the whole state, so the page redraws
 * from one consistent picture. Between answers it refreshes on a timer and
 * keeps its countdowns moving on the server's clock.
 */

import { h, icon, replace } from "../lib/dom.js";
import { api } from "../net/api.js";
import { ICONS } from "../ui/icons.js";
import { applyStoredTheme } from "../ui/theme.js";
import { toast } from "../ui/toast.js";
import { openDeadlineDialog, openSettingsDialog, openTaskDialog } from "./dialogs.js";
import { STATES, colorSlot, duration, moment, points, relative, stateOf, tokens, waitText } from "./format.js";
import { drawTimeline } from "./timeline.js";

applyStoredTheme();

const $ = (id) => document.getElementById(id);
const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const REFRESH_MS = 15_000;
const TICK_MS = 30_000;
const FINISHED = ["done", "failed", "cancelled"];
const STEP_MARK = { done: ICONS.check, running: ICONS.resume, failed: ICONS.close, skipped: "M6 12h12", pending: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z" };

let state = null;
let skew = 0;
let filter = "open";
const expanded = new Set();

const now = () => Date.now() / 1000 + skew;
/** A dialog or an open menu is in use: a redraw now would take it away. */
const busy = () => Boolean(document.querySelector("dialog[open], details.lazyMenu[open]"));

// -- loading and acting --------------------------------------------------------------------
function statusLine(text, tone = "") {
  const node = $("status");
  node.textContent = text;
  node.dataset.tone = tone;
  node.hidden = !text;
}

function show(next) {
  state = next;
  state.loadedAt = Date.now() / 1000;
  skew = state.now - state.loadedAt;
  statusLine("");
  render();
}

async function load() {
  try {
    show(await api.lazy(TZ));
  } catch (error) {
    statusLine(error.message, "error");
  }
}

/** Send a change; the answer is the new state. */
export async function act(request, success) {
  try {
    const next = await request;
    show(next);
    if (success) toast(success, { tone: "success" });
    return next;
  } catch (error) {
    toast(error.message, { tone: "error" });
    return null;
  }
}

// -- the header ------------------------------------------------------------------------------
const MODES = {
  live: { tone: "ok", text: (account) => `Live · read ${relative(account.observed_at, now())}` },
  stale: { tone: "warn", text: (account) => `Last read ${relative(account.observed_at, now())} · planning with care` },
  estimate: { tone: "muted", text: () => "Estimated from your usage" },
  unknown: { tone: "warn", text: () => "Nothing read yet · planning with care" },
};
const MODE_HINTS = {
  live: "The subscription reported its windows a moment ago.",
  stale: "The last report is old: your usual use since then is added, so the plan errs toward less room. The next request refreshes it.",
  estimate: "This subscription reports nothing about its limits: use is counted from the tokens of your calls through it, against the capacity set in the settings.",
  unknown: "No report yet: every window is taken as half used until the subscription answers once.",
};

function renderHeader() {
  const { account, settings } = state;
  const mode = MODES[account.mode] ?? MODES.unknown;
  const badge = $("telemetry");
  badge.hidden = false;
  badge.dataset.tone = mode.tone;
  badge.title = MODE_HINTS[account.mode] ?? "";
  replace(badge, h("i.lazyBadge__dot"), mode.text(account));
  const pause = $("pause-all");
  pause.hidden = false;
  pause.textContent = settings.paused ? "Resume all" : "Pause all";
  pause.classList.toggle("btn--primary", settings.paused);
  pause.classList.toggle("btn--ghost", !settings.paused);

  const banners = [];
  if (settings.paused) banners.push(["muted", "Lazy agents are paused: no step starts until you resume them."]);
  if (account.overage_allowed) {
    banners.push(["danger", "Extra usage is on for this account: going past the plan would be billed. Turn it off in your Claude account to keep lazy work within the subscription."]);
  }
  if (account.status === "rejected") banners.push(["warn", "The subscription refused its last request: a limit is reached. Steps wait for the window to reset."]);
  replace($("banners"), banners.map(([tone, text]) => h("p.lazyBanner", { dataset: { tone } }, text)));
}

// -- the windows ------------------------------------------------------------------------------
function segment(kind, value, title) {
  if (value <= 0.05) return null;
  return h(`span.gauge__seg.gauge__seg--${kind}`, { style: { flexBasis: `${Math.min(100, value)}%` }, title });
}

function windowCard(name, window) {
  const free = Math.max(0, 100 - window.used - window.reserved - window.forecast - window.safety);
  const shareUsed = window.agents + window.reserved;
  const resets = window.resets_at
    ? `Resets ${relative(window.resets_at, now())} · ${moment(window.resets_at, now())}`
    : "Opens with the next use";
  return h("article.windowCard", { dataset: { window: name } },
    h("header.windowCard__head", {},
      h("h3", { text: window.label }),
      h("span.windowCard__reset", { text: resets })),
    h("div.windowCard__total", {},
      h("strong", { text: `${points(window.used)}%` }),
      h("span", { text: " used" })),
    h("div.gauge", { role: "img", "aria-label": `${window.label}: ${points(window.used)} of 100 points used` },
      segment("user", window.user, `You: ${points(window.user)}`),
      segment("agents", window.agents, `Agents: ${points(window.agents)}`),
      segment("reserved", window.reserved, `Booked by the running step: ${points(window.reserved)}`),
      segment("forecast", window.forecast, `Your expected use until the reset: ${points(window.forecast)}`),
      segment("free", free, `Free: ${points(free)}`),
      segment("safety", window.safety, `Kept free for you: ${points(window.safety)}`)),
    h("ul.windowCard__legend", {},
      h("li", {}, h("i.sw.sw--user"), `You ${points(window.user)}`),
      h("li", {}, h("i.sw.sw--agents"), `Agents ${points(window.agents)}`),
      window.reserved > 0 ? h("li", {}, h("i.sw.sw--reserved"), `Booked ${points(window.reserved)}`) : null,
      h("li", {}, h("i.sw.sw--forecast"), `Your forecast +${points(window.forecast)}`),
      h("li", {}, h("i.sw.sw--reserve"), `Reserve ${points(window.safety)}`)),
    h("div.share", {},
      h("div.share__line", {},
        h("span", { text: "Agents' share" }),
        h("span.share__numbers", {},
          h("strong", { text: points(shareUsed) }), ` of ${points(window.share)} · `,
          h("strong.share__room", { text: points(window.room) }), " free now")),
      h("div.share__bar", { role: "img", "aria-label": `Agents used ${points(shareUsed)} of their ${points(window.share)}` },
        h("span.share__used", { style: { width: `${Math.min(100, (window.agents / Math.max(window.share, 0.01)) * 100)}%` } }),
        h("span.share__booked", { style: { width: `${Math.min(100, (window.reserved / Math.max(window.share, 0.01)) * 100)}%` } }))),
  );
}

function poolQuota(account, windows, secondary = false) {
  const calibrated = account.calibrated_runs
    ? `Prices calibrated by ${account.calibrated_runs} run${account.calibrated_runs === 1 ? "" : "s"}`
    : "Prices are first guesses until a run is measured";
  const mode = MODES[account.mode] ?? MODES.unknown;
  return [
    h("header.lazyQuota__head", {},
      h("div", {},
        h("h2", { text: account.label }),
        h("p", { text: secondary
          ? `${mode.text(account)} · ${calibrated} · the tasks of agents on this subscription spend it, beside the one above`
          : `${calibrated} · your rates learned from ${account.learned_readings} interval${account.learned_readings === 1 ? "" : "s"}` }))),
    h("div.lazyQuota__windows", {}, windowCard("five_hour", windows.five_hour), windowCard("seven_day", windows.seven_day)),
  ];
}

function renderQuota() {
  const { account, windows } = state;
  // Every other subscription the tasks spend has a pool of its own: its windows follow.
  replace($("quota"),
    ...poolQuota(account, windows),
    ...(state.pools ?? []).flatMap((pool) => poolQuota(pool.account, pool.windows, true)),
  );
}

// -- the timeline -----------------------------------------------------------------------------
function highlight(taskId) {
  for (const card of document.querySelectorAll(".taskCard")) {
    card.classList.toggle("is-pointed", taskId !== null && card.dataset.task === taskId);
  }
}

function renderTimeline() {
  const host = $("timeline");
  const width = Math.max(720, host.clientWidth || 960);
  replace(host, drawTimeline({ ...state, now: now() }, width, highlight));
}

// -- the tasks --------------------------------------------------------------------------------
function chip(stateKey) {
  const info = STATES[stateKey] ?? STATES.flexible;
  return h("span.stateChip", { dataset: { tone: info.tone }, title: info.hint }, info.label);
}

function stepPill(step, task) {
  const running = step.status === "running";
  const measured = Object.keys(step.used || {}).length > 0;
  const cost = measured ? step.used : step.estimate;
  const detail = `${step.title}: ${step.status} · ~${tokens(step.tokens)} tokens, ${step.minutes} min`
    + ` · 5-hour ${points(cost.five_hour)}, weekly ${points(cost.seven_day)} pts ${measured ? "used" : "estimated"}`
    + (step.reason ? ` · ${step.reason}` : "");
  return h("li.stepPill", { dataset: { status: step.status, optional: step.optional ? "true" : "false" }, title: detail },
    icon(STEP_MARK[step.status] ?? STEP_MARK.pending, { size: 12, className: "stepPill__mark" }),
    h("span", { text: step.title }),
    running ? h("span.stepPill__since", { text: duration(now() - (step.started_at || now())) }) : null,
    step.optional ? h("span.stepPill__tag", { text: "optional" }) : null,
  );
}

function planLine(task, planned) {
  if (!planned || FINISHED.includes(task.status)) return null;
  const parts = [];
  const running = task.steps.find((step) => step.status === "running");
  if (running) {
    parts.push(h("span", {}, h("strong", { text: running.title }), ` running for ${duration(now() - (running.started_at || now()))}`));
  } else if (planned.next_start) {
    const next = task.steps.find((step) => step.status === "pending");
    const when = planned.next_start <= now() + 60 ? "starting now" : `${moment(planned.next_start, now())} (${relative(planned.next_start, now())})`;
    parts.push(h("span", {}, "Next: ", h("strong", { text: next?.title ?? "step" }), ` · ${when}`));
  }
  if (planned.wait && !running && planned.state !== "paused") {
    parts.push(h("span.planLine__wait", { text: waitText(planned.wait, now()) }));
  }
  let finish = null;
  if (planned.finish_at) {
    if (task.deadline) {
      const margin = task.deadline - planned.finish_at;
      finish = margin >= 0
        ? `Ready ${moment(planned.finish_at, now())} · ${duration(margin)} before the deadline`
        : `Ready ${moment(planned.finish_at, now())} · ${duration(-margin)} after the deadline`;
    } else {
      finish = `Ready ${moment(planned.finish_at, now())}`;
    }
  }
  return h("div.planLine", {}, h("div.planLine__now", {}, ...parts), finish ? h("div.planLine__finish", { text: finish }) : null);
}

function waysOut(task, planned) {
  if (!planned?.options?.length) return null;
  const late = planned.late_by ? `Misses the deadline by ${duration(planned.late_by)}.` : "No plan meets the deadline.";
  return h("div.waysOut", {},
    h("p.waysOut__title", {}, icon(ICONS.error, { size: 14 }), ` ${late} Ways out:`),
    h("div.waysOut__options", {},
      planned.options.map((option) => h("button.btn.btn--ghost.waysOut__option", {
        type: "button",
        title: "Applies this change; the plan follows at once",
        on: { click: () => act(api.lazyApply(task.id, option.change), "Plan updated") },
      }, optionText(option)))),
  );
}

function optionText(option) {
  if (option.kind === "deadline") return `Move the deadline to ${moment(option.change.task.deadline, now())}`;
  return option.text;
}

function menu(task) {
  const finished = FINISHED.includes(task.status);
  const items = [];
  if (!finished) {
    for (const size of ["S", "M", "L"]) {
      if (size !== task.size) items.push([`Size ${size}`, () => act(api.lazyUpdate(task.id, { size }), `Re-estimated as ${size}`)]);
    }
    for (const priority of ["high", "normal", "low"]) {
      if (priority !== task.priority) items.push([`Priority: ${priority}`, () => act(api.lazyUpdate(task.id, { priority }))]);
    }
    if (task.steps.some((step) => step.optional && step.status === "pending")) {
      items.push(["Drop the optional part", () => act(api.lazyUpdate(task.id, { drop_optional: true }), "Optional part dropped")]);
    }
    items.push(["Cancel the task", () => {
      if (confirm(`Cancel "${task.title}"? A running step stops at a safe point; done work stays in its chat.`)) {
        act(api.lazyCancel(task.id), "Cancelled");
      }
    }, "danger"]);
  } else {
    items.push(["Remove from the list", () => act(api.lazyDelete(task.id), "Removed"), "danger"]);
  }
  const details = h("details.lazyMenu", {},
    h("summary.iconBtn", { title: "More", "aria-label": "More actions" }, icon("M5 12h.01M12 12h.01M19 12h.01", { size: 18 })),
    h("div.lazyMenu__list", { role: "menu" },
      items.map(([label, run, tone]) => h("button.lazyMenu__item", {
        type: "button", role: "menuitem", dataset: tone ? { tone } : undefined,
        on: { click: () => { details.open = false; run(); } },
      }, label))));
  return details;
}

function actions(task, planned) {
  const finished = FINISHED.includes(task.status);
  const buttons = [];
  if (task.conversation_id) {
    buttons.push(h("a.btn.btn--ghost", { href: `/?conversation=${encodeURIComponent(task.conversation_id)}`, target: "grid-chat", rel: "noopener" },
      icon(ICONS.message ?? ICONS.send, { size: 14 }), "Open chat"));
  }
  if (!finished) {
    if (task.status === "paused") {
      buttons.push(h("button.btn.btn--primary", { type: "button", on: { click: () => act(api.lazyUpdate(task.id, { status: "active" }), "Resumed") } },
        icon(ICONS.resume, { size: 14 }), "Resume"));
    } else {
      if (!task.run_now) {
        buttons.push(h("button.btn.btn--ghost", {
          type: "button", title: "First in line, and it may run in your hours too - never past the budget",
          on: { click: () => act(api.lazyUpdate(task.id, { run_now: true }), "First in line") },
        }, icon(ICONS.resume, { size: 14 }), "Do it now"));
      } else {
        buttons.push(h("button.btn.btn--ghost", { type: "button", on: { click: () => act(api.lazyUpdate(task.id, { run_now: false }), "Back in line") } },
          "Back in line"));
      }
      buttons.push(h("button.btn.btn--ghost", { type: "button", on: { click: () => act(api.lazyUpdate(task.id, { status: "paused" }), "Paused") } },
        icon(ICONS.stop, { size: 13 }), "Pause"));
    }
    buttons.push(h("button.btn.btn--ghost", {
      type: "button", on: { click: () => openDeadlineDialog(task, planned, state, act) },
    }, icon(ICONS.clock, { size: 14 }), task.deadline ? "Deadline" : "Set a deadline"));
  }
  buttons.push(h("button.btn.btn--ghost.taskCard__toggle", {
    type: "button", "aria-expanded": String(expanded.has(task.id)),
    on: { click: () => { expanded.has(task.id) ? expanded.delete(task.id) : expanded.add(task.id); renderTasks(); } },
  }, expanded.has(task.id) ? "Less" : "Details"));
  return h("div.taskCard__actions", {}, buttons, menu(task));
}

function details(task) {
  if (!expanded.has(task.id)) return null;
  const block = (label, text) => (text?.trim() ? h("div.taskDetails__block", {}, h("h4", { text: label }), h("p", { text })) : null);
  return h("div.taskDetails", {},
    h("div.taskDetails__brief", {},
      block("What to do", task.brief), block("Done when", task.acceptance), block("Optional part", task.optional)),
    h("table.stepTable", {},
      h("thead", {}, h("tr", {}, ["Step", "Status", "Estimate", "Used", "Tokens", "Where it left the work"].map((label) => h("th", { text: label })))),
      h("tbody", {}, task.steps.map((step) => h("tr", { dataset: { status: step.status } },
        h("td", {}, step.title, step.optional ? h("span.stepPill__tag", { text: "optional" }) : null),
        h("td", { text: step.status + (step.attempts > 1 ? ` · ${step.attempts} tries` : "") }),
        h("td", { text: `${points(step.estimate.five_hour)} / ${points(step.estimate.seven_day)}` }),
        h("td", { text: Object.keys(step.used || {}).length ? `${points(step.used.five_hour)} / ${points(step.used.seven_day)}` : "–" }),
        h("td", { text: step.tokens_used ? tokens(step.tokens_used) : `~${tokens(step.tokens)}` }),
        h("td.stepTable__summary", { text: step.summary || step.reason || "" }),
      )))),
    h("p.stepTable__note", { text: "Points: 5-hour window / weekly limit." }),
    task.history?.length
      ? h("ol.history", {}, [...task.history].reverse().map((entry) => h("li", { dataset: { kind: entry.kind } },
        h("time", { text: moment(entry.at, now()), datetime: new Date(entry.at * 1000).toISOString() }),
        h("span", { text: entry.text }))))
      : null,
  );
}

function taskCard(task) {
  const planned = state.plan.tasks[task.id];
  const key = stateOf(task, planned);
  const agent = state.agents.find((choice) => choice.system === task.system && choice.agent === task.agent);
  const meta = [
    agent ? `${agent.label} · ${agent.system_label}` : `${task.agent} · ${task.system}`,
    // A task on another subscription than the settings' says which one it spends.
    task.account_label ? `On ${task.account_label}` : null,
    `Size ${task.size}`,
    task.priority !== "normal" ? `${task.priority} priority` : null,
    task.deadline ? `Deadline ${moment(task.deadline, now())} (${relative(task.deadline, now())})` : "No deadline",
    task.run_now ? "Do it now" : null,
    task.repeat ? `Repeats every ${task.repeat >= 86400 && task.repeat % 86400 === 0 ? `${task.repeat / 86400} d` : `${task.repeat / 3600} h`}` : null,
  ].filter(Boolean);
  return h("article.taskCard", { dataset: { task: task.id, state: key }, class: `c${colorSlot(task.id)}` },
    h("header.taskCard__head", {},
      h("span.taskCard__dot", { "aria-hidden": "true" }),
      h("h3.taskCard__title", { text: task.title }),
      chip(key)),
    h("p.taskCard__meta", { text: meta.join(" · ") }),
    h("ol.stepStrip", { "aria-label": "Steps" }, task.steps.map((step) => stepPill(step, task))),
    planLine(task, planned),
    ["at_risk", "blocked", "running_late"].includes(key) ? waysOut(task, planned) : null,
    actions(task, planned),
    details(task),
  );
}

function emptyState() {
  return h("div.lazyEmpty", {},
    h("div.lazyEmpty__art", { "aria-hidden": "true" }, icon(ICONS.clock, { size: 34 })),
    h("h3", { text: filter === "finished" ? "Nothing finished yet" : "No lazy tasks yet" }),
    filter === "finished" ? null : h("p", { text: "Describe a result, say how to check it and when you need it. The agents find the hours and the room for it, and tell you early when a deadline cannot hold." }),
    filter === "finished" ? null : h("ol.lazyEmpty__steps", {},
      h("li", {}, h("strong", { text: "Scout" }), " — a short look that sizes the work"),
      h("li", {}, h("strong", { text: "Work" }), " — done in the windows you can spare"),
      h("li", {}, h("strong", { text: "Verify" }), " — checked against your criteria, budgeted up front")),
    filter === "finished" ? null : h("button.btn.btn--primary", { type: "button", on: { click: () => openTaskDialog(state, act) } }, icon(ICONS.plus, { size: 14 }), "New task"),
  );
}

function renderTasks() {
  const visible = state.tasks.filter((task) =>
    filter === "all" || (filter === "finished" ? FINISHED.includes(task.status) : !FINISHED.includes(task.status)));
  const order = { running: 0, running_late: 1, at_risk: 2, blocked: 3, on_track: 4, flexible: 5, paused: 6, done: 7, failed: 8, cancelled: 9 };
  visible.sort((a, b) => {
    const sa = order[stateOf(a, state.plan.tasks[a.id])] ?? 9;
    const sb = order[stateOf(b, state.plan.tasks[b.id])] ?? 9;
    return sa - sb || (a.deadline ?? Infinity) - (b.deadline ?? Infinity) || b.created_at - a.created_at;
  });
  replace($("tasks"), visible.length ? visible.map(taskCard) : emptyState());
  for (const tab of document.querySelectorAll("#filter [data-filter]")) {
    tab.setAttribute("aria-selected", String(tab.dataset.filter === filter));
  }
}

function render() {
  if (!state) return;
  renderHeader();
  renderQuota();
  renderTimeline();
  renderTasks();
}

// -- wiring ------------------------------------------------------------------------------------
$("new-task").addEventListener("click", () => state && openTaskDialog(state, act));
$("open-settings").addEventListener("click", () => state && openSettingsDialog(state, act));
$("pause-all").addEventListener("click", () => {
  if (!state) return;
  const paused = !state.settings.paused;
  act(api.lazySettings({ paused }), paused ? "Paused: running steps stop at a safe point" : "Resumed");
});
for (const tab of document.querySelectorAll("#filter [data-filter]")) {
  tab.addEventListener("click", () => {
    filter = tab.dataset.filter;
    renderTasks();
  });
}
new ResizeObserver(() => state && renderTimeline()).observe($("timeline"));
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") load();
});
setInterval(() => {
  if (document.visibilityState === "visible" && !busy()) load();
}, REFRESH_MS);
setInterval(() => state && !busy() && render(), TICK_MS);
load();
