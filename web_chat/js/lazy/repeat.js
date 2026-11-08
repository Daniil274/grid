/**
 * How often a lazy task repeats, and how that is said in words.
 *
 * A task repeats in one of two ways, and they are not the same thing:
 *
 * - an **interval** - a period between rounds. The service keeps it in seconds
 *   (``Task.repeat``) and takes it from the form as ``repeat_hours``. The next
 *   round is *released* a whole period after this round's own release, on a
 *   fixed grid, so two rounds never run back to back and a late round skips
 *   the slots it missed;
 * - a **calendar rule** - a wall time on a local day (``Task.repeat_calendar``,
 *   sent as ``repeat``): ``{"kind": "daily", "time": "04:00", "timezone":
 *   "Europe/Moscow"}`` or ``{"kind": "weekly", ..., "weekday": "mon"}``. The
 *   zone is named, so 04:00 means 04:00 where the user lives.
 *
 * A slot missed while nothing could run is skipped once, never replayed as a
 * queue (``missed: "skip"`` in the rule the service stores).
 */

/** The choices the form offers: [key, label, hint]. */
export const REPEAT_CHOICES = [
  ["once", "Once", "Runs a single time"],
  ["interval", "Own interval", "Every N hours, counted from the previous round's release"],
  ["daily", "Every day", "At a fixed time of day, in your own zone"],
  ["weekly", "Every week", "On one day of the week, at a fixed time of day"],
];

/** The days of the week a weekly rule names: [key, label]. */
export const WEEKDAY_OPTIONS = [
  ["mon", "Mon"], ["tue", "Tue"], ["wed", "Wed"], ["thu", "Thu"], ["fri", "Fri"], ["sat", "Sat"], ["sun", "Sun"],
];

/** What the form says under the picker: the two ways are different. */
export const REPEAT_HINT =
  "An interval counts from the previous round's release - rounds never run back to back. "
  + "A daily or weekly rule is a time of day in your own zone.";

/** Shown beside a calendar rule: missing a slot does not queue it. */
export const MISSED_NOTE = "A slot missed while nothing could run is skipped, never replayed.";

/** A zone the browser knows, or the problem to show. */
export function zoneProblem(timezone) {
  const name = String(timezone ?? "").trim();
  if (!name) return "Name your time zone, for example Europe/Moscow.";
  try {
    new Intl.DateTimeFormat("en", { timeZone: name });
  } catch {
    return `Unknown time zone: ${name}.`;
  }
  return "";
}

/** A wall time as HH:MM, or the problem to show. */
export function timeProblem(time) {
  const text = String(time ?? "").trim();
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(text)) return "Give a time of day as HH:MM, for example 04:00.";
  return "";
}

/**
 * The payload fields for a repeat choice: ``{repeat_hours, repeat, problem}``.
 *
 * Exactly one of the two is sent: an interval as ``repeat_hours`` (hours, 0 for
 * a single run), a calendar rule as a ``repeat`` object with its zone and the
 * missed policy. A problem is shown instead of sending something the service
 * would refuse.
 */
export function repeatPayload({ choice, hours = "", time = "04:00", weekday = "mon", timezone = "UTC" } = {}) {
  const once = { repeat_hours: 0, repeat: null, problem: "" };
  if (!choice || choice === "once") return once;
  if (choice === "interval") {
    const raw = String(hours ?? "").trim().replace(",", ".");
    if (!raw) return { ...once, problem: "Give the interval in hours, for example 36." };
    const value = Number(raw);
    if (!Number.isFinite(value)) return { ...once, problem: "The interval is a number of hours." };
    if (value < 1) return { ...once, problem: "The interval is at least one hour." };
    return { repeat_hours: value, repeat: null, problem: "" };
  }
  if (choice !== "daily" && choice !== "weekly") return { ...once, problem: `Unknown repeat: ${choice}.` };
  const clock = timeProblem(time);
  if (clock) return { ...once, problem: clock };
  const zone = zoneProblem(timezone);
  if (zone) return { ...once, problem: zone };
  const rule = { kind: choice, time: String(time).trim(), timezone: String(timezone).trim(), missed: "skip" };
  if (choice === "weekly") {
    if (!WEEKDAY_OPTIONS.some(([key]) => key === weekday)) return { ...once, problem: `Unknown weekday: ${weekday}.` };
    rule.weekday = weekday;
  }
  return { repeat_hours: 0, repeat: rule, problem: "" };
}

/** "Runs once", "Every 6 h", "Daily at 04:00 (Europe/Moscow)", "Weekly on Mon at 04:00 (…)". */
export function describeSchedule(schedule) {
  if (!schedule) return "Runs once";
  if (schedule.kind === "interval") {
    const hours = Number(schedule.seconds ? schedule.seconds / 3600 : schedule.hours ?? 0);
    if (!Number.isFinite(hours) || hours <= 0) return "Runs once";
    if (hours >= 24 && hours % 24 === 0) return `Every ${hours / 24 === 1 ? "day" : `${hours / 24} days`}`;
    return `Every ${hours} h`;
  }
  const clock = schedule.time ?? "00:00";
  if (schedule.kind === "daily") return `Daily at ${clock} (${schedule.timezone})`;
  const day = WEEKDAY_OPTIONS.find(([key]) => key === schedule.weekday)?.[1] ?? schedule.weekday ?? "mon";
  return `Weekly on ${day} at ${clock} (${schedule.timezone})`;
}
