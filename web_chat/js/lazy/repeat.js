/**
 * How often a lazy task repeats.
 *
 * The service keeps the interval in seconds (``Task.repeat``) and takes it from
 * the form as ``repeat_hours``. A finished round sets up the next one with a
 * deadline that is the interval after the round's own deadline (or after the
 * moment it finished, when that is later) - an interval, not a calendar time:
 * a daily task does not run at a fixed hour of the day.
 */

/** The choices the form offers: [key, label, hint]. */
export const REPEAT_CHOICES = [
  ["once", "Once", "Runs a single time"],
  ["daily", "Every day", "The next round is due 24 hours after the previous one"],
  ["weekly", "Every week", "The next round is due 7 days after the previous one"],
  ["custom", "Own interval", "Hours between rounds"],
];

/** The interval of the presets, in hours. */
const PRESET_HOURS = { once: 0, daily: 24, weekly: 168 };

/** What the form should say: "an interval, not a time of day". */
export const REPEAT_HINT =
  "Counted from the previous round's deadline (or from when it ended, if later) - not at a set time of day.";

/**
 * The choice and custom hours for a stored interval in seconds: what an edit
 * form would show. A value that is not a preset comes back as "custom".
 */
export function repeatChoice(seconds) {
  if (!seconds) return { choice: "once", custom: "" };
  const hours = seconds / 3600;
  if (hours === PRESET_HOURS.daily) return { choice: "daily", custom: "" };
  if (hours === PRESET_HOURS.weekly) return { choice: "weekly", custom: "" };
  return { choice: "custom", custom: String(hours) };
}

/**
 * The hours to send for a choice, or the problem to show instead.
 * ``{hours, problem}``; hours is null when the custom value is not usable.
 */
export function repeatHours(choice, custom = "") {
  if (Object.hasOwn(PRESET_HOURS, choice)) return { hours: PRESET_HOURS[choice], problem: "" };
  const raw = String(custom ?? "").trim().replace(",", ".");
  if (!raw) return { hours: null, problem: "Give the interval in hours, for example 36." };
  const hours = Number(raw);
  if (!Number.isFinite(hours)) return { hours: null, problem: "The interval is a number of hours." };
  if (hours < 1) return { hours: null, problem: "The interval is at least one hour." };
  return { hours, problem: "" };
}
