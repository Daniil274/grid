import { api } from "../net/api.js";
import { h } from "../lib/dom.js";
import { createThemeToggle } from "../ui/theme.js";
import { number, dateRange, modelKey, csv } from "./data.js";
import { timeline, distribution } from "./charts.js";

const $ = (id) => document.getElementById(id);
createThemeToggle($("theme"));
let report = null;
let controller = null;
let earliest = null;
function setDates() {
  if ($("period").value === "custom") return;
  const range = dateRange($("period").value, new Date(), earliest);
  $("start").value = range.start;
  $("end").value = range.end;
}
setDates();
const selectModel = (row) => { $("model").value = modelKey(row); void load(); };
function table() {
  const body = $("model-rows");
  body.replaceChildren();
  const fields = ["total_tokens", "tokens_in", "tokens_out", "cached_in", "reasoning_out", "responses"];
  for (const row of [...report.models].sort((a, b) => b[$("sort").value] - a[$("sort").value])) {
    const button = h("button", { type: "button" }, row.model);
    button.addEventListener("click", () => selectModel(row));
    body.append(h("tr", {}, h("td", {}, button, h("small", {}, row.provider || "Provider not recorded")), ...fields.map(key => h("td", {}, number(row[key])))));
  }
  if (!report.models.length) body.append(h("tr", {}, h("td", { colspan: 7, class: "usageEmpty" }, "No responses for these filters.")));
}
const dollars = (amount = 0) => `$${amount >= 1 ? amount.toFixed(2) : amount.toFixed(4)}`;
function render() {
  const summary = report.summary;
  $("metrics").replaceChildren(...[
    ["Total tokens", summary.total_tokens, "Input + output"],
    ["Input tokens", summary.tokens_in, "Includes cached input"],
    ["Output tokens", summary.tokens_out, "Includes reasoning output"],
    ["Cached input", summary.cached_in, "As reported by providers"],
    ["Reasoning output", summary.reasoning_out, "As reported by providers"],
    ["Model responses", summary.responses, "Detailed records only"],
    ["Cost to the server", dollars(summary.charged_usd), "Calls on the server's keys"],
    ["Value of all calls", dollars(summary.cost_usd), "Own keys and plans included"],
  ].map(([label, value, hint]) => h("div", { class: "usageMetric" }, h("span", {}, label), h("strong", {}, typeof value === "string" ? value : number(value)), h("small", {}, hint))));
  $("scope-label").hidden = !report.can_view_all;
  const selected = $("model").value;
  $("model").replaceChildren(h("option", { value: "" }, "All models"), ...report.model_options.map(row => h("option", { value: row.key }, `${row.model}${row.provider ? ` · ${row.provider}` : ""}`)));
  // API keys may contain whitespace; normalize to the same compact JSON used by the UI.
  for (const option of $("model").options) if (option.value) option.value = JSON.stringify(JSON.parse(option.value));
  $("model").value = selected;
  const grouping = { hour: "Hourly", day: "Daily", week: "7-day buckets from range start" };
  $("range-caption").textContent = `${report.start} — ${report.end} · ${grouping[report.bucket]} · UTC`;
  const avg = summary.responses ? Math.round((summary.total_tokens - report.coverage.legacy_tokens) / summary.responses) : 0;
  $("response-caption").textContent = `${number(summary.responses)} responses · ${number(avg)} tokens per response`;
  timeline($("timeline"), report.series, report.bucket, $("timeline-detail"));
  distribution($("models-chart"), report.models, selectModel);
  table();
  const since = new Date(report.coverage.detailed_since).toISOString().slice(0, 10);
  $("coverage").textContent = `Detailed model accounting started ${since} UTC.` + (report.coverage.legacy_tokens ? ` ${number(report.coverage.legacy_tokens)} historical tokens have daily totals only and appear as Unknown model; their response counts are unavailable.` : "") + (report.coverage.legacy_omitted_from_chart ? " Historical daily totals are omitted from the hourly chart and included in the totals above." : "");
  $("export-models").disabled = false;
  $("export-time").disabled = false;
}
async function load() {
  if (!$("filters").reportValidity()) return;
  if ($("start").value > $("end").value) {
    $("status").textContent = "The end date must be on or after the start date.";
    $("status").dataset.error = "true";
    return;
  }
  controller?.abort();
  const current = new AbortController();
  controller = current;
  $("status").textContent = "Updating…";
  $("status").dataset.error = "false";
  $("refresh").disabled = true;
  try {
    const query = Object.fromEntries(["start", "end", "model", "bucket", "scope"].map(id => [id, $(id).value]).filter(([, value]) => value));
    const next = await api.usageReport(query, { signal: current.signal });
    if (current !== controller) return;
    const previousEarliest = earliest;
    earliest = next.coverage.earliest;
    if ($("period").value === "all" && earliest && earliest !== previousEarliest && $("start").value !== earliest) {
      setDates();
      void load();
      return;
    }
    report = next;
    render();
    $("status").textContent = `Updated ${new Date().toLocaleTimeString()} · ${next.scope === "all" ? "Whole server" : "My usage"}`;
  } catch (error) {
    if (error.name === "AbortError" || current !== controller) return;
    $("status").textContent = `${error.message}${report ? " · Showing the last successful report." : ""}`;
    $("status").dataset.error = "true";
  } finally {
    if (current === controller) $("refresh").disabled = false;
  }
}
$("filters").addEventListener("submit", event => { event.preventDefault(); void load(); });
$("period").addEventListener("change", () => { setDates(); if ($("period").value !== "custom") void load(); });
for (const id of ["start", "end"]) $(id).addEventListener("change", () => { $("period").value = "custom"; });
for (const id of ["model", "bucket", "scope"]) $(id).addEventListener("change", () => {
  if (id === "scope") { earliest = null; $("model").value = ""; }
  void load();
});
$("refresh").addEventListener("click", () => { setDates(); void load(); });
$("clear-model").addEventListener("click", () => { $("model").value = ""; void load(); });
$("sort").addEventListener("change", () => { if (report) table(); });
function download(kind) {
  if (!report) return;
  const fields = ["tokens_in", "tokens_out", "total_tokens", "cached_in", "reasoning_out", "responses"];
  const columns = kind === "models" ? ["model", "provider", ...fields] : ["at", ...fields];
  const url = URL.createObjectURL(new Blob([csv(columns, kind === "models" ? report.models : report.series)], { type: "text/csv;charset=utf-8" }));
  const link = h("a", { href: url, download: `grid-usage-${kind}-${report.scope}-${report.start}-${report.end}.csv` });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
$("export-models").addEventListener("click", () => download("models"));
$("export-time").addEventListener("click", () => download("time"));
let resizeFrame;
window.addEventListener("resize", () => {
  cancelAnimationFrame(resizeFrame);
  resizeFrame = requestAnimationFrame(() => { if (report) timeline($("timeline"), report.series, report.bucket, $("timeline-detail")); });
});
setInterval(() => { if ($("auto-refresh").checked && !document.hidden && !$("refresh").disabled) { setDates(); void load(); } }, 30000);
void load();
