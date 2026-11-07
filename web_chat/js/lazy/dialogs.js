/**
 * The lazy agents page's dialogs - native <dialog>s: focus stays inside,
 * Escape closes them.
 *
 * - A new task: the form, and as it is filled in, where the task would land
 *   in the plan (POST /api/lazy/preview) - before anything is saved.
 * - A deadline: the plan as it is, and as it would be with the new deadline
 *   (POST .../preview), with the effect on the other tasks.
 * - The settings: which subscription, how much of each window, how much to
 *   keep free, and the hours that are yours.
 */

import { debounce, h, icon, replace } from "../lib/dom.js";
import { api } from "../net/api.js";
import { ICONS } from "../ui/icons.js";
import {
  STATES, duration, fromLocalInput, hoursSummary, moment, points, quickDeadlines, toLocalInput, tokens, waitText,
} from "./format.js";
import { REPEAT_CHOICES, REPEAT_HINT, repeatHours } from "./repeat.js";

const nowOf = (state) => state.now + (Date.now() / 1000 - state.loadedAt);

function chip(stateKey) {
  const info = STATES[stateKey] ?? STATES.flexible;
  return h("span.stateChip", { dataset: { tone: info.tone }, title: info.hint }, info.label);
}

function dialogShell(id, title, ...body) {
  const dialog = document.getElementById(id);
  replace(dialog, h("form.lazyDialog__form", { method: "dialog" },
    h("header.lazyDialog__head", {},
      h("h2", { id: `${id}-title`, text: title }),
      h("button.iconBtn", { type: "button", "aria-label": "Close", on: { click: () => dialog.close() } }, icon(ICONS.close, { size: 16 }))),
    ...body));
  dialog.showModal();
  return dialog;
}

function segmented(name, options, value, onChange) {
  return h("div.segmented", { role: "radiogroup", "aria-label": name },
    options.map(([key, label, hint]) => {
      const button = h("button", {
        type: "button", role: "radio", title: hint ?? "", "aria-checked": String(key === value), dataset: { value: key },
        on: {
          click: () => {
            for (const other of button.parentElement.children) other.setAttribute("aria-checked", String(other === button));
            onChange(key);
          },
        },
      }, label);
      return button;
    }));
}

function deadlinePicker(state, initial, onChange) {
  const input = h("input.control", { type: "datetime-local", value: toLocalInput(initial) });
  input.addEventListener("change", () => onChange(fromLocalInput(input.value)));
  const quick = quickDeadlines(nowOf(state)).map(({ label, t }) => h("button.chipBtn", {
    type: "button", on: { click: () => { input.value = toLocalInput(t); onChange(t); } },
  }, label));
  const none = h("button.chipBtn", { type: "button", on: { click: () => { input.value = ""; onChange(null); } } }, "No deadline");
  return h("div.deadlinePicker", {}, input, h("div.chipRow", {}, quick, none));
}

function planSummary(state, planned, steps, placements) {
  const now = nowOf(state);
  if (!planned) return h("p.preview__muted", { text: "Fill in the task to see where it lands." });
  const rows = (placements || []).map((placement) => {
    const step = steps?.find((s) => s.kind === placement.kind);
    return h("li", {},
      h("span.preview__when", { text: moment(placement.start, now) }),
      h("span.preview__what", { text: step?.title ?? placement.kind }),
      h("span.preview__cost", { text: `${points(placement.cost.five_hour)} / ${points(placement.cost.seven_day)} pts` }));
  });
  const finish = planned.finish_at ? `Ready ${moment(planned.finish_at, now)} (${duration(planned.finish_at - now)} from now)` : "No time found in the next two weeks";
  return h("div.preview__plan", {},
    h("div.preview__head", {}, chip(planned.state), h("strong", { text: finish })),
    planned.wait ? h("p.preview__wait", { text: waitText(planned.wait, now) }) : null,
    rows.length ? h("ol.preview__steps", {}, rows) : null,
    planned.options?.length
      ? h("div.preview__ways", {}, h("p", { text: "To make the deadline:" }),
        h("ul", {}, planned.options.map((option) => h("li", {
          text: option.kind === "deadline" ? `Move it to ${moment(option.change.task.deadline, now)}` : option.text,
        }))))
      : null,
  );
}

function othersSummary(state, others) {
  if (!others?.length) return null;
  const now = nowOf(state);
  return h("div.preview__others", {},
    h("p", { text: "Other tasks change too:" }),
    h("ul", {}, others.map((other) => h("li", {},
      h("strong", { text: other.title }), ": ",
      chip(other.before.state), " → ", chip(other.after.state),
      other.after.finish_at ? ` · ready ${moment(other.after.finish_at, now)}` : ""))));
}

// -- a new task ---------------------------------------------------------------------------------
export function openTaskDialog(state, act) {
  state.loadedAt ??= Date.now() / 1000;
  // Every subscription agent is usable: a task spends its own agent's subscription,
  // planned in that subscription's pool. The settings' subscription comes first.
  const usable = state.agents.filter((choice) => choice.account && !choice.problem)
    .sort((a, b) => Number(b.account === state.account.key) - Number(a.account === state.account.key));
  const accountLabel = (key) => (key === state.account.key ? state.account.label
    : state.pools?.find((pool) => pool.account.key === key)?.account.label ?? key);
  const draft = {
    title: "", brief: "", acceptance: "", optional: "", deadline: null, size: "M", priority: "normal",
    system: usable[0]?.system ?? null, agent: usable[0]?.agent ?? null, cap: null,
    repeat_hours: 0,
  };
  const preview = h("aside.preview", { "aria-live": "polite" }, h("p.preview__muted", { text: "Fill in the task to see where it lands." }));
  const error = h("p.lazyDialog__error", { role: "alert" });
  const refresh = debounce(async () => {
    if (!draft.system) return;
    try {
      const result = await api.lazyPreviewNew(draft);
      error.textContent = "";
      replace(preview,
        h("h3", { text: "Where it lands" }),
        planSummary(state, result.plan, result.steps, result.placements),
        h("p.preview__total", {
          text: `Estimate: ${result.steps.map((step) => `${step.title} ~${tokens(step.tokens)}`).join(" · ")}`,
        }),
        othersSummary(state, result.others));
    } catch (problem) {
      replace(preview, h("p.preview__muted", { text: problem.message }));
    }
  }, 350);
  const field = (label, control, hint) => h("label.field", {}, h("span.field__label", { text: label }), control, hint ? h("span.field__hint", { text: hint }) : null);
  const text = (key, placeholder, area = false) => {
    const control = h(area ? "textarea.control.control--area.control--prose" : "input.control", { placeholder, value: area ? undefined : draft[key] });
    control.addEventListener("input", () => { draft[key] = control.value; refresh(); });
    return control;
  };
  const agentSelect = h("select.control", {},
    state.agents.map((choice) => h("option", {
      value: `${choice.system}/${choice.agent}`,
      disabled: !choice.account || !!choice.problem,
      selected: choice.system === draft.system && choice.agent === draft.agent,
      title: choice.problem ?? "",
    }, `${choice.label} · ${choice.system_label}${choice.problem ? " — unavailable" : choice.account !== state.account.key ? ` · ${accountLabel(choice.account)}` : ""}`)));
  const agentHint = h("span.field__hint");
  const showAccount = () => {
    const choice = state.agents.find((c) => c.system === draft.system && c.agent === draft.agent);
    agentHint.textContent = choice ? `Runs on your ${accountLabel(choice.account)}.` : "No agent of yours runs on a subscription.";
  };
  showAccount();
  agentSelect.addEventListener("change", () => {
    [draft.system, draft.agent] = agentSelect.value.split("/");
    showAccount();
    refresh();
  });
  const sizes = state.sizes;
  const sizeHint = (size) => {
    const total = Object.entries(sizes[size]).filter(([kind]) => ["scout", "work", "verify"].includes(kind))
      .reduce((sum, [, value]) => sum + value.tokens, 0);
    return `~${tokens(total)} tokens`;
  };

  // How often the task repeats: the service takes hours between rounds, counted
  // from the previous round's deadline - an interval, not a time of day.
  let repeatChoice = "once";
  const repeatProblem = h("span.field__problem", { role: "alert" });
  const repeatInput = h("input.control", {
    type: "number", min: "1", step: "1", inputmode: "numeric", placeholder: "Hours, for example 36",
  });
  repeatInput.hidden = true;
  const applyRepeat = () => {
    const { hours, problem } = repeatHours(repeatChoice, repeatInput.value);
    draft.repeat_hours = problem ? null : hours;
    repeatInput.hidden = repeatChoice !== "custom";
    repeatProblem.textContent = problem;
    refresh();
  };
  repeatInput.addEventListener("input", applyRepeat);

  const dialog = dialogShell("task-dialog", "New lazy task",
    h("div.lazyDialog__grid", {},
      h("div.lazyDialog__fields", {},
        field("Title", text("title", "Weekly report on the API errors")),
        field("What to do", text("brief", "What the result is, where to look, what to keep in mind", true)),
        field("Done when", text("acceptance", "How to check it: tests pass, the report covers A and B", true),
          "Verification checks exactly this, and its budget is set aside up front."),
        field("Optional part", text("optional", "Charts, extra examples - first to go when time runs short"),
          "Kept as a step of its own, dropped first when the deadline is near."),
        h("label.field", {}, h("span.field__label", { text: "Agent" }), agentSelect, agentHint),
        field("Deadline", deadlinePicker(state, null, (t) => { draft.deadline = t; refresh(); }),
          "Leave it open and the task runs whenever there is room."),
        field("Repeat", h("div.repeatPicker", {},
          segmented("Repeat", REPEAT_CHOICES, repeatChoice, (value) => { repeatChoice = value; applyRepeat(); }),
          repeatInput, repeatProblem), REPEAT_HINT),
        h("div.field__pair", {},
          field("Size", segmented("Size", [
            ["S", "Small", sizeHint("S")], ["M", "Medium", sizeHint("M")], ["L", "Large", sizeHint("L")],
          ], draft.size, (value) => { draft.size = value; refresh(); }), "The scout corrects it."),
          field("Priority", segmented("Priority", [["low", "Low"], ["normal", "Normal"], ["high", "High"]], draft.priority,
            (value) => { draft.priority = value; refresh(); })))),
      preview),
    error,
    h("footer.lazyDialog__actions", {},
      h("button.btn.btn--ghost", { type: "button", on: { click: () => dialog.close() } }, "Cancel"),
      h("button.btn.btn--primary", {
        type: "button",
        on: {
          click: async () => {
            if (!draft.title.trim()) {
              error.textContent = "Give the task a title.";
              return;
            }
            const repeat = repeatHours(repeatChoice, repeatInput.value);
            if (repeat.problem) {
              error.textContent = repeat.problem;
              repeatProblem.textContent = repeat.problem;
              return;
            }
            draft.repeat_hours = repeat.hours;
            const next = await act(api.lazyCreate(draft), "Planned");
            if (next) dialog.close();
          },
        },
      }, icon(ICONS.plus, { size: 14 }), "Plan it")),
  );
  dialog.querySelector("input")?.focus();
  refresh();
}

// -- a deadline -----------------------------------------------------------------------------------
export function openDeadlineDialog(task, planned, state, act) {
  state.loadedAt ??= Date.now() / 1000;
  const now = nowOf(state);
  let deadline = task.deadline ?? null;
  const before = h("div.compare__side", {},
    h("h3", { text: "Now" }),
    h("p", { text: task.deadline ? `Deadline ${moment(task.deadline, now)}` : "No deadline" }),
    planned ? chip(planned.state) : null,
    planned?.finish_at ? h("p", { text: `Ready ${moment(planned.finish_at, now)}` }) : null);
  const after = h("div.compare__side.compare__side--after", {}, h("h3", { text: "With the change" }), h("p.preview__muted", { text: "Pick a new deadline." }));
  const others = h("div");
  const refresh = debounce(async () => {
    try {
      const result = await api.lazyPreview(task.id, { deadline });
      const next = result.after;
      replace(after,
        h("h3", { text: "With the change" }),
        h("p", { text: deadline ? `Deadline ${moment(deadline, now)}` : "No deadline" }),
        next ? chip(next.state) : null,
        next?.finish_at ? h("p", { text: `Ready ${moment(next.finish_at, now)}` }) : h("p", { text: "No time found in the next two weeks" }),
        next?.late_by ? h("p.compare__late", { text: `${duration(next.late_by)} late` }) : null,
        next?.options?.length
          ? h("ul.compare__ways", {}, next.options.map((option) => h("li", {
            text: option.kind === "deadline" ? `or move it to ${moment(option.change.task.deadline, now)}` : `or: ${option.text.toLowerCase()}`,
          })))
          : null);
      replace(others, othersSummary(state, result.others));
    } catch (problem) {
      replace(after, h("p.preview__muted", { text: problem.message }));
    }
  }, 250);
  const dialog = dialogShell("deadline-dialog", `Deadline · ${task.title}`,
    h("p.lazyDialog__lead", { text: "Changing it replans everything at once; nothing runs any sooner than the budget allows." }),
    deadlinePicker(state, deadline, (t) => { deadline = t; refresh(); }),
    h("div.compare", {}, before, h("span.compare__arrow", { "aria-hidden": "true" }, "→"), after),
    others,
    h("footer.lazyDialog__actions", {},
      h("button.btn.btn--ghost", { type: "button", on: { click: () => dialog.close() } }, "Keep it"),
      h("button.btn.btn--primary", {
        type: "button",
        on: { click: async () => { if (await act(api.lazyUpdate(task.id, { deadline }), "Deadline changed")) dialog.close(); } },
      }, "Change the deadline")));
  if (task.deadline) refresh();
}

// -- the settings ---------------------------------------------------------------------------------
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** Hour cells (7 x 24) from minute ranges, and back. */
export function cellsOf(hours) {
  const cells = Array.from({ length: 7 }, () => Array(24).fill(false));
  for (const [day, ranges] of Object.entries(hours || {})) {
    for (const [a, b] of ranges) {
      for (let hour = 0; hour < 24; hour += 1) {
        if (a < (hour + 1) * 60 && b > hour * 60) cells[Number(day)][hour] = true;
      }
    }
  }
  return cells;
}

export function hoursOf(cells) {
  const hours = {};
  cells.forEach((row, day) => {
    const ranges = [];
    let start = null;
    row.forEach((on, hour) => {
      if (on && start === null) start = hour;
      if (!on && start !== null) { ranges.push([start * 60, hour * 60]); start = null; }
    });
    if (start !== null) ranges.push([start * 60, 1440]);
    if (ranges.length) hours[day] = ranges;
  });
  return hours;
}

function hoursGrid(cells, onChange) {
  let painting = null;
  const grid = h("div.hoursGrid", { role: "grid", "aria-label": "Your hours: click or drag to mark them" },
    h("div.hoursGrid__corner"),
    Array.from({ length: 24 }, (_, hour) => h("div.hoursGrid__hour", { text: hour % 3 === 0 ? String(hour).padStart(2, "0") : "" })),
    cells.flatMap((row, day) => [
      h("div.hoursGrid__day", { text: WEEKDAYS[day] }),
      ...row.map((on, hour) => h("button.hoursGrid__cell", {
        type: "button", "aria-pressed": String(on), title: `${WEEKDAYS[day]} ${String(hour).padStart(2, "0")}:00`,
        dataset: { day, hour },
      })),
    ]));
  const set = (cell, value) => {
    cells[Number(cell.dataset.day)][Number(cell.dataset.hour)] = value;
    cell.setAttribute("aria-pressed", String(value));
  };
  grid.addEventListener("pointerdown", (event) => {
    const cell = event.target.closest(".hoursGrid__cell");
    if (!cell) return;
    event.preventDefault();
    painting = cell.getAttribute("aria-pressed") !== "true";
    set(cell, painting);
    grid.setPointerCapture?.(event.pointerId);
  });
  grid.addEventListener("pointermove", (event) => {
    if (painting === null) return;
    const cell = document.elementFromPoint(event.clientX, event.clientY)?.closest?.(".hoursGrid__cell");
    if (cell && grid.contains(cell)) set(cell, painting);
  });
  const stop = () => {
    if (painting !== null) onChange();
    painting = null;
  };
  grid.addEventListener("pointerup", stop);
  grid.addEventListener("pointercancel", stop);
  grid.addEventListener("keydown", (event) => {
    const cell = event.target.closest(".hoursGrid__cell");
    if (cell && (event.key === " " || event.key === "Enter")) {
      event.preventDefault();
      set(cell, cell.getAttribute("aria-pressed") !== "true");
      onChange();
    }
  });
  return grid;
}

function slider(label, value, max, onChange, hint) {
  const output = h("output.slider__value", { text: `${value}%` });
  const input = h("input.slider__input", { type: "range", min: "0", max: String(max), step: "0.5", value: String(value) });
  input.addEventListener("input", () => {
    output.textContent = `${input.value}%`;
    onChange(Number(input.value));
  });
  return h("label.slider", {}, h("span.slider__label", { text: label }), input, output, hint ? h("span.field__hint", { text: hint }) : null);
}

export function openSettingsDialog(state, act) {
  const settings = structuredClone(state.settings);
  const cells = cellsOf(settings.routine.hours);
  const accounts = new Map();
  for (const choice of state.agents) {
    if (choice.account && !choice.problem) accounts.set(choice.account, (accounts.get(choice.account) ?? 0) + 1);
  }
  const summary = h("p.field__hint", { text: hoursSummary(settings.routine.hours) });
  const capacity = h("div.capacity", { hidden: settings.account !== "chatgpt" },
    h("p.field__hint", { text: "The ChatGPT plan reports nothing about its limits. Say how many tokens fill each window, and use is counted against that." }),
    h("div.field__pair", {},
      ["five_hour", "seven_day"].map((name) => {
        const input = h("input.control", { type: "number", min: "100000", step: "100000", value: String(settings.capacity_tokens[name]) });
        input.addEventListener("change", () => { settings.capacity_tokens[name] = Number(input.value) || settings.capacity_tokens[name]; });
        return h("label.field", {}, h("span.field__label", { text: name === "five_hour" ? "5-hour window, tokens" : "Weekly limit, tokens" }), input);
      })));
  const accountCards = h("div.accountCards", { role: "radiogroup", "aria-label": "Subscription" },
    [["claude-code", "Claude Code subscription", "Reports both windows with every answer: exact."], ["chatgpt", "ChatGPT plan", "Reports nothing: estimated from your token use."]]
      .map(([key, label, hint]) => {
        const count = accounts.get(key) ?? 0;
        const card = h("button.accountCard", {
          type: "button", role: "radio", "aria-checked": String(settings.account === key), disabled: count === 0 && settings.account !== key,
          on: {
            click: () => {
              settings.account = key;
              for (const other of accountCards.children) other.setAttribute("aria-checked", String(other === card));
              capacity.hidden = key !== "chatgpt";
            },
          },
        }, h("strong", { text: label }), h("span", { text: hint }),
        h("span.accountCard__count", { text: count ? `${count} agent${count === 1 ? "" : "s"} of yours run on it` : "None of your agents runs on it" }));
        return card;
      }));
  const allowance = (name, label) => h("fieldset.allowance", {},
    h("legend", { text: label }),
    slider("Agents may use", settings.allowances[name].share, 50, (value) => { settings.allowances[name].share = value; },
      "Of each period, for all lazy tasks together."),
    slider("Keep free for you", settings.allowances[name].safety, 40, (value) => { settings.allowances[name].safety = value; },
      "On top of what you are expected to use."));
  const toggle = (key, label, hint) => {
    const input = h("input", { type: "checkbox", checked: settings[key] });
    input.addEventListener("change", () => { settings[key] = input.checked; });
    return h("label.toggle", {}, input, h("span.toggle__track"), h("span.toggle__text", {}, h("strong", { text: label }), h("span", { text: hint })));
  };
  const tz = h("input.control", { value: settings.routine.timezone, spellcheck: "false" });
  const preset = (label, hours) => h("button.chipBtn", {
    type: "button",
    on: {
      click: () => {
        const fresh = cellsOf(hours);
        fresh.forEach((row, day) => row.forEach((on, hour) => { cells[day][hour] = on; }));
        replace(gridHost, hoursGrid(cells, changed));
        changed();
      },
    },
  }, label);
  const changed = () => { summary.textContent = hoursSummary(hoursOf(cells)); };
  const gridHost = h("div.hoursGrid__host", {}, hoursGrid(cells, changed));

  const dialog = dialogShell("settings-dialog", "Lazy agents · settings",
    h("section.settingsBlock", {}, h("h3", { text: "Subscription" }), accountCards, capacity),
    h("section.settingsBlock", {},
      h("h3", { text: "How much the agents may take" }),
      h("div.field__pair", {}, allowance("five_hour", "5-hour window"), allowance("seven_day", "Weekly limit"))),
    h("section.settingsBlock", {},
      h("h3", { text: "Your hours" }),
      h("p.field__hint", { text: "When the subscription is yours: no step starts in them, and each step ends before they begin." }),
      h("div.chipRow", {},
        preset("Work days 10–19", Object.fromEntries([0, 1, 2, 3, 4].map((day) => [day, [[600, 1140]]]))),
        preset("Every day 9–23", Object.fromEntries([0, 1, 2, 3, 4, 5, 6].map((day) => [day, [[540, 1380]]]))),
        preset("None", {})),
      gridHost, summary,
      h("label.field", {}, h("span.field__label", { text: "Time zone" }), tz)),
    h("section.settingsBlock", {},
      toggle("yield_to_user", "Yield when I start working", "A running step stops at a safe point when you start a turn of your own; it continues later.")),
    h("footer.lazyDialog__actions", {},
      h("button.btn.btn--ghost", { type: "button", on: { click: () => dialog.close() } }, "Cancel"),
      h("button.btn.btn--primary", {
        type: "button",
        on: {
          click: async () => {
            settings.routine = { timezone: tz.value.trim() || "UTC", hours: hoursOf(cells) };
            if (await act(api.lazySettings(settings), "Settings saved")) dialog.close();
          },
        },
      }, "Save")),
  );
}
