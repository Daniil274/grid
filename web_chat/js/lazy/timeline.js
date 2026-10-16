/**
 * The plan over the next days, as one SVG:
 *
 * - your hours, shaded across every row: agents keep off them;
 * - each planned step, a block in its task's color (the running one pulses);
 * - each deadline, a flag in the same color;
 * - each window's use over time - yours and the agents', stacked - with the
 *   line of your reserve; a window's reset shows as the fall back to zero;
 * - now.
 *
 * `layout` is the arithmetic (testable without a DOM); `drawTimeline` builds
 * the SVG from it.
 */

import { colorSlot, moment, points } from "./format.js";

const SVG = "http://www.w3.org/2000/svg";
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const GUTTER = 104;
const PAD = 12;
const ROWS = { axis: 26, steps: 40, window: 54, gap: 10 };

function s(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value != null) node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child == null) continue;
    node.append(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

/** Where things go: the time span and how a moment maps to x. */
export function layout({ now, width, span = 7 * 86400, lead = 3600 }) {
  const start = now - lead;
  const end = now + span;
  const inner = Math.max(200, width - GUTTER - PAD);
  const x = (t) => GUTTER + ((Math.min(Math.max(t, start), end) - start) / (end - start)) * inner;
  const days = [];
  const first = new Date(start * 1000);
  let day = new Date(first.getFullYear(), first.getMonth(), first.getDate() + 1).getTime() / 1000;
  while (day < end) {
    days.push(day);
    const date = new Date(day * 1000);
    day = new Date(date.getFullYear(), date.getMonth(), date.getDate() + 1).getTime() / 1000;
  }
  return { start, end, x, days, inner, width: GUTTER + inner + PAD };
}

/** The area under a series of (t, value 0..100) points, between y0 (0) and y0 - height (100). */
export function areaPath(times, values, base, x, y0, height) {
  if (!times.length) return "";
  const y = (value) => y0 - (Math.min(100, Math.max(0, value)) / 100) * height;
  const top = times.map((t, i) => `${x(t).toFixed(1)},${y(values[i] + (base ? base[i] : 0)).toFixed(1)}`);
  const bottom = times.map((t, i) => `${x(t).toFixed(1)},${y(base ? base[i] : 0).toFixed(1)}`).reverse();
  return `M${top.join("L")}L${bottom.join("L")}Z`;
}

function windowRow(name, data, geometry, y0, label) {
  const { x } = geometry;
  const height = ROWS.window - 8;
  const bottom = y0 + ROWS.window - 4;
  const curve = data.curve;
  const reserveY = bottom - ((100 - data.safety) / 100) * height;
  const resets = (data.periods || []).slice(1).map((period) => period.start);
  return s("g", { class: "tl__window", "data-window": name },
    s("rect", { x: GUTTER, y: bottom - height, width: geometry.inner, height, class: "tl__windowBg" }),
    s("path", { d: areaPath(curve.t, curve.user, null, x, bottom, height), class: "tl__user" }),
    s("path", { d: areaPath(curve.t, curve.agents, curve.user, x, bottom, height), class: "tl__agents" }),
    s("line", { x1: GUTTER, x2: GUTTER + geometry.inner, y1: reserveY, y2: reserveY, class: "tl__reserve" }),
    resets.filter((t) => t < geometry.end).map((t) =>
      s("line", { x1: x(t), x2: x(t), y1: bottom - height, y2: bottom, class: "tl__reset" },
        s("title", {}, `${label} resets · ${moment(t, geometry.now)}`))),
    s("text", { x: 10, y: y0 + ROWS.window / 2 + 4, class: "tl__label" }, label),
  );
}

/**
 * @param {object} state the page's state (GET /api/lazy)
 * @param {number} width the space to fill
 * @param {(taskId: string|null) => void} [onHover] a step or a deadline is pointed at
 */
export function drawTimeline(state, width, onHover = () => {}) {
  const now = state.now;
  const geometry = { ...layout({ now, width }), now };
  const { x } = geometry;
  const tasks = new Map(state.tasks.map((task) => [task.id, task]));
  const top = ROWS.axis;
  const stepsY = top + 4;
  const fiveY = stepsY + ROWS.steps + ROWS.gap;
  const weekY = fiveY + ROWS.window + 2;
  const height = weekY + ROWS.window + 6;

  const svg = s("svg", {
    class: "tl", width: geometry.width, height, viewBox: `0 0 ${geometry.width} ${height}`,
    role: "img", "aria-label": "Plan of the next seven days",
  });

  svg.append(s("defs", {},
    s("pattern", { id: "tl-hatch", width: 6, height: 6, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" },
      s("line", { x1: 0, y1: 0, x2: 0, y2: 6, class: "tl__hatchLine" }))));

  // Your hours, behind everything.
  for (const [a, b] of state.my_hours || []) {
    if (b <= geometry.start || a >= geometry.end) continue;
    svg.append(s("rect", { x: x(a), y: top, width: Math.max(1, x(b) - x(a)), height: height - top, class: "tl__mine" },
      s("title", {}, `Your hours · ${moment(a, now)} – ${moment(b, now)}`)));
  }

  // The axis: days and the hours that matter.
  const axis = s("g", { class: "tl__axis" });
  for (const day of geometry.days) {
    const date = new Date(day * 1000);
    axis.append(
      s("line", { x1: x(day), x2: x(day), y1: 6, y2: height, class: "tl__day" }),
      s("text", { x: x(day) + 6, y: 17, class: "tl__dayLabel" }, `${DAYS[date.getDay()]} ${date.getDate()}`),
    );
    for (const hour of [6, 12, 18]) {
      const t = day + hour * 3600;
      if (t < geometry.end) axis.append(s("line", { x1: x(t), x2: x(t), y1: top - 4, y2: top, class: "tl__tick" }));
    }
  }
  svg.append(axis);
  svg.append(s("text", { x: 10, y: stepsY + ROWS.steps / 2 + 4, class: "tl__label" }, "Steps"));

  // Each window's use, then the steps on top.
  svg.append(windowRow("five_hour", state.plan.windows.five_hour, geometry, fiveY, "5-hour"));
  svg.append(windowRow("seven_day", state.plan.windows.seven_day, geometry, weekY, "Weekly"));

  const steps = s("g", { class: "tl__steps" });
  for (const placement of state.plan.placements) {
    if (placement.end <= geometry.start || placement.start >= geometry.end) continue;
    const task = tasks.get(placement.task_id);
    const x0 = x(placement.start);
    const w = Math.max(5, x(placement.end) - x0);
    const label = `${task?.title ?? "Task"} · ${placement.kind} · ${moment(placement.start, now)}–${moment(placement.end, now)}`
      + ` · 5-hour ${points(placement.cost.five_hour)}, weekly ${points(placement.cost.seven_day)} pts`;
    const block = s("rect", {
      x: x0, y: stepsY + 6, width: w, height: ROWS.steps - 12, rx: 4,
      class: `tl__step c${colorSlot(placement.task_id)}${placement.running ? " is-running" : ""}`,
      "data-task": placement.task_id,
    }, s("title", {}, label));
    block.addEventListener("mouseenter", () => onHover(placement.task_id));
    block.addEventListener("mouseleave", () => onHover(null));
    steps.append(block);
    if (w > 26) {
      steps.append(s("text", { x: x0 + w / 2, y: stepsY + ROWS.steps / 2 + 4, class: "tl__stepLabel" }, placement.kind[0].toUpperCase()));
    }
  }
  svg.append(steps);

  // Deadlines: a flag in the task's color.
  for (const task of state.tasks) {
    if (!task.deadline || ["done", "cancelled", "failed"].includes(task.status)) continue;
    if (task.deadline < geometry.start || task.deadline > geometry.end) continue;
    const dx = x(task.deadline);
    const flag = s("g", { class: `tl__deadline c${colorSlot(task.id)}`, "data-task": task.id },
      s("line", { x1: dx, x2: dx, y1: top - 2, y2: height - 2 }),
      s("path", { d: `M${dx},${top - 2}l9,4l-9,4z` }),
      s("title", {}, `${task.title} · deadline ${moment(task.deadline, now)}`));
    flag.addEventListener("mouseenter", () => onHover(task.id));
    flag.addEventListener("mouseleave", () => onHover(null));
    svg.append(flag);
  }

  // Now.
  const nx = x(now);
  svg.append(s("g", { class: "tl__now" },
    s("line", { x1: nx, x2: nx, y1: 4, y2: height }),
    s("text", { x: nx + 4, y: top - 8 }, "now")));
  return svg;
}
