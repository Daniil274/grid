import test from "node:test";
import assert from "node:assert/strict";
import { dateRange, csv, chartBars } from "../web_chat/js/usage/data.js";

test("presets use inclusive UTC dates across month and year boundaries", () => {
  assert.deepEqual(dateRange("7", new Date("2026-01-02T00:30:00Z")), { start: "2025-12-27", end: "2026-01-02" });
  assert.deepEqual(dateRange("today", new Date("2026-10-03T23:30:00-04:00")), { start: "2026-10-04", end: "2026-10-04" });
  assert.deepEqual(dateRange("all", new Date("2026-10-03Z"), "2026-01-01"), { start: "2026-01-01", end: "2026-10-03" });
});
test("CSV preserves quotes and line breaks, and neutralizes spreadsheet formulas", () => {
  const result = csv(["model", "tokens"], [{ model: 'a,"b"\nc', tokens: 12 }, { model: " =SUM(A1)", tokens: 0 }]);
  assert.equal(result, '\uFEFF"model","tokens"\r\n"a,""b""\nc","12"\r\n"\' =SUM(A1)","0"');
});
test("stacked chart geometry handles zero and large token totals without overflow", () => {
  assert.deepEqual(chartBars([]).bars, []);
  const result = chartBars([{ tokens_in: 0, tokens_out: 0, total_tokens: 0 }, { tokens_in: 9e9, tokens_out: 1e9, total_tokens: 1e10 }]);
  assert.equal(result.bars[0].incoming, 0);
  assert.equal(result.bars[1].incoming + result.bars[1].outgoing, 230);
  assert.equal(result.bars[1].x, 500);
});
