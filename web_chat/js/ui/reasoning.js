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
 * A sub-agent's run is a step of kind `agent` that holds a timeline of its
 * own, and behaves like this panel in miniature: it shows what the sub-agent is
 * doing while it runs, folds away once it reports, and ends with that report.
 * Steps name their block with `parent_id`; the wire stays a flat list.
 *
 * Steps are addressed by id and updated in place: the server re-sends a step
 * when it completes, and reasoning arrives as deltas appended to a text node,
 * so a long turn never re-renders what the reader is already looking at.
 */

import { h, icon, replace } from "../lib/dom.js";
import { duration, plural, tokens } from "../lib/format.js";
import { renderMarkdown } from "../lib/markdown.js";
import { ICONS } from "./icons.js";
import { toolFamily } from "../lib/languages.js";
import { renderPayload } from "./tool-payload.js";
import { createPlanCard } from "./plan-card.js";

/** The tokens a step spent, prompt first, completion second; "" when none. */
const usageText = (tokensIn, tokensOut) => {
  const parts = [];
  if (tokensIn > 0) parts.push(`↑${tokens(tokensIn)}`);
  if (tokensOut > 0) parts.push(`↓${tokens(tokensOut)}`);
  return parts.join(" ");
};

/** Steps whose body is prose the reader should see without clicking. */
const INLINE_BODY_KINDS = new Set(["reasoning", "message"]);
const MAX_VISIBLE_REFS = 6;

const stepIcon = (kind) => ICONS[kind] ?? ICONS.prepare;

/** An icon per tool family: one glance says what kind of work the step runs,
 * before its label, payload or colour say the same thing again in text. */
const FAMILY_ICONS = {
  read: "file", write: "edit", edit: "edit", delete: "trash",
  search: "search", web: "url", agent: "users", task: "timeline", shell: "tool",
};
const familyIcon = (step) => ICONS[FAMILY_ICONS[toolFamily(step.tool)] ?? ""] ?? stepIcon(step.kind);

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

/** The parts every row shares: icon, title, subtitle, policy badge, timing. */
function stepHead(step) {
  const parts = {
    glyph: h("span.step__icon", {}, icon(familyIcon(step), { size: 14 })),
    title: h("span.step__title", { text: step.title }),
    subtitle: h("span.step__subtitle"),
    policy: h("span.step__policy", { hidden: true }),
    usage: h("span.step__usage"),
    timing: h("span.step__timing"),
    // A call held for the user's permission: Allow / Decline, outside the head button.
    approval: h("div.step__approval", { hidden: true }),
  };
  parts.head = h(
    "button.step__head",
    { type: "button", "aria-expanded": "false" },
    parts.glyph,
    h("span.step__label", {}, parts.title, parts.subtitle),
    parts.usage,
    parts.policy,
    parts.timing,
    h("span.step__chevron", {}, icon(ICONS.chevron, { size: 14 })),
  );
  return parts;
}

/** The tokens a step's model responses spent, when it spent any. */
function applyUsage(usage, next) {
  usage.textContent = usageText(next.tokens_in, next.tokens_out);
}

function applyPolicy(policy, next) {
  policy.hidden = !next.policy;
  policy.textContent = next.policy?.label || "";
  policy.title = next.policy?.title || "";
  policy.dataset.decision = next.policy?.decision || "";
}

/**
 * The answer a held call waits for (core/action_policy.py): shown while the
 * badge carries an approval id, gone once the server reports the outcome.
 */
function applyApproval(node, next) {
  const pending = next.policy?.approval_id;
  if (!pending) {
    if (!node.hidden) {
      node.hidden = true;
      replace(node);
      delete node.dataset.approvalId;
    }
    return;
  }
  if (node.dataset.approvalId === pending) return;
  node.dataset.approvalId = pending;
  node.hidden = false;
  const why = next.policy.title || "";
  if (next.policy.approvals === "operator") {
    replace(node, h("span.step__approvalNote", { text: "An operator decides on this call.", title: why }));
    return;
  }
  const answer = (approve, remember = false) => () => {
    for (const button of node.querySelectorAll("button")) button.disabled = true;
    document.dispatchEvent(new CustomEvent("policy-review", { detail: { approvalId: pending, approve, remember } }));
  };
  replace(
    node,
    h("span.step__approvalNote", { text: "The policy asks before this call.", title: why }),
    h("button.btn.btn--primary.btn--xs", { type: "button", on: { click: answer(true) } }, "Allow"),
    h("button.btn.btn--ghost.btn--xs", { type: "button", title: "Allow this tool for the rest of this turn", on: { click: answer(true, true) } }, "Allow for this turn"),
    h("button.btn.btn--ghost.btn--xs", { type: "button", on: { click: answer(false) } }, "Decline"),
  );
}

/**
 * Open/close state a reader can take over: once they click, auto-management stops.
 * `onOpen` runs whenever the row opens - rows build their hidden content then.
 */
function disclosure(root, head, onOpen = null) {
  let pinned = false;
  const setOpen = (open) => {
    root.classList.toggle("is-open", open);
    head.setAttribute("aria-expanded", String(open));
    if (open) onOpen?.();
  };
  head.addEventListener("click", () => {
    pinned = true;
    setOpen(!root.classList.contains("is-open"));
  });
  /** Open or close on the panel's behalf, unless the reader has decided. */
  const suggest = (open) => {
    if (!pinned) setOpen(open);
  };
  return { setOpen, suggest };
}

/**
 * One timeline row. Returns handles so the panel can patch it in place rather
 * than rebuilding the DOM every time the server re-sends the step.
 *
 * What a row hides until it is opened - rendered thinking, tool input and
 * result - is built when it opens: a stored chat holds hundreds of steps,
 * some with tens of thousands of characters, and nearly all stay closed.
 */
function createStepRow(step) {
  const inlineBody = INLINE_BODY_KINDS.has(step.kind);
  const { head, title, subtitle, policy, usage, timing, glyph, approval } = stepHead(step);
  const detail = h("div.step__detail");
  const refs = h("div.step__refsSlot");
  const body = inlineBody ? h("div.step__prose") : null;
  let latest = step;
  let stale = true;
  const payloadState = { input: {}, result: {} };

  /** Build the hidden part from the latest step; once per change, only when open. */
  const fill = () => {
    if (!stale) return;
    stale = false;
    // Streaming thinking is appended as it arrives; it is rendered once it lands.
    if (body && latest.status !== "running") {
      body.classList.remove("is-streaming");
      body.innerHTML = renderMarkdown(latest.body);
    }
    replace(detail, [
      renderPayload("Input", latest.input_payload, latest.detail, { state: payloadState.input, tool: latest.tool }),
      inlineBody ? null : renderPayload(latest.status === "error" ? "Error" : "Result", latest.result_payload, latest.body, { state: payloadState.result, tool: latest.tool, markdown: latest.kind === "agent" }),
    ].filter(Boolean));
  };

  const root = h("li.step", { dataset: {
    kind: step.kind, status: step.status, tone: step.tone || "neutral",
    family: toolFamily(step.tool),
  } }, head, approval, refs, body, detail);
  const { suggest } = disclosure(root, head, fill);

  /** Thinking stays open while it streams and folds away once it lands. */
  const settle = () => {
    if (body) body.classList.remove("is-streaming");
    if (inlineBody) suggest(false);
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
    root.dataset.family = toolFamily(next.tool);
    // The icon follows the family: a call that reveals its tool retrofits
    // the glyph in place rather than rebuilding the head.
    const path = familyIcon(next);
    if (glyph.dataset.path !== path) { glyph.dataset.path = path; replace(glyph, icon(path, { size: 14 })); }
    title.textContent = next.title;
    // Reasoning has no summary until it lands; a running call keeps its arguments.
    subtitle.textContent = inlineBody && next.status === "running" ? "" : next.subtitle || "";
    applyPolicy(policy, next);
    applyApproval(approval, next);
    applyUsage(usage, next);
    timing.textContent = next.duration_ms == null ? "" : duration(next.duration_ms);
    replace(refs, refsRow(next.refs));
    latest = next;
    stale = true;
    if (root.classList.contains("is-open")) fill();
    const hasBlocks = Boolean(next.detail || next.input_payload) || Boolean(!inlineBody && (next.body || next.result_payload));
    root.classList.toggle("has-detail", hasBlocks || Boolean(inlineBody && (next.status === "running" || next.body)));
    if (inlineBody) suggest(next.status === "running");
  };

  update(step);
  return { root, kind: step.kind, update, appendDelta, settle, activity: () => title.textContent, tick() {} };
}

/**
 * The rows directly inside one list - the panel's, or a sub-agent block's -
 * and what they add up to: the latest activity for a live headline, the step
 * count for a settled one.
 */
function createTimeline(list) {
  const rows = new Map();
  return {
    list,
    rows,
    get size() {
      return rows.size;
    },
    /** What is happening right now, reaching into a running sub-agent. */
    activity() {
      return [...rows.values()].at(-1)?.activity() || "";
    },
    tick(now) {
      for (const row of rows.values()) row.tick(now);
    },
  };
}

/** A sub-agent is asked in words; show them, not the JSON envelope they came in. */
function taskText(detail) {
  try {
    const args = JSON.parse(detail);
    const values = Object.values(args ?? {});
    if (values.length === 1 && typeof values[0] === "string") return values[0];
    // Agent tools take `input`; orchestrate takes `task` beside its settings.
    for (const key of ["input", "task"]) if (typeof args?.[key] === "string") return args[key];
  } catch {
    // Not JSON: already plain text.
  }
  return detail;
}

/**
 * A sub-agent's run: a row whose detail is the sub-agent's own timeline, framed
 * by the task it was given and the report it returned.
 *
 * @param {() => number} turnStart - when the turn began, for a live duration.
 */
function createAgentRow(step, turnStart) {
  const { head, title, subtitle, policy, usage, timing, approval } = stepHead(step);
  const timeline = createTimeline(h("ol.agent__steps"));
  const task = h("div.agent__task");
  const report = h("div.agent__report");
  const refs = h("div.step__refsSlot");
  const root = h(
    "li.step.has-detail",
    { dataset: { kind: "agent", status: step.status, tone: step.tone || "neutral" } },
    head,
    approval,
    refs,
    h("div.step__agent", {}, task, timeline.list, report),
  );
  let current = step;
  let stale = true;
  const payloadState = { input: {}, result: {} };
  /** The task and the report, built from the latest step when the block is open. */
  const fill = () => {
    if (!stale) return;
    stale = false;
    const asked = taskText(current.detail);
    replace(task, renderPayload("Task", current.input_payload, asked, { markdown: asked !== current.detail, state: payloadState.input }));
    replace(report, renderPayload(current.status === "error" ? "Error" : "Report", current.result_payload, current.body, { markdown: current.status !== "error", state: payloadState.result }));
  };
  const { suggest } = disclosure(root, head, fill);

  const renderSubtitle = () => {
    const asked = taskText(current.detail || "").replace(/\s+/g, " ").trim() || current.subtitle;
    if (current.status === "running") {
      subtitle.textContent = timeline.activity() || asked || "Starting";
      return;
    }
    const count = timeline.size ? plural(timeline.size, "step") : "";
    subtitle.textContent = [count, asked].filter(Boolean).join(" · ");
  };

  const update = (next) => {
    const wasRunning = current.status === "running";
    current = next;
    root.dataset.status = next.status;
    root.dataset.tone = next.tone || "neutral";
    title.textContent = next.title;
    applyPolicy(policy, next);
    applyApproval(approval, next);
    applyUsage(usage, next);
    if (next.duration_ms != null) timing.textContent = duration(next.duration_ms);
    replace(refs, refsRow(next.refs));
    stale = true;
    if (root.classList.contains("is-open")) fill();
    renderSubtitle();
    // Like the panel: open while the sub-agent works, out of the way once it reports.
    if (next.status === "running") suggest(true);
    else if (wasRunning) suggest(false);
  };

  update(step);
  return {
    root,
    kind: "agent",
    timeline,
    update,
    appendDelta() {},
    settle: () => suggest(false),
    activity: () => {
      const inner = current.status === "running" ? timeline.activity() : "";
      return inner ? `${title.textContent} › ${inner}` : title.textContent;
    },
    /** Called on the panel's ticker: a running block keeps its clock moving. */
    tick(now) {
      if (current.status !== "running") return;
      timing.textContent = duration(Math.max(now - turnStart() - (current.at_ms ?? 0), 0));
      renderSubtitle();
      timeline.tick(now);
    },
    /** A child changed: refresh what the collapsed head says about it. */
    refresh: renderSubtitle,
  };
}

/**
 * @returns the panel's element plus the handles the transcript drives it with.
 */
export function createReasoningPanel() {
  const top = createTimeline(h("ol.reasoning__steps"));
  // The plan is a step of the trace, but the reader follows it outside the
  // panel, which folds away once the turn ends (ui/plan-card.js).
  const plan = createPlanCard();
  // Every row at any depth, by step id, with the timeline that holds it.
  const index = new Map();
  const headline = h("span.reasoning__headline", { text: "Thinking" });
  const timer = h("span.reasoning__timer");

  const toggle = h(
    "button.reasoning__toggle",
    { type: "button", "aria-expanded": "false" },
    h("span.reasoning__chevron", {}, icon(ICONS.chevron, { size: 14 })),
    headline,
    timer,
  );

  const root = h("section.reasoning", { hidden: true, dataset: { state: "idle" } }, toggle, top.list);

  let running = false;
  let startedAt = 0;
  let ticker = null;
  let pinnedOpen = false; // the reader opened or closed it; stop auto-managing
  let totalMs = null;
  let totalUsage = null; // the turn's tokens (input, output), when it reported them

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
      const now = Date.now();
      headline.textContent = top.activity() || "Thinking";
      timer.textContent = duration(now - startedAt);
      top.tick(now);
      return;
    }
    // Sub-agent steps live inside their blocks; the headline counts what the
    // agent itself did, so a delegation reads as one step, not twenty.
    const count = top.size;
    if (!count) return;
    headline.textContent = totalMs == null ? plural(count, "step") : `Worked for ${duration(totalMs)}`;
    const trailing = [];
    if (totalMs != null) trailing.push(`· ${plural(count, "step")}`);
    if (totalUsage) trailing.push(`· ${usageText(totalUsage.tokensIn, totalUsage.tokensOut)}`);
    timer.textContent = trailing.join(" ");
  };

  /** The block a step belongs in: its sub-agent's, or the top when unknown. */
  const timelineFor = (step) => index.get(step.parent_id)?.row.timeline ?? top;

  /** Every sub-agent block above a row, innermost first. */
  const ancestors = function* (id) {
    for (let entry = index.get(index.get(id)?.parentId); entry; entry = index.get(entry.parentId)) yield entry.row;
  };

  const makeRow = (step) => (step.kind === "agent" ? createAgentRow(step, () => startedAt) : createStepRow(step));

  return {
    el: root,
    planEl: plan.el,

    get isEmpty() {
      return index.size === 0;
    },

    /** A turn started (or, when reattaching, started `elapsedMs` ago): show it live and open. */
    start(elapsedMs = 0) {
      running = true;
      startedAt = Date.now() - elapsedMs;
      totalMs = null;
      totalUsage = null;
      pinnedOpen = false;
      root.hidden = false;
      root.dataset.state = "running";
      setExpanded(true);
      renderHeadline();
      ticker = setInterval(renderHeadline, 200);
    },

    /** Create or patch a step, addressed by `step.id`, inside its sub-agent's block. */
    upsert(step) {
      if (step.kind === "plan") {
        plan.update(step);
        return;
      }
      root.hidden = false;
      const existing = index.get(step.id);
      if (existing && (existing.row.kind === "agent") === (step.kind === "agent")) {
        existing.row.update(step);
      } else {
        // A call becomes an agent block once its sub-agent starts: rebuild the
        // row in place, keeping its position.
        const row = makeRow(step);
        const timeline = existing?.timeline ?? timelineFor(step);
        if (existing) existing.row.root.replaceWith(row.root);
        else timeline.list.append(row.root);
        timeline.rows.set(step.id, row);
        index.set(step.id, { row, timeline, parentId: existing?.parentId ?? step.parent_id });
      }
      for (const block of ancestors(step.id)) block.refresh();
      renderHeadline();
    },

    /** Drop a step the server retracted (an empty reasoning block). */
    remove(id) {
      const entry = index.get(id);
      if (!entry) return;
      entry.row.root.remove();
      entry.timeline.rows.delete(id);
      const parents = [...ancestors(id)];
      index.delete(id);
      for (const block of parents) block.refresh();
      if (!index.size) root.hidden = true;
      renderHeadline();
    },

    /** Stream a fragment into an open reasoning step, at any depth. */
    appendReasoning(id, delta) {
      index.get(id)?.row.appendDelta(delta);
    },

    /** The turn ended: settle the headline and step back out of the way.
     * Idempotent - both `done` and the socket closing settle the same turn. */
    finish(durationMs, autoCollapse = true, usage = null) {
      if (!running && totalMs != null) return;
      running = false;
      clearInterval(ticker);
      ticker = null;
      totalMs = durationMs ?? Date.now() - startedAt;
      // The server's own count of the turn's tokens, when it said them.
      if (usage && (usage.tokensIn || usage.tokensOut)) totalUsage = usage;
      root.dataset.state = "idle";
      root.hidden = index.size === 0;
      for (const { row } of index.values()) row.settle();
      if (!pinnedOpen && autoCollapse) setExpanded(false);
      renderHeadline();
    },

    /** Render a stored trace from a reloaded conversation, collapsed. */
    hydrate(steps) {
      for (const step of steps ?? []) if (step.kind === "plan") plan.update(step);
      steps = steps?.filter((step) => step.kind !== "plan");
      if (!steps?.length) return;
      for (const step of steps) this.upsert(step);
      // The last step listed may be a short one inside a block that ran on.
      // A block's children are counted inside it, not beside it.
      const ids = new Set(steps.map((step) => step.id));
      const outermost = steps.filter((step) => !step.parent_id || !ids.has(step.parent_id));
      const spent = (key) => outermost.reduce((total, step) => total + (Number(step[key]) || 0), 0);
      this.finish(Math.max(...steps.map((step) => (step.at_ms ?? 0) + (step.duration_ms ?? 0))), true, {
        tokensIn: spent("tokens_in"),
        tokensOut: spent("tokens_out"),
      });
      setExpanded(false);
    },
  };
}
