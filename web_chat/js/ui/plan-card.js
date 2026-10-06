/**
 * The plan card: the conversation's tracker tasks and where each one stands.
 *
 * The server keeps it as a `plan` step of the turn's trace
 * (web_chat/plan_board.py) and re-sends that step whenever a task changes. It
 * sits above the answer rather than inside the reasoning panel: the panel
 * folds away when the turn ends, the plan is what the reader follows.
 */

import { h, replace } from "../lib/dom.js";

/** How each state reads, in the order the summary lists them. */
const STATES = {
  closed: { label: "Done", mark: "✓" },
  in_progress: { label: "In progress", mark: "●" },
  ready: { label: "Ready", mark: "○" },
  waiting: { label: "Waiting", mark: "◌" },
  blocked: { label: "Blocked", mark: "!" },
};

function taskRow(task) {
  const state = STATES[task.state] ?? STATES.ready;
  const waits = task.state === "waiting" && task.waits_for?.length ? `waits for ${task.waits_for.join(", ")}` : "";
  return h(
    "li.planCard__task",
    { dataset: { state: task.state } },
    h("span.planCard__mark", { text: state.mark, title: state.label, "aria-label": state.label }),
    h("span.planCard__title", { text: task.title }),
    h("span.planCard__meta", { text: [task.id, waits].filter(Boolean).join(" · ") }),
  );
}

export function createPlanCard() {
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

  return {
    el: root,

    /** Show the plan a `plan` step carries. */
    update(step) {
      const plan = step.plan;
      if (!plan?.tasks?.length) return;
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
      replace(list, plan.tasks.map(taskRow));
    },
  };
}
