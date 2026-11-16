/**
 * The public conversation of a turn and the work between its messages.
 *
 * A top-level agent message closes the execution segment before it. Reasoning,
 * calls and sub-agent blocks after that message form the next segment. Every
 * segment is the same full reasoning panel: one implementation, one set of
 * icons, animations, lazy payloads and disclosure behaviour.
 *
 * Live frames and stored history take the same path. Stored traces carry the
 * full steps; typed public events restore their wording and authors, or act as
 * a fallback for old messages without a trace. Step ids route late updates to
 * the segment that already owns the step, never to the newest one.
 */

import { h } from "../lib/dom.js";
import { renderMarkdown } from "../lib/markdown.js";
import { createReasoningPanel } from "./reasoning.js";

/** The kinds a person may read. A report is internal by contract. */
export const COMMUNICATION_KINDS = new Set(["message", "status"]);

/** Steps whose title is a status a person may read (`PUBLIC_STATUS_STEP_KINDS`). */
export const STATUS_STEP_KINDS = new Set(["prepare", "routing", "tool", "compact", "plan", "handoff", "mcp"]);

/** Authors that are roles, not names: the message itself already says them. */
const ROLE_AUTHORS = new Set(["assistant", "system", "grid", "user"]);

/** Whether an event is a public communication this view may draw. */
export function isPublicCommunication(event) {
  if (!event || typeof event !== "object") return false;
  const kind = String(event.kind ?? "");
  if (!COMMUNICATION_KINDS.has(kind)) return false;
  return String(event.visibility ?? "public") === "public";
}

/** The identity a live event and its stored replay share. */
export function communicationKey(event) {
  const kind = String(event?.kind ?? "");
  const stepId = event?.step_id;
  if (stepId) return `${kind}:step:${stepId}`;
  if (event?.id) return `${kind}:id:${event.id}`;
  return `${kind}:text:${String(event?.content ?? "")}`;
}

/** "Read config.yaml · core/app.py": the observer's own status wording. */
function statusText(step) {
  const title = String(step?.title ?? "");
  const subtitle = String(step?.subtitle ?? "");
  return subtitle && !title.includes(subtitle) ? `${title} · ${subtitle}` : title;
}

/** The public communication a top-level step represents, or null when silent. */
export function eventFromStep(step) {
  if (!step || typeof step !== "object") return null;
  if (step.parent_id) return null;
  const kind = String(step.kind ?? "");
  const base = {
    step_id: step.id,
    status: String(step.status ?? "done"),
    author: "",
    at_ms: step.at_ms ?? 0,
    visibility: "public",
  };
  if (kind === "message") {
    const content = String(step.body ?? "") || String(step.subtitle ?? "");
    return content ? { ...base, kind: "message", content, _complete: true } : null;
  }
  if (kind === "error") {
    const content = String(step.body ?? "") || String(step.title ?? "");
    return content ? { ...base, kind: "status", content, status: "error" } : null;
  }
  if (STATUS_STEP_KINDS.has(kind)) {
    const content = statusText(step);
    return content ? { ...base, kind: "status", content } : null;
  }
  return null;
}

/** Every public event once: first position, latest status and wording. */
export function publicCommunications(events) {
  const seen = new Map();
  const list = [];
  for (const event of events ?? []) {
    if (!isPublicCommunication(event)) continue;
    const key = communicationKey(event);
    if (seen.has(key)) {
      list[seen.get(key)] = event;
      continue;
    }
    seen.set(key, list.length);
    list.push(event);
  }
  return list;
}

/** A name a person can read, or the message's own author as a fallback. */
function authorOf(event, fallback) {
  const author = String(event?.author ?? "");
  if (author && !ROLE_AUTHORS.has(author.toLowerCase())) return author;
  return fallback || "";
}

/** An old event-only history still opens in the full execution component. */
function stepFromStatus(event) {
  return {
    id: String(event.step_id ?? event.id ?? communicationKey(event)),
    kind: "tool",
    title: String(event.content ?? ""),
    status: String(event.status ?? "done"),
    subtitle: "",
    detail: "",
    body: "",
    refs: [],
    at_ms: event.at_ms ?? 0,
    duration_ms: null,
    tone: "neutral",
    policy: null,
    parent_id: event.parent_id ?? null,
  };
}

function stepFromMessage(event) {
  const content = String(event.content ?? "");
  return { ...stepFromStatus(event), kind: "message", title: content, body: content };
}

/**
 * The alternating message/execution stream of one assistant answer. The
 * returned object is both the activity view and the reasoning controller the
 * chat drives, so a step can never be drawn in two parallel representations.
 */
export function createActivityView({ mode = "segmented" } = {}) {
  const timeline = h("div.msg__activityTimeline");
  const el = h("section.msg__activity", { hidden: true, "aria-label": "Agent activity" }, timeline);

  const blocks = new Map();
  const modelSteps = new Map();
  const modelEvents = new Map();
  const executions = [];
  const stepPanels = new Map();
  const realSteps = new Set();
  const publicChildSteps = new Set();
  const publicChildEvents = new Map();
  let currentExecution = null;
  let planExecution = null;
  let fallbackAuthor = "";
  let replayEvents = [];
  let viewMode = mode === "combined" ? "combined" : "segmented";
  let liveRunning = false;
  let stepSource = null;
  let storedLoader = null;
  // When the turn itself began, on the page's clock. Every live segment counts
  // its rows from the turn's start, never from the moment the segment opened.
  let turnStartedAt = null;
  // Where the segment being built began, in turn-relative milliseconds: 0 for
  // the first one, else the `at_ms` of the message that closed the previous
  // segment. Together with the next boundary (or the turn's own total) it is
  // the exact segment interval - no sum of step durations, whose parallel
  // calls overlap.
  let boundaryAt = 0;

  /** How long the live turn has run, or null when no live turn is measured. */
  const turnElapsed = () => (turnStartedAt == null ? null : Math.max(Date.now() - turnStartedAt, 0));

  /** A stored step's `at_ms` as a turn-relative boundary, or null when unknown. */
  const numberAt = (value) => {
    const at = Number(value);
    return Number.isFinite(at) && at >= 0 ? at : null;
  };

  const refresh = () => {
    const active = liveRunning && currentExecution;
    const hasWork = executions.some((entry) => !entry.panel.isEmpty || entry.panel.hasPlan);
    el.hidden = !active && blocks.size === 0 && !hasWork;
  };

  /** Tokens of the segment's outermost rows; children stay inside their block. */
  const segmentUsage = (entry) => {
    const steps = [...entry.steps.values()];
    const ids = new Set(steps.map((step) => step.id));
    const outermost = steps.filter((step) => !step.parent_id || !ids.has(step.parent_id));
    return {
      tokensIn: outermost.reduce((total, step) => total + (Number(step.tokens_in) || 0), 0),
      tokensOut: outermost.reduce((total, step) => total + (Number(step.tokens_out) || 0), 0),
    };
  };

  /**
   * Close exactly the segment before a message, or the last one at turn end.
   *
   * Its duration is the distance between boundaries: `endAt` is the `at_ms` of
   * the message that ends it, or `durationMs` is the turn's own total and the
   * segment ends with the turn. With neither, the interval is unknown and the
   * panel shows no made-up number (a live panel without a `done` falls back to
   * its own clock, which measures from the same boundary).
   */
  const closeExecution = ({ durationMs = null, endAt = null, autoCollapse = true, usage = null } = {}) => {
    const entry = currentExecution;
    if (!entry) return;
    currentExecution = null;
    // An empty segment is not a segment: narration can arrive before any work,
    // and the panel it would draw contains nothing to time.
    if (entry.panel.isEmpty && !entry.panel.hasPlan) {
      entry.panel.finish(0, false);  // stop its clock; it never had work to show
      entry.container.remove();
      const index = executions.indexOf(entry);
      if (index >= 0) executions.splice(index, 1);
      refresh();
      return;
    }
    const startAt = entry.startAt ?? 0;
    // A boundary that lies before a step it should end is not a timing this
    // trace can be trusted for (a legacy step without `at_ms` reads as 0):
    // the interval then stays unknown rather than becoming a false zero.
    const latestStepAt = [...entry.steps.values()].reduce(
      (latest, step) => Math.max(latest, Number(step.at_ms) || 0), startAt,
    );
    const exact = durationMs != null && Number(durationMs) >= startAt
      ? Number(durationMs) - startAt
      : endAt != null && endAt >= latestStepAt
        ? endAt - startAt
        : undefined;
    entry.panel.finish(
      exact,
      autoCollapse,
      usage ?? segmentUsage(entry),
    );
    if (!entry.live) entry.panel.setPlanCollapsed(true);
    refresh();
  };

  /** Start a segment only at the point the stream reaches it. */
  const ensureExecution = (elapsedMs = 0, turnElapsedMs = null) => {
    if (currentExecution) return currentExecution;
    const panel = createReasoningPanel({ showPublicMessages: viewMode === "combined" });
    if (storedLoader) panel.setStepSource(() => storedLoader);
    else if (stepSource) panel.setStepSource(stepSource);
    const container = h("div.msg__activityExecution", {}, panel.el, panel.planEl);
    const elapsedTurn = turnElapsedMs ?? turnElapsed() ?? elapsedMs;
    const entry = {
      panel, container, steps: new Map(), live: liveRunning,
      // The segment's own boundary, and how far the turn had run when it opened
      // (the panel's clock measures from the boundary, not from zero).
      startAt: boundaryAt ?? 0,
    };
    executions.push(entry);
    currentExecution = entry;
    timeline.append(container);
    if (entry.live) panel.start(Math.max(elapsedTurn - entry.startAt, 0), elapsedTurn);
    refresh();
    return entry;
  };

  /**
   * One public message, and the boundary it draws in the turn's timeline.
   *
   * `atKnown` says whether `at_ms` is a real trace timing: only a stored or
   * streamed step carries one. An event-only replay has no timing to trust, so
   * it never invents a boundary (nor a segment interval).
   */
  const addMessage = (event, { atKnown = false } = {}) => {
    const key = communicationKey(event);
    const content = String(event.content ?? "");
    if (!content) return false;
    const own = authorOf(event, "");
    const author = own || fallbackAuthor;
    const at = atKnown && !event.parent_id ? numberAt(event.at_ms) : null;
    const known = blocks.get(key);
    if (known) {
      // The trace carries full prose; a typed event may be capped by the wire.
      if (known.content !== content && (event._complete || !known.complete)) {
        known.content = content;
        known.text.innerHTML = renderMarkdown(content);
      }
      known.complete ||= Boolean(event._complete);
      if (own && known.author !== own) {
        known.own = own;
        known.author = own;
        known.authorNode.textContent = own;
        known.authorNode.hidden = false;
      }
      // The same message arrived earlier as a typed event; its step frame now
      // supplies the real boundary, if it is the newest one.
      if (at != null && at >= boundaryAt && !currentExecution) boundaryAt = at;
      return true;
    }

    // Only a top-level utterance ends a segment. A child's public commentary
    // must not detach later child steps from their sub-agent's trace block.
    if (!event.parent_id) closeExecution({ endAt: at });
    if (at != null) boundaryAt = at;
    const authorNode = h("span.msg__activityAuthor", { text: author });
    authorNode.hidden = !author;
    const text = h("div.msg__activityText", { html: renderMarkdown(content) });
    const node = h("div.msg__activityMessage", { dataset: { kind: "message" } }, authorNode, text);
    blocks.set(key, { el: node, text, authorNode, content, author, own, complete: Boolean(event._complete) });
    timeline.append(node);
    refresh();
    return true;
  };

  /** Add a real trace step, or an event-only fallback when `synthetic` is set. */
  const addExecutionStep = (step, { synthetic = false } = {}) => {
    if (!step || typeof step !== "object" || !step.id) return false;
    const id = String(step.id);
    modelSteps.set(id, step);
    if (!synthetic) realSteps.add(id);
    if (viewMode === "combined") {
      const entry = stepPanels.get(id) ?? stepPanels.get(step.parent_id) ?? ensureExecution(0, turnElapsed());
      entry.steps.set(id, step);
      stepPanels.set(id, entry);
      if (step.kind === "plan") planExecution = entry;
      entry.panel.upsert(step);
      refresh();
      return true;
    }
    if (step.kind === "message" && step.parent_id && publicChildSteps.has(id)) {
      const stored = publicChildEvents.get(id);
      const content = String(step.body ?? "") || String(step.subtitle ?? "") || String(stored?.content ?? "");
      return addMessage({ ...stored, kind: "message", step_id: id, parent_id: step.parent_id, content, _complete: true });
    }
    if (step.kind === "message" && !step.parent_id) {
      const event = eventFromStep(step);
      return event ? addMessage(event, { atKnown: !synthetic }) : false;
    }
    // A late frame belongs to its original segment; a new child belongs with
    // its parent even if the public conversation has since moved on.
    const isPlan = step.kind === "plan";
    const entry = stepPanels.get(id)
      ?? (isPlan ? planExecution : null)
      ?? stepPanels.get(step.parent_id)
      ?? ensureExecution(0, turnElapsed());
    if (isPlan) planExecution = entry;
    entry.steps.set(id, step);
    stepPanels.set(id, entry);
    entry.panel.upsert(step);
    refresh();
    return true;
  };

  const addStatus = (event) => {
    const id = String(event.step_id ?? event.id ?? communicationKey(event));
    // The full trace wins: a compact replay must never erase its payload.
    if (realSteps.has(id)) return true;
    return addExecutionStep(stepFromStatus(event), { synthetic: true });
  };

  const addPublicMessage = (event) => {
    if (viewMode === "combined") {
      const id = String(event.step_id);
      if (realSteps.has(id)) return true;
      return addExecutionStep(stepFromMessage(event), { synthetic: true });
    }
    if (event.parent_id && event.step_id) {
      const id = String(event.step_id);
      publicChildSteps.add(id);
      publicChildEvents.set(id, event);
      // Explicit public commentary belongs to the conversation, not to a
      // second copy inside the sub-agent's execution report.
      const entry = stepPanels.get(id);
      if (entry) {
        entry.panel.remove(id);
        entry.steps.delete(id);
        stepPanels.delete(id);
      }
    }
    return addMessage(event);
  };

  const resetView = () => {
    for (const entry of executions) entry.panel.finish(0, false);
    blocks.clear();
    executions.length = 0;
    stepPanels.clear();
    realSteps.clear();
    publicChildSteps.clear();
    publicChildEvents.clear();
    currentExecution = null;
    planExecution = null;
    boundaryAt = 0;
    timeline.replaceChildren();
  };

  refresh();

  return {
    el,

    get size() {
      return blocks.size;
    },

    get isEmpty() {
      return executions.every((entry) => entry.panel.isEmpty && !entry.panel.hasPlan);
    },

    get hasPlan() {
      return executions.some((entry) => entry.panel.hasPlan);
    },

    get mode() {
      return viewMode;
    },

    /** Record one public event; false when it is internal or the final answer. */
    add(event) {
      if (!isPublicCommunication(event)) return false;
      if (event.kind === "message") {
        if (!event.step_id) return false;
        modelEvents.set(communicationKey(event), event);
        return addPublicMessage(event);
      }
      modelEvents.set(communicationKey(event), event);
      return addStatus(event);
    },

    /** Record stored events; hydrate uses this same replay with the full trace. */
    apply(events) {
      replayEvents = [...(events ?? [])];
      for (const event of replayEvents) this.add(event);
      if (!liveRunning) closeExecution();
      return this;
    },

    /** A top-level message step is prose; work arrives through `upsert`. */
    observeStep(step) {
      const event = eventFromStep(step);
      return event?.kind === "message" ? addExecutionStep(step) : false;
    },

    /** The live reasoning controller: one panel per execution segment. */
    start(elapsedMs = 0) {
      storedLoader = null;
      liveRunning = true;
      turnStartedAt = Date.now() - Math.max(Number(elapsedMs) || 0, 0);
      boundaryAt = 0;
      ensureExecution(elapsedMs);
    },

    /**
     * The server's own count of the running turn (the `attached` frame).
     *
     * The history read that precedes an attach is older than the attach; only
     * this frame says how long the turn has really run, so the live clocks are
     * re-based on it. The segment keeps its own offset from the turn's start.
     */
    setElapsed(elapsedMs) {
      const elapsed = Number(elapsedMs);
      if (!Number.isFinite(elapsed) || elapsed < 0) return;
      turnStartedAt = Date.now() - elapsed;
      if (liveRunning && currentExecution) {
        currentExecution.panel.rebase(
          Math.max(elapsed - (currentExecution.startAt ?? 0), 0), elapsed,
        );
      }
    },

    upsert(step) {
      return addExecutionStep(step);
    },

    remove(id) {
      const key = String(id);
      modelSteps.delete(key);
      modelEvents.delete(`message:step:${key}`);
      modelEvents.delete(`status:step:${key}`);
      const entry = stepPanels.get(key);
      if (!entry) return;
      entry.panel.remove(key);
      entry.steps.delete(key);
      stepPanels.delete(key);
      realSteps.delete(key);
      refresh();
    },

    appendReasoning(id, delta) {
      const key = String(id);
      const known = modelSteps.get(key);
      if (known) modelSteps.set(key, { ...known, body: String(known.body ?? "") + delta });
      stepPanels.get(key)?.panel.appendReasoning(id, delta);
    },

    finish(durationMs, autoCollapse = true, usage = null) {
      // The server reports the whole turn. Each segment ends at its own next
      // boundary; this total closes the last one, and only a single panel
      // keeps the turn's tokens.
      const single = executions.length === 1;
      closeExecution({ durationMs, autoCollapse, usage: single ? usage : null });
      liveRunning = false;
      refresh();
    },

    setStepSource(source) {
      stepSource = source;
      for (const entry of executions) entry.panel.setStepSource(source);
    },

    setPlanCollapsed(collapsed) {
      for (const entry of executions) entry.panel.setPlanCollapsed(collapsed);
    },

    applyStoredPlan(stored) {
      (planExecution ?? ensureExecution(0, turnElapsed())).panel.applyStoredPlan(stored);
      refresh();
    },

    /** Switch representation of the same steps, including the running turn. */
    setMode(next) {
      if (!["segmented", "combined"].includes(next) || next === viewMode) return this;
      viewMode = next;
      const trace = [...modelSteps.values()];
      replayEvents = [...modelEvents.values()];
      this.hydrate(trace, { loadStep: storedLoader, keepRunning: liveRunning });
      return this;
    },

    /** Rebuild a stored stream in one chronological order, with lazy payloads. */
    hydrate(steps, { loadStep = null, keepRunning = false, durationMs = null } = {}) {
      const resume = keepRunning && liveRunning;
      storedLoader = loadStep;
      liveRunning = resume;
      if (!resume) {
        turnStartedAt = null;
        boundaryAt = 0;
      }
      const trace = [...(steps ?? [])];
      const events = publicCommunications(replayEvents);
      resetView();
      modelSteps.clear();
      modelEvents.clear();
      for (const step of trace) modelSteps.set(String(step.id), step);
      for (const event of events) modelEvents.set(communicationKey(event), event);
      const traceIds = new Set(trace.map((step) => String(step.id)));
      const topMessages = new Set(trace.filter((step) => step.kind === "message" && !step.parent_id).map((step) => String(step.id)));
      const storedMessages = new Map(events
        .filter((event) => event.kind === "message" && topMessages.has(String(event.step_id)))
        .map((event) => [String(event.step_id), event]));
      const items = trace.map((step, order) => ({ step, at_ms: step.at_ms ?? 0, order }));
      let order = items.length;
      for (const event of events) {
        if (event.kind === "message") {
          if (!event.step_id || topMessages.has(String(event.step_id))) continue;
        } else if (traceIds.has(String(event.step_id ?? event.id))) continue;
        items.push({ event, at_ms: event.at_ms ?? 0, order: order++ });
      }
      items.sort((a, b) => (Number(a.at_ms) || 0) - (Number(b.at_ms) || 0) || a.order - b.order);

      for (const item of items) {
        if (item.event) {
          this.add(item.event);
          continue;
        }
        const step = item.step;
        if (viewMode === "combined") addExecutionStep(step);
        else if (step.kind === "message" && !step.parent_id) {
          const derived = eventFromStep(step);
          const stored = storedMessages.get(String(step.id));
          if (derived && stored) {
            addMessage(
              { ...derived, ...stored, kind: "message", step_id: step.id, content: derived.content, _complete: true },
              { atKnown: true },
            );
          } else if (derived) addMessage(derived, { atKnown: true });
          else if (stored) addMessage(stored);
        } else addExecutionStep(step);
      }
      // Every segment before a message already ended at that message's boundary;
      // the turn's own recorded total closes the last one (unknown when absent).
      if (!resume) closeExecution({ durationMs });
      refresh();
    },

    setAuthor(name) {
      fallbackAuthor = String(name ?? "");
      for (const block of blocks.values()) {
        if (block.own) continue;
        block.author = fallbackAuthor;
        block.authorNode.textContent = fallbackAuthor;
        block.authorNode.hidden = !fallbackAuthor;
      }
      return this;
    },

    clear() {
      replayEvents = [];
      modelSteps.clear();
      modelEvents.clear();
      storedLoader = null;
      liveRunning = false;
      turnStartedAt = null;
      boundaryAt = 0;
      resetView();
      refresh();
      return this;
    },
  };
}
