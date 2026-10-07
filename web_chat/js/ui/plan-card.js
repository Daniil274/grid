/**
 * The plan card: the conversation's tracker tasks and where each one stands.
 *
 * The server keeps it as a `plan` step of the turn's trace
 * (web_chat/plan_board.py) and re-sends that step whenever a task changes. It
 * sits above the answer rather than inside the reasoning panel: the panel
 * folds away when the turn ends, the plan is what the reader follows.
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

let dialogSequence = 0;

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
  const list = h("ol.planCard__tasks");
  const toggle = h(
    "button.planCard__head",
    { type: "button", "aria-expanded": "true" },
    title,
    summary,
    h("span.planCard__bar", {}, bar),
  );
  const root = h("section.planCard", { hidden: true }, toggle, list);
  toggle.addEventListener("click", () => {
    const open = root.classList.toggle("is-collapsed") === false;
    toggle.setAttribute("aria-expanded", String(open));
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

  let tasks = []; // the plan as it stands
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
    const index = tasks.findIndex((task) => task.id === openId);
    if (index < 0) return;
    const task = tasks[index];
    const state = STATES[task.state] ?? STATES.ready;
    replace(stateBadge, h("span.planCard__mark", { text: state.mark, "aria-label": state.label }), h("span", { text: state.label }));
    stateBadge.dataset.state = task.state;
    idBadge.textContent = task.id ?? "";
    taskTitle.textContent = task.title;
    const signature = JSON.stringify([
      {
        id: task.id, description: task.description, acceptance: task.acceptance,
        notes: task.notes, close_reason: task.close_reason, report: task.report,
        closed: task.state === "closed", depends_on: task.depends_on,
      },
      tasks.filter((target) => (task.depends_on ?? []).some((dep) => dep === target.id || dep === target.key))
        .map(({ id, key, title, state }) => ({ id, key, title, state })),
    ]);
    if (signature !== contentSignature) {
      const active = document.activeElement;
      if (active && body.contains(active)) focus = true;
      taskPanelEl = taskPanel(task, tasks, jumpTo, ids);
      resultPanelEl = resultPanel(task, ids);
      replace(body, taskPanelEl, resultPanelEl);
      contentSignature = signature;
    }
    prevButton.disabled = index === 0;
    nextButton.disabled = index === tasks.length - 1;
    count.textContent = `${index + 1} of ${tasks.length}`;
    setTab(openTab, focus);
    body.scrollTop = scrollTop;
  };

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
    const index = tasks.findIndex((task) => task.id === openId);
    const target = tasks[index + offset];
    if (!target) return;
    openId = target.id;
    scrollTop = 0;
    renderDialog();
  };
  prevButton.addEventListener("click", () => stepTo(-1));
  nextButton.addEventListener("click", () => stepTo(1));

  return {
    el: root,

    /** Show the plan a `plan` step carries. */
    update(step) {
      const plan = step.plan;
      if (!plan?.tasks?.length) {
        tasks = [];
        rows.clear();
        replace(list);
        dismiss();
        root.hidden = true;
        return;
      }
      root.hidden = false;
      const counts = plan.counts ?? {};
      const done = counts.closed ?? 0;
      title.textContent = `Plan · ${done}/${plan.total} done`;
      summary.textContent = Object.entries(STATES)
        .filter(([state]) => state !== "closed" && counts[state])
        .map(([state, { label }]) => `${counts[state]} ${label.toLowerCase()}`)
        .join(" · ");
      bar.style.width = `${plan.total ? Math.round((done / plan.total) * 100) : 0}%`;
      root.dataset.complete = String(plan.total > 0 && done === plan.total);
      tasks = plan.tasks;
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
      // The dialog follows the plan while it is open: same task, same tab,
      // same place in it - and closed when the plan no longer knows the task.
      if (openId != null) {
        if (tasks.some((task) => task.id === openId)) {
          scrollTop = body.scrollTop ?? 0;
          renderDialog();
        } else {
          dismiss();
        }
      }
    },
  };
}
