import { h } from "../lib/dom.js";
import { chartBars, compact, number } from "./data.js";

const svgNode = (tag, attrs, text) => {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text !== undefined) node.textContent = text;
  return node;
};
export function timeline(container, series, bucket, detail) {
  container.replaceChildren();
  detail.textContent = "Hover or focus a bar for exact totals.";
  if (!series.some(row => row.total_tokens)) {
    container.append(h("p", { class: "usageEmpty" }, "No token activity in this chart for the selected range."));
    return;
  }
  const width = Math.max(280, container.clientWidth);
  const svg = svgNode("svg", { viewBox: `0 0 ${width} 280`, role: "img", "aria-label": "Input and output tokens over time" });
  const { max, bars } = chartBars(series, width - 80);
  for (let i = 0; i <= 4; i++) {
    const y = 240 - i * 57.5;
    svg.append(svgNode("line", { x1: 60, x2: width - 20, y1: y, y2: y, class: "grid-line" }));
    svg.append(svgNode("text", { x: 48, y: y + 4, "text-anchor": "end" }, compact(max * i / 4)));
  }
  const label = (row) => new Intl.DateTimeFormat("en-US", { timeZone: "UTC", month: "short", day: "numeric", ...(bucket === "hour" ? { hour: "2-digit", hourCycle: "h23" } : {}) }).format(new Date(row.at));
  bars.forEach((bar, index) => {
    const { row, incoming, outgoing } = bar;
    const description = `${label(row)} UTC · Input ${number(row.tokens_in)} · Output ${number(row.tokens_out)} · Total ${number(row.total_tokens)} · ${number(row.responses)} responses`;
    const group = svgNode("g", { class: "chart-bar", tabindex: 0, role: "graphics-symbol", "aria-label": description });
    group.append(svgNode("title", {}, description));
    group.append(svgNode("rect", { x: 60 + bar.x, y: 240 - incoming, width: bar.width, height: incoming, class: "bar-in" }));
    group.append(svgNode("rect", { x: 60 + bar.x, y: 240 - incoming - outgoing, width: bar.width, height: outgoing, class: "bar-out" }));
    // Include empty buckets in pointer and keyboard inspection.
    group.append(svgNode("rect", { x: 60 + bar.x, y: 10, width: bar.width, height: 230, fill: "transparent" }));
    group.addEventListener("mouseenter", () => { detail.textContent = description; });
    group.addEventListener("focus", () => { detail.textContent = description; });
    svg.append(group);
    const stride = Math.max(1, Math.ceil(bars.length / Math.max(2, Math.floor(width / (bucket === "hour" ? 125 : 90)))));
    const tick = width < 600 && bucket === "hour" ? `${new Date(row.at).getUTCHours()}:00` : label(row);
    if (index % stride === 0) svg.append(svgNode("text", { x: 60 + bar.x, y: 267 }, tick));
  });
  container.append(svg);
}
export function distribution(container, models, select) {
  container.replaceChildren();
  if (!models.length) {
    container.append(h("p", { class: "usageEmpty" }, "No models in this range."));
    return;
  }
  const total = models.reduce((sum, row) => sum + row.total_tokens, 0);
  for (const row of models) {
    const share = total ? row.total_tokens / total * 100 : 0;
    const button = h("button", { class: "usageModelBar", type: "button", title: `Filter: ${row.model}` },
      h("span", { class: "bar-label" }, h("span", { class: "bar-name" }, row.model, h("small", {}, row.provider || "Provider not recorded")), h("span", { class: "bar-number" }, `${compact(row.total_tokens)} · ${share.toFixed(1)}%`)),
      h("span", { class: "bar-track" }, h("span", { class: "bar-fill", style: { width: `${share}%` } })),
    );
    button.addEventListener("click", () => select(row));
    container.append(button);
  }
}
