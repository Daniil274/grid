/** UTC date ranges and exports, shared by the dashboard and tests. */
export const number = (value) => new Intl.NumberFormat("en-US").format(value || 0);
export const compact = (value) => new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(value || 0);
export const modelKey = (row) => JSON.stringify([row.provider || "", row.model]);
export function dateRange(period, now = new Date(), earliest = null) {
  const end = now.toISOString().slice(0, 10);
  const start = new Date(`${end}T00:00:00Z`);
  start.setUTCDate(start.getUTCDate() - (period === "today" ? 0 : period === "all" ? 29 : Number(period) - 1));
  return { start: period === "all" && earliest ? earliest : start.toISOString().slice(0, 10), end };
}
export function csv(columns, rows) {
  const cell = (value) => {
    let text = String(value ?? "");
    // Spreadsheet programs interpret these prefixes as formulas, even when quoted.
    if (typeof value === "string" && /^[\s]*[=+\-@]/.test(text)) text = `'${text}`;
    return `"${text.replaceAll('"', '""')}"`;
  };
  return "\uFEFF" + [columns.map(cell).join(","), ...rows.map(row => columns.map(key => cell(row[key])).join(","))].join("\r\n");
}
export function chartBars(series, width = 1000, height = 230) {
  const max = Math.max(1, ...series.map(row => row.total_tokens));
  const step = width / Math.max(1, series.length);
  return { max, bars: series.map((row, index) => ({
    row, x: index * step, width: Math.max(.5, step * .74),
    incoming: height * row.tokens_in / max, outgoing: height * row.tokens_out / max,
  })) };
}
