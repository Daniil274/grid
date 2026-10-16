/**
 * Words and numbers for the lazy agents page: durations, moments, points and
 * the states a task can be in. Pure functions of their arguments (and the
 * browser's time zone), so the page and its tests read them the same way.
 */

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Each state: its label, the tone of its chip and what it means. */
export const STATES = {
  running: { label: "Running", tone: "accent", hint: "A step is working now" },
  on_track: { label: "On track", tone: "ok", hint: "Planned to finish before the deadline" },
  flexible: { label: "Whenever there is room", tone: "muted", hint: "No deadline: it runs as the budget allows" },
  at_risk: { label: "At risk", tone: "warn", hint: "The plan finishes after the deadline" },
  running_late: { label: "Running late", tone: "warn", hint: "Running, but the rest finishes after the deadline" },
  blocked: { label: "No plan found", tone: "danger", hint: "Some step finds no room in the next two weeks" },
  paused: { label: "Paused", tone: "muted", hint: "Nothing runs until you resume it" },
  done: { label: "Done", tone: "ok", hint: "Every step finished" },
  failed: { label: "Failed", tone: "danger", hint: "A step kept failing; the task stopped" },
  cancelled: { label: "Cancelled", tone: "muted", hint: "Stopped for good" },
};

/** The state a task shows: its own status when finished or paused, else the plan's verdict. */
export function stateOf(task, planned) {
  if (["done", "failed", "cancelled"].includes(task.status)) return task.status;
  if (task.status === "paused") return "paused";
  return planned?.state && STATES[planned.state] ? planned.state : "flexible";
}

const pad = (value) => String(value).padStart(2, "0");

/** "2 d 4 h", "3 h 10 m", "45 m", "< 1 m". */
export function duration(seconds) {
  const total = Math.max(0, Math.round(Math.abs(seconds) / 60));
  if (total < 1) return "< 1 m";
  const days = Math.floor(total / 1440);
  const hours = Math.floor((total % 1440) / 60);
  const minutes = total % 60;
  if (days) return hours ? `${days} d ${hours} h` : `${days} d`;
  if (hours) return minutes ? `${hours} h ${minutes} m` : `${hours} h`;
  return `${minutes} m`;
}

/** "in 2 h 10 m", "5 m ago", "now". */
export function relative(t, now) {
  const delta = t - now;
  if (Math.abs(delta) < 45) return "now";
  return delta > 0 ? `in ${duration(delta)}` : `${duration(delta)} ago`;
}

function startOfDay(date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

/** "21:40" today, "Tomorrow 09:00", "Fri 18:00" within a week, "Oct 24, 18:00" beyond. */
export function moment(t, now) {
  const date = new Date(t * 1000);
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  const days = Math.round((startOfDay(date) - startOfDay(new Date(now * 1000))) / 86_400_000);
  if (days === 0) return time;
  if (days === 1) return `Tomorrow ${time}`;
  if (days === -1) return `Yesterday ${time}`;
  if (days > 1 && days < 7) return `${DAYS[date.getDay()]} ${time}`;
  return `${MONTHS[date.getMonth()]} ${date.getDate()}, ${time}`;
}

/** Points of a window: "3.2", "0.45", "12". */
export function points(value) {
  const number = Number(value) || 0;
  if (number === 0) return "0";
  const digits = Math.abs(number) < 1 ? 2 : 1;
  return String(Number(number.toFixed(digits)));
}

/** "1.2M", "350k", "900". */
export function tokens(value) {
  const number = Number(value) || 0;
  if (number >= 1_000_000) return `${Number((number / 1_000_000).toFixed(1))}M`;
  if (number >= 1000) return `${Math.round(number / 1000)}k`;
  return String(number);
}

/** A stable color slot (0..7) for a task, so its steps, deadline and card match. */
export function colorSlot(id) {
  let hash = 0;
  for (const char of String(id)) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return hash % 8;
}

/** Why a step waits, with when that ends. */
export function waitText(wait, now) {
  if (!wait) return "";
  const until = wait.until && wait.until > now + 30 ? ` · until ${moment(wait.until, now)}` : "";
  const numbers = wait.need != null && wait.room != null ? ` (needs ${points(wait.need)}, ${points(wait.room)} free)` : "";
  return `${wait.text}${numbers}${until}`;
}

/** A moment for <input type="datetime-local">, in the browser's zone. */
export function toLocalInput(t) {
  if (!t) return "";
  const date = new Date(t * 1000);
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** Seconds since the epoch from <input type="datetime-local">; null when empty. */
export function fromLocalInput(value) {
  if (!value) return null;
  const parsed = new Date(value).getTime();
  return Number.isFinite(parsed) ? parsed / 1000 : null;
}

/** Deadlines one click away: tonight, tomorrow morning, the end of the week, in three days. */
export function quickDeadlines(now) {
  const base = new Date(now * 1000);
  const at = (days, hours) => {
    const date = new Date(base.getFullYear(), base.getMonth(), base.getDate() + days, hours, 0, 0);
    return date.getTime() / 1000;
  };
  const tonight = at(0, 23);
  const friday = (5 - base.getDay() + 7) % 7 || 7;
  const options = [
    { label: "Tomorrow 09:00", t: at(1, 9) },
    { label: "Friday 18:00", t: at(friday, 18) },
    { label: "In 3 days", t: at(3, 18) },
    { label: "In a week", t: at(7, 18) },
  ];
  if (tonight - now > 2 * 3600) options.unshift({ label: "Tonight 23:00", t: tonight });
  return options;
}

/** "Mon–Fri 10:00–19:00": a routine's hours in a line, for the settings summary. */
export function hoursSummary(hours) {
  const days = Object.entries(hours || {}).filter(([, ranges]) => ranges.length);
  if (!days.length) return "None: agents may work any time";
  const label = (ranges) => ranges.map(([a, b]) => `${pad(Math.floor(a / 60))}:${pad(a % 60)}–${pad(Math.floor(b / 60))}:${pad(b % 60)}`).join(", ");
  const groups = new Map();
  for (const [day, ranges] of days) {
    const key = label(ranges);
    groups.set(key, [...(groups.get(key) || []), Number(day)]);
  }
  const names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const span = (list) => {
    const sorted = [...list].sort((a, b) => a - b);
    const contiguous = sorted.every((day, index) => index === 0 || day === sorted[index - 1] + 1);
    return contiguous && sorted.length > 2 ? `${names[sorted[0]]}–${names[sorted.at(-1)]}` : sorted.map((day) => names[day]).join(", ");
  };
  return [...groups.entries()].map(([time, list]) => `${span(list)} ${time}`).join("; ");
}
