/**
 * The plan card: the conversation's tracker tasks and where each one stands.
 *
 * The server keeps it as a `plan` step of the turn's trace
 * (web_chat/plan_board.py) and re-sends that step whenever a task changes. It
 * sits above the answer rather than inside the reasoning panel: the panel
 * folds away when the turn ends, the plan is what the reader follows.
 *
 * A plan outlives the request that made it, so the card shows two scopes:
 * "This request" - the tasks this turn planned, took, updated or closed (each
 * task's `turn` flag, web_chat/plan_board.py) - and "Whole plan". It opens on
 * the request's own share; the switch shows only when the two differ. The
 * head folds the card to its progress line: an older answer's card starts
 * folded, the newest one open, and once the reader folds or opens a card
 * themselves it stays as they left it.
 *
 * Every row opens the task's own dialog (a native `<dialog>`, docked to the
 * right edge on a wide screen, full-bleed on a phone). Its Task tab carries
 * what the tracker recorded - description, acceptance criteria, notes, close
 * reason, dependencies; its Result tab carries what the run that took the
 * task reported, its answer only when the run completed. Whatever no call
 * recorded stays visibly missing, and a closed task is closed, never a
 * verified success. While the plan updates, the dialog follows the task it
 * shows: the open task, the chosen tab and where the reader scrolled all
 * survive the re-render, and focus comes back to the row that opened it.
 */

import { h, icon, replace } from "../lib/dom.js";
import { renderMarkdown } from "../lib/markdown.js";
import { ICONS } from "./icons.js";

/** How each state reads, in the order the summary lists them. */
const STATES = {
  closed: { label: "Closed", mark: "✓" },
  in_progress: { label: "In progress", mark: "●" },
  ready: { label: "Ready", mark: "○" },
  waiting: { label: "Waiting", mark: "◌" },
  blocked: { label: "Blocked", mark: "!" },
};

/** The card's two scopes: the request's own tasks, and every task of the plan. */
const SCOPES = {
  turn: { label: "This request", heading: "This request" },
  all: { label: "Whole plan", heading: "Plan" },
};

let dialogSequence = 0;

/** Each state's count among *tasks*. */
const countStates = (tasks) =>
  tasks.reduce((counts, task) => ({ ...counts, [task.state]: (counts[task.state] ?? 0) + 1 }), {});

const usd = (amount) => (amount >= 1 ? `$${amount.toFixed(2)}` : `$${amount.toFixed(4)}`);

const duration = (seconds) => {
  const total = Math.round(Number(seconds) || 0);
  return total >= 60 ? `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, "0")}s` : `${total}s`;
};

/** The task's row in the plan: everything it says, plus a way to open it. */
function taskRow(task, onOpen) {
  const state = STATES[task.state] ?? STATES.ready;
  const waits = task.state === "waiting" && task.waits_for?.length ? `waits for ${task.waits_for.join(", ")}` : "";
  return h(
    "li.planCard__task",
    { dataset: { state: task.state } },
    h(
      "button.planCard__row",
      { type: "button", "aria-haspopup": "dialog", on: { click: () => onOpen(task) } },
      h("span.planCard__mark", { text: state.mark, title: state.label, "aria-label": state.label }),
      h("span.planCard__title", { text: task.title }),
      h("span.planCard__meta", { text: [task.id, waits].filter(Boolean).join(" · ") }),
    ),
  );
}

/** Task text the tracker recorded, as markdown; the plan never trusts it as HTML. */
const prose = (markdown) => h("div.taskDialog__prose", { html: renderMarkdown(markdown) });

/** What the field honestly says when nothing was ever recorded for it. */
const missing = (text) => h("p.taskDialog__missing", { text });

const section = (title, content) =>
  h("section.taskDialog__section", {}, h("h3.taskDialog__sectionTitle", { text: title }), content);

/** One dependency: a jump to that task when the plan knows it, its id when not. */
function dependencyItem(dep, tasks, jump) {
  const target = tasks.find((task) => task.id === dep || task.key === dep);
  if (!target) return h("li.taskDialog__dep", {}, h("span.taskDialog__depOff", { text: dep }));
  const state = STATES[target.state] ?? STATES.ready;
  return h(
    "li.taskDialog__dep",
    {},
    h(
      "button.taskDialog__depLink",
      { type: "button", dataset: { state: target.state }, title: `Open ${target.title}`, on: { click: () => jump(target) } },
      h("span.planCard__mark", { text: state.mark, "aria-label": state.label }),
      h("span.taskDialog__depTitle", { text: target.title }),
    ),
  );
}

/** The Task tab: what the task is, what it takes, what the tracker holds. */
function taskPanel(task, tasks, jump, ids) {
  const deps = task.depends_on ?? [];
  return h(
    "div.taskDialog__panel",
    { role: "tabpanel", id: ids.panelTask, "aria-labelledby": ids.tabTask, tabindex: "0" },
    section("Description", task.description ? prose(task.description) : missing("No description was recorded.")),
    section("Acceptance criteria", task.acceptance ? prose(task.acceptance) : missing("No acceptance criteria were recorded.")),
    section("Notes", task.notes ? h("p.taskDialog__notes", { text: task.notes }) : missing("No notes were recorded.")),
    // Only a closed task can have a close reason; a closed task without one
    // says so rather than borrowing the notes or the report as one.
    task.state === "closed"
      ? section("Close reason", task.close_reason ? h("p.taskDialog__notes", { text: task.close_reason }) : missing("Closed without a recorded reason."))
      : null,
    section(
      "Depends on",
      deps.length ? h("ul.taskDialog__deps", {}, deps.map((dep) => dependencyItem(dep, tasks, jump))) : missing("No dependencies."),
    ),
  );
}

/** The Result tab: what the run reported, its answer only when it completed. */
function resultPanel(task, ids) {
  const report = task.report;
  const closeReason = task.state === "closed"
    ? section("Close reason", task.close_reason ? prose(task.close_reason) : missing("Closed without a recorded reason."))
    : null;
  const content = !report
    ? missing(task.state === "closed"
      ? "This task was closed without a run reporting a result."
      : "No run has reported a result for this task yet.")
    : report.status === "completed" && typeof report.final === "string" && report.final.trim()
      ? prose(report.final)
      : missing(report.status === "completed"
        ? "The run completed, but no result text was recorded."
        : `The run did not complete (${report.status ?? "no status"}); its result was not recorded.`);
  return h(
    "div.taskDialog__panel",
    { role: "tabpanel", id: ids.panelResult, "aria-labelledby": ids.tabResult, tabindex: "0" },
    report ? metrics(report) : null,
    content,
    closeReason,
  );
}

/** What a run cost and used, each field only when the report brought it. */
function metrics(report) {
  const rows = [];
  if (report.status != null) rows.push(["Status", String(report.status)]);
  if (report.seconds != null) rows.push(["Duration", duration(report.seconds)]);
  if (report.model_calls != null) rows.push(["Model calls", String(report.model_calls)]);
  if (report.tokens != null) {
    if (typeof report.tokens === "object") {
      for (const [key, label] of [["input", "Input tokens"], ["output", "Output tokens"]]) {
        const value = report.tokens[key];
        if (typeof value === "number" && Number.isFinite(value)) rows.push([label, value.toLocaleString()]);
      }
    } else if (Number.isFinite(Number(report.tokens))) {
      rows.push(["Tokens", Number(report.tokens).toLocaleString()]);
    }
  }
  if (report.cost_usd != null) rows.push(["Cost", usd(Number(report.cost_usd))]);
  if (report.model_key != null) rows.push(["Model", String(report.model_key)]);
  if (report.tier != null) rows.push(["Tier", String(report.tier)]);
  return h(
    "ul.taskDialog__metrics",
    {},
    rows.map(([label, value]) =>
      h("li.taskDialog__metric", {}, h("span.taskDialog__metricLabel", { text: label }), h("span.taskDialog__metricValue", { text: value })),
    ),
  );
}

export function createPlanCard() {
  const prefix = `taskDialog-${++dialogSequence}`;
  const ids = Object.fromEntries(["title", "tabTask", "tabResult", "panelTask", "panelResult"].map((name) => [name, `${prefix}-${name}`]));
  const title = h("span.planCard__heading", { text: "Plan" });
  const summary = h("span.planCard__summary");
  const bar = h("span.planCard__fill");
  const list = h("ol.planCard__tasks", { id: `${prefix}-tasks` });
  const toggle = h(
    "button.planCard__head",
    { type: "button", "aria-expanded": "true", "aria-controls": list.id, title: "Fold or open the plan" },
    h("span.planCard__chevron", {}, icon(ICONS.chevron, { size: 12 })),
    title,
    summary,
    h("span.planCard__bar", {}, bar),
  );
  const scopeTabs = Object.fromEntries(
    Object.entries(SCOPES).map(([name, { label }]) => [
      name,
      h("button.planCard__scope", {
        type: "button", role: "tab", "aria-selected": "false", "aria-controls": list.id,
        dataset: { scope: name }, text: label,
      }),
    ]),
  );
  const scopes = h(
    "div.planCard__scopes",
    { role: "tablist", "aria-label": "Which tasks the plan shows", hidden: true },
    scopeTabs.turn,
    scopeTabs.all,
  );
  const root = h("section.planCard", { hidden: true }, toggle, scopes, list);

  let collapsePinned = false; // the reader folded or opened it: leave it so
  const setCollapsed = (collapsed) => {
    root.classList.toggle("is-collapsed", collapsed);
    toggle.setAttribute("aria-expanded", String(!collapsed));
  };
  toggle.addEventListener("click", () => {
    collapsePinned = true;
    setCollapsed(!root.classList.contains("is-collapsed"));
  });

  // -- the task dialog -------------------------------------------------------

  const stateBadge = h("span.taskDialog__state");
  const idBadge = h("span.taskDialog__id");
  const taskTitle = h("h2.taskDialog__title", { id: ids.title });
  const taskTab = h("button.taskDialog__tab", {
    type: "button", role: "tab", id: ids.tabTask,
    "aria-controls": ids.panelTask, "aria-selected": "true", text: "Task",
  });
  const resultTab = h("button.taskDialog__tab", {
    type: "button", role: "tab", id: ids.tabResult,
    "aria-controls": ids.panelResult, "aria-selected": "false", tabindex: "-1", text: "Result",
  });
  const body = h("div.taskDialog__body");
  // The same task on the tracker page (?task=<id> opens its dialog there).
  const trackerLink = h("a.taskDialog__tracker", { hidden: true, text: "Open in tracker" });
  const prevButton = h("button.btn.btn--ghost.btn--xs", { type: "button", text: "Previous" });
  const nextButton = h("button.btn.btn--ghost.btn--xs", { type: "button", text: "Next" });
  const count = h("span.taskDialog__count");
  const closeButton = h(
    "button.taskDialog__close",
    { type: "button", "aria-label": "Close task details" },
    icon(ICONS.close, { size: 15 }),
  );
  const dialog = h(
    "dialog.taskDialog",
    { "aria-labelledby": ids.title },
    h(
      "header.taskDialog__head",
      {},
      h("span.taskDialog__badges", {}, stateBadge, idBadge),
      trackerLink,
      closeButton,
    ),
    taskTitle,
    h("div.taskDialog__tabs", { role: "tablist", "aria-label": "Task sections" }, taskTab, resultTab),
    body,
    h("footer.taskDialog__foot", {}, prevButton, count, nextButton),
  );
  // The dialog lives inside the card, so it leaves with the message it
  // belongs to; once open it renders in the top layer, above anything else.
  root.append(dialog);

  let allTasks = []; // the plan as it stands
  let tasks = []; // the tasks the chosen scope shows
  let scope = "turn"; // which scope is shown, when the plan has both
  let scopePinned = false; // the reader chose it: updates keep it
  let openId = null; // the task whose dialog is open; null while it is closed
  let openerId = null; // the row that opened it - focus returns there
  let openTab = "task"; // the tab the reader is on
  let scrollTop = 0; // where the reader was, kept across plan updates
  let taskPanelEl = null;
  let resultPanelEl = null;
  let contentSignature = null;
  const rows = new Map(); // task id -> its row's button, for focus return

  const setClosed = () => {
    openId = null;
    dialog.dataset.open = "false";
    (rows.get(openerId) ?? toggle).focus?.();
  };
  const dismiss = () => {
    if (openId == null) return;
    dialog.close?.();
    setClosed();
  };
  closeButton.addEventListener("click", dismiss);
  // The browser's Escape closes the dialog itself; this is that path's share.
  dialog.addEventListener("close", () => {
    if (openId != null) setClosed();
  });

  const setTab = (tab, focus = false) => {
    openTab = tab;
    const on = tab === "task" ? taskTab : resultTab;
    const off = tab === "task" ? resultTab : taskTab;
    on.setAttribute("aria-selected", "true");
    off.setAttribute("aria-selected", "false");
    on.tabIndex = 0;
    off.tabIndex = -1;
    if (taskPanelEl) taskPanelEl.hidden = tab !== "task";
    if (resultPanelEl) resultPanelEl.hidden = tab !== "result";
    if (focus) on.focus?.();
  };
  taskTab.addEventListener("click", () => setTab("task"));
  resultTab.addEventListener("click", () => setTab("result"));
  for (const tab of [taskTab, resultTab]) {
    tab.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
      event.preventDefault?.();
      setTab(openTab === "task" ? "result" : "task", true);
    });
  }

  /** Paint the dialog for the task it shows; keeps the tab and the scroll. */
  const renderDialog = (focus = false) => {
    const walk = walkable();
    const index = walk.findIndex((task) => task.id === openId);
    if (index < 0) return;
    const task = walk[index];
    const state = STATES[task.state] ?? STATES.ready;
    replace(stateBadge, h("span.planCard__mark", { text: state.mark, "aria-label": state.label }), h("span", { text: state.label }));
    stateBadge.dataset.state = task.state;
    idBadge.textContent = task.id ?? "";
    // The tracker's own page: a deep link to the same task's dialog.
    trackerLink.hidden = !task.id;
    if (task.id) {
      trackerLink.href = `/tasks?task=${encodeURIComponent(task.id)}`;
      trackerLink.title = `Open ${task.id} in the tracker`;
    }
    taskTitle.textContent = task.title;
    const signature = JSON.stringify([
      {
        id: task.id, description: task.description, acceptance: task.acceptance,
        notes: task.notes, close_reason: task.close_reason, report: task.report,
        closed: task.state === "closed", depends_on: task.depends_on,
      },
      allTasks.filter((target) => (task.depends_on ?? []).some((dep) => dep === target.id || dep === target.key))
        .map(({ id, key, title, state }) => ({ id, key, title, state })),
    ]);
    if (signature !== contentSignature) {
      const active = document.activeElement;
      if (active && body.contains(active)) focus = true;
      taskPanelEl = taskPanel(task, allTasks, jumpTo, ids);
      resultPanelEl = resultPanel(task, ids);
      replace(body, taskPanelEl, resultPanelEl);
      contentSignature = signature;
    }
    prevButton.disabled = index === 0;
    nextButton.disabled = index === walk.length - 1;
    count.textContent = `${index + 1} of ${walk.length}`;
    setTab(openTab, focus);
    body.scrollTop = scrollTop;
  };

  /** The tasks Previous and Next walk: the shown scope's, or the whole plan
   * once a dependency jumped to a task the scope does not show. */
  function walkable() {
    return tasks.some((task) => task.id === openId) ? tasks : allTasks;
  }

  /** Open on Task; live updates never change the reader's tab. */
  const openTask = (task) => {
    openId = task.id;
    openerId = task.id;
    openTab = "task";
    scrollTop = 0;
    renderDialog();
    dialog.dataset.open = "true";
    dialog.showModal?.();
    taskTab.focus?.();
  };

  /** Another task of the same plan (a dependency or a step): show it instead. */
  const jumpTo = (task) => {
    if (task.id === openId) return;
    openId = task.id;
    scrollTop = 0;
    renderDialog();
  };

  const stepTo = (offset) => {
    const walk = walkable();
    const index = walk.findIndex((task) => task.id === openId);
    const target = walk[index + offset];
    if (!target) return;
    openId = target.id;
    scrollTop = 0;
    renderDialog();
  };
  prevButton.addEventListener("click", () => stepTo(-1));
  nextButton.addEventListener("click", () => stepTo(1));

  /** Open the dialog on a task of the plan, when the plan knows it. */
  const focusKnown = (id) => {
    if (!id) return false;
    const task = allTasks.find((candidate) => candidate.id === id || candidate.key === id);
    if (!task) return false;
    openTask(task);
    return true;
  };
  // A tracker deep link (?conversation=<id>&task=<id>): the card that knows
  // the task opens it; the others ignore the call.
  globalThis.document?.addEventListener?.("grid:focus-task", (event) => {
    if (focusKnown(event?.detail?.id)) globalThis.GridFocusTask = null;
  });

  /** Paint the head and the rows for the chosen scope. */
  const render = () => {
    const turnTasks = allTasks.filter((task) => task.turn);
    // Both scopes only when the request's share is a part of the plan.
    const scoped = turnTasks.length > 0 && turnTasks.length < allTasks.length;
    const shown = scoped ? scope : "all";
    tasks = shown === "turn" ? turnTasks : allTasks;
    scopes.hidden = !scoped;
    root.dataset.scope = shown;
    for (const [name, tab] of Object.entries(scopeTabs)) {
      const selected = name === shown;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      const size = name === "turn" ? turnTasks.length : allTasks.length;
      tab.textContent = `${SCOPES[name].label} · ${size}`;
    }

    const total = tasks.length;
    const counts = countStates(tasks);
    const done = counts.closed ?? 0;
    title.textContent = `${SCOPES[shown].heading} · ${done}/${total} done`;
    summary.textContent = Object.entries(STATES)
      .filter(([state]) => state !== "closed" && counts[state])
      .map(([state, { label }]) => `${counts[state]} ${label.toLowerCase()}`)
      .join(" · ");
    bar.style.width = `${total ? Math.round((done / total) * 100) : 0}%`;
    root.dataset.complete = String(total > 0 && done === total);

    // The row the reader is focused on keeps focus across the rebuild.
    const active = typeof document !== "undefined" ? document.activeElement : null;
    const focusedId =
      openId == null && active ? [...rows.entries()].find(([, button]) => button === active)?.[0] : null;
    rows.clear();
    replace(
      list,
      tasks.map((task) => {
        const row = taskRow(task, openTask);
        const button = row.querySelector("button");
        if (button) rows.set(task.id, button);
        return row;
      }),
    );
    if (focusedId != null) (rows.get(focusedId) ?? toggle).focus?.();
  };

  const chooseScope = (name, focus = false) => {
    scope = name;
    scopePinned = true;
    render();
    if (focus) scopeTabs[name].focus?.();
  };
  for (const [name, tab] of Object.entries(scopeTabs)) {
    tab.addEventListener("click", () => chooseScope(name));
    tab.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
      event.preventDefault?.();
      chooseScope(name === "turn" ? "all" : "turn", true);
    });
  }

  return {
    el: root,

    /** Whether the card shows a plan at all. */
    get hasPlan() {
      return allTasks.length > 0;
    },

    /**
     * Fold the card to its progress line, or open it. A card the reader
     * folded or opened themselves stays as they left it unless *force*.
     */
    setCollapsed(collapsed, { force = false } = {}) {
      if (collapsePinned && !force) return;
      setCollapsed(collapsed);
    },

    /**
     * Show a plan: either a `plan` step of the trace (`{plan}`) or the bare
     * plan the chat saved (`{tasks, counts, total}`), so a stored chat can
     * draw its card before a new turn re-sends a live step. `scoped: false`
     * drops which turn touched what - a saved plan is no one answer's own.
     */
    update(stepOrPlan, { scoped = true } = {}) {
      const plan = stepOrPlan?.plan ?? stepOrPlan;
      if (!plan?.tasks?.length) {
        allTasks = [];
        tasks = [];
        rows.clear();
        replace(list);
        dismiss();
        root.hidden = true;
        return;
      }
      root.hidden = false;
      allTasks = scoped ? plan.tasks : plan.tasks.map(({ turn, ...task }) => task);
      // The request's own share first, until the reader picks a scope.
      if (!scopePinned) scope = "turn";
      render();
      // The dialog follows the plan while it is open: same task, same tab,
      // same place in it - and closed when the plan no longer knows the task.
      if (openId != null) {
        if (allTasks.some((task) => task.id === openId)) {
          scrollTop = body.scrollTop ?? 0;
          renderDialog();
        } else {
          dismiss();
        }
      }
      // A deep link from the tracker (?conversation=<id>&task=<id>): open that
      // task once the plan reaches the screen, then forget the request.
      if (globalThis.GridFocusTask && focusKnown(globalThis.GridFocusTask)) globalThis.GridFocusTask = null;
    },

    /** Open a task of the plan from outside (a tracker deep link). */
    focusTask(id) {
      return focusKnown(id);
    },
  };
}
