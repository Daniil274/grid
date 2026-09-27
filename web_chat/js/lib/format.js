/** Presentation-layer formatting. Locale-aware, never throws on bad input. */

const LOCALE = undefined; // follow the browser

/** `340ms`, `4.2s`, `1m 12s` - the unit people actually read at that scale. */
export function duration(ms) {
  if (ms == null || Number.isNaN(ms)) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)}s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return seconds ? `${minutes}m ${seconds}s` : `${minutes}m`;
}

function toDate(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function clock(value) {
  const date = toDate(value);
  return date ? date.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" }) : "";
}

export function nowClock() {
  return clock(Date.now());
}

/** Time for today, date otherwise - the sidebar's compact timestamp. */
export function shortStamp(value) {
  const date = toDate(value);
  if (!date) return "";
  const isToday = date.toDateString() === new Date().toDateString();
  return isToday ? clock(date) : date.toLocaleDateString(LOCALE, { day: "2-digit", month: "short" });
}

/** Bucket a timestamp for sidebar grouping. */
export function dayBucket(value) {
  const date = toDate(value);
  if (!date) return "Earlier";
  const days = Math.floor((startOfDay(new Date()) - startOfDay(date)) / 86_400_000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return "This week";
  if (days < 30) return "This month";
  return "Earlier";
}

function startOfDay(date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

export function plural(count, singular, pluralForm = `${singular}s`) {
  return `${count} ${count === 1 ? singular : pluralForm}`;
}
