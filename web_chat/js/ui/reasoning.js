/**
 * The reasoning panel: a live timeline of everything an answer was built from.
 *
 * Three levels of detail, each one click deeper, so the panel informs without
 * ever getting in the way of the answer:
 *
 *   1. **The headline** - "Thinking", "Reading config.yaml", then "Worked for
 *      12s · 7 steps" once the turn lands. Collapses itself when the answer
 *      arrives, unless the reader chose to keep it open.
 *   2. **The steps** - one row per thing the agent did, with its own duration
 *      and the context it touched shown as chips.
 *   3. **The payloads** - a step expands to its exact input and result; model
 *      reasoning is prose and always readable in place.
 *
 * Steps are addressed by id and updated in place: the server re-sends a step
 * when it completes, and reasoning arrives as deltas appended to a text node,
 * so a long turn never re-renders what the reader is already looking at.
 */

import { h, icon, replace } from "../lib/dom.js";
import { duration, plural } from "../lib/format.js";
import { renderMarkdown } from "../lib/markdown.js";
import { ICONS } from "./icons.js";
import { copyText } from "./toast.js";

/** Steps whose body is prose the reader should see without clicking. */
const INLINE_BODY_KINDS = new Set(["reasoning", "message"]);
const MAX_VISIBLE_REFS = 6;

const stepIcon = (kind) => ICONS[kind] ?? ICONS.prepare;

function refChip(ref) {
  const label = h("span", { text: ref.label });
  const glyph = icon(ICONS[ref.kind] ?? ICONS.file, { size: 12 });
  const title = ref.detail || ref.label;
  if (ref.kind === "url" && /^https?:\/\//i.test(ref.detail)) {
    return h("a.refChip", { href: ref.detail, target: "_blank", rel: "noreferrer noopener", title }, glyph, label);
  }
  return h("span.refChip", { title }, glyph, label);
}

function refsRow(refs) {
  if (!refs?.length) return null;
  const visible = refs.slice(0, MAX_VISIBLE_REFS).map(refChip);
  if (refs.length > MAX_VISIBLE_REFS) {
    const rest = refs.slice(MAX_VISIBLE_REFS);
    visible.push(h("span.refChip.refChip--more", { title: rest.map((ref) => ref.detail || ref.label).join("\n") }, `+${rest.length}`));
  }
  return h("div.step__refs", {}, visible);
}

/** A labelled, copyable payload block. */
function payload(label, text) {
  if (!text) return null;
  return h(
    "div.payload",
    {},
    h(
      "div.payload__bar",
      {},
      h("span.payload__label", { text: label }),
      h("button.iconBtn.iconBtn--xs", {
        type: "button",
        title: `Copy ${label.toLowerCase()}`,
        on: { click: () => copyText(text, `${label} copied`) },
      }, icon(ICONS.copy, { size: 13 })),
    ),
    h("pre.payload__body", {}, h("code", { text })),
  );
}

/**
 * One timeline row. Returns handles so the panel can patch it in place rather
 * than rebuilding the DOM every time the server re-sends the step.
 */
function createStepRow(step) {
  const inlineBody = INLINE_BODY_KINDS.has(step.kind);
  const glyph = h("span.step__icon", {}, icon(stepIcon(step.kind), { size: 14 }));
  const title = h("span.step__title", { text: step.title });
  const subtitle = h("span.step__subtitle");
  const policy = h("span.step__policy", { hidden: true });
  const timing = h("span.step__timing");
  const detail = h("div.step__detail");
  const refs = h("div.step__refsSlot");
  const body = inlineBody ? h("div.step__prose") : null;

  const head = h(
    "button.step__head",
    { type: "button", "aria-expanded": "false" },
    glyph,
    h("span.step__label", {}, title, subtitle),
    policy,
    timing,
    h("span.step__chevron", {}, icon(ICONS.chevron, { size: 14 })),
  );

  const root = h("li.step", { dataset: { kind: step.kind, status: step.status, tone: step.tone || "neutral" } }, head, refs, body, detail);

  let pinned = false; // the reader toggled this step; stop auto-managing it

  const setOpen = (open) => {
    root.classList.toggle("is-open", open);
    head.setAttribute("aria-expanded", String(open));
  };

  head.addEventListener("click", () => {
    pinned = true;
    setOpen(!root.classList.contains("is-open"));
  });

  /** Thinking stays open while it streams and folds away once it lands. */
  const settle = () => {
    if (body) body.classList.remove("is-streaming");
    if (inlineBody && !pinned) setOpen(false);
  };

  /** Live reasoning: append text without re-parsing what is already rendered. */
  const appendDelta = (delta) => {
    if (!body) return;
    body.classList.add("is-streaming");
    body.append(document.createTextNode(delta));
  };

  const update = (next) => {
    root.dataset.status = next.status;
    root.dataset.tone = next.tone || "neutral";
    title.textContent = next.title;
    // Reasoning has no summary until it lands; a running call keeps its arguments.
    subtitle.textContent = inlineBody && next.status === "running" ? "" : next.subtitle || "";
    policy.hidden = !next.policy;
    policy.textContent = next.policy?.label || "";
    policy.title = next.policy?.title || "";
    policy.dataset.decision = next.policy?.decision || "";
    timing.textContent = next.duration_ms == null ? "" : duration(next.duration_ms);
    replace(refs, refsRow(next.refs));
    if (body && next.status !== "running") {
      body.classList.remove("is-streaming");
      body.innerHTML = renderMarkdown(next.body);
    }
    const blocks = [payload("Input", next.detail), inlineBody ? null : payload("Result", next.body)].filter(Boolean);
    replace(detail, blocks);
    root.classList.toggle("has-detail", blocks.length > 0 || Boolean(inlineBody && (next.status === "running" || next.body)));
    if (inlineBody && !pinned) setOpen(next.status === "running");
  };

  update(step);
  return { root, update, appendDelta, settle };
}

/**
 * @returns the panel's element plus the handles the transcript drives it with.
 */
export function createReasoningPanel() {
  const rows = new Map();
  const list = h("ol.reasoning__steps");
  const headline = h("span.reasoning__headline", { text: "Thinking" });
  const timer = h("span.reasoning__timer");

  const toggle = h(
    "button.reasoning__toggle",
    { type: "button", "aria-expanded": "false" },
    h("span.reasoning__chevron", {}, icon(ICONS.chevron, { size: 14 })),
    headline,
    timer,
  );

  const root = h("section.reasoning", { hidden: true, dataset: { state: "idle" } }, toggle, list);

  let running = false;
  let startedAt = 0;
  let ticker = null;
  let pinnedOpen = false; // the reader opened or closed it; stop auto-managing
  let totalMs = null;

  const setExpanded = (expanded) => {
    root.classList.toggle("is-expanded", expanded);
    toggle.setAttribute("aria-expanded", String(expanded));
  };

  toggle.addEventListener("click", () => {
    pinnedOpen = true;
    setExpanded(!root.classList.contains("is-expanded"));
  });

  const renderHeadline = () => {
    if (running) {
      const latest = [...rows.values()].at(-1);
      headline.textContent = latest?.title || "Thinking";
      timer.textContent = duration(Date.now() - startedAt);
      return;
    }
    const count = rows.size;
    if (!count) return;
    headline.textContent = totalMs == null ? plural(count, "step") : `Worked for ${duration(totalMs)}`;
    timer.textContent = totalMs == null ? "" : `· ${plural(count, "step")}`;
  };

  return {
    el: root,

    get isEmpty() {
      return rows.size === 0;
    },

    /** A turn started (or, when reattaching, started `elapsedMs` ago): show it live and open. */
    start(elapsedMs = 0) {
      running = true;
      startedAt = Date.now() - elapsedMs;
      totalMs = null;
      pinnedOpen = false;
      root.hidden = false;
      root.dataset.state = "running";
      setExpanded(true);
      renderHeadline();
      ticker = setInterval(renderHeadline, 200);
    },

    /** Create or patch a step, addressed by `step.id`. */
    upsert(step) {
      root.hidden = false;
      const existing = rows.get(step.id);
      if (existing) {
        existing.title = step.title;
        existing.row.update(step);
      } else {
        const row = createStepRow(step);
        rows.set(step.id, { row, title: step.title });
        list.append(row.root);
      }
      renderHeadline();
    },

    /** Drop a step the server retracted (an empty reasoning block). */
    remove(id) {
      rows.get(id)?.row.root.remove();
      rows.delete(id);
      if (!rows.size) root.hidden = true;
      renderHeadline();
    },

    /** Stream a fragment into an open reasoning step. */
    appendReasoning(id, delta) {
      rows.get(id)?.row.appendDelta(delta);
    },

    /** The turn ended: settle the headline and step back out of the way.
     * Idempotent - both `done` and the socket closing settle the same turn. */
    finish(durationMs, autoCollapse = true) {
      if (!running && totalMs != null) return;
      running = false;
      clearInterval(ticker);
      ticker = null;
      totalMs = durationMs ?? Date.now() - startedAt;
      root.dataset.state = "idle";
      root.hidden = rows.size === 0;
      for (const { row } of rows.values()) row.settle();
      if (!pinnedOpen && autoCollapse) setExpanded(false);
      renderHeadline();
    },

    /** Render a stored trace from a reloaded conversation, collapsed. */
    hydrate(steps) {
      if (!steps?.length) return;
      for (const step of steps) this.upsert(step);
      const last = steps.at(-1);
      this.finish((last.at_ms ?? 0) + (last.duration_ms ?? 0));
      setExpanded(false);
    },
  };
}
