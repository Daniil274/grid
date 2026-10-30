/**
 * What the agent said and what it is doing: the public communication of a turn.
 *
 * The server records one typed event per communication (`web_chat/observer.py`):
 * a **message** is what the agent says to people, a **status** is a short line
 * around its words, and a **report** is internal work - thinking, a sub-agent's
 * answer, a tool's output - which never leaves the server. `visibility` is the
 * gate, and this module applies it again: a report cannot be drawn here even if
 * one arrived by mistake.
 *
 * The view renders the narration as prose blocks with their author, and the
 * work as a single compact status line. Both survive the actions between them
 * and the final answer: a turn that calls tools no longer loses the sentences
 * it said before each call, and a reconnect redraws the same blocks.
 *
 * The same events arrive from two sources and must not double up:
 *   - **live** - derived from the step frames of the turn, which are exactly
 *     what the stored trace is made of (`eventFromStep`);
 *   - **replay** - the `events` of a stored message (`message.events`).
 * A derived event and its stored twin share the step id, so the step id - not
 * the content or the order - is what dedupes them.
 */

import { h } from "../lib/dom.js";
import { renderMarkdown } from "../lib/markdown.js";

/** The kinds a person may read. A report is internal by contract. */
export const COMMUNICATION_KINDS = new Set(["message", "status"]);

/**
 * Steps whose title is a status a person may read (`PUBLIC_STATUS_STEP_KINDS`
 * in web_chat/observer.py). Their body is not public.
 */
export const STATUS_STEP_KINDS = new Set(["prepare", "routing", "tool", "compact", "plan", "handoff", "mcp"]);

/** Authors that are roles, not names: the message itself already says them. */
const ROLE_AUTHORS = new Set(["assistant", "system", "grid", "user"]);

/** Whether an event is a public communication this view may draw. */
export function isPublicCommunication(event) {
  if (!event || typeof event !== "object") return false;
  const kind = String(event.kind ?? "");
  if (!COMMUNICATION_KINDS.has(kind)) return false;
  // A missing visibility is the wire's public default; "internal" is the gate.
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

/**
 * The public event a live step frame stands for, or null for a silent step.
 *
 * Mirrors `event_from_step` in web_chat/observer.py: a top-level `message` step
 * is public narration; a top-level tool/prepare/... step is a public status
 * carrying its title only. A step inside a sub-agent block (`parent_id`) is
 * internal work, never a message of this turn.
 */
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
    return content ? { ...base, kind: "message", content } : null;
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

/** Every public event of a list, once, in the order it first arrived. */
export function publicCommunications(events) {
  const seen = new Set();
  const list = [];
  for (const event of events ?? []) {
    if (!isPublicCommunication(event)) continue;
    const key = communicationKey(event);
    if (seen.has(key)) continue;
    seen.add(key);
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

/**
 * The activity section of one assistant message.
 *
 * @returns the element the message inserts above its answer, plus the handles
 *   the live turn, a stored conversation and a reconnect drive it with.
 */
export function createActivityView() {
  const messages = h("div.msg__activityMessages");
  const label = h("span.msg__activityLabel");
  const count = h("span.msg__activityCount");
  const status = h(
    "div.msg__activityStatus",
    { hidden: true },
    h("span.msg__activityMark", { "aria-hidden": "true", text: "›" }),
    label,
    count,
  );
  const el = h(
    "section.msg__activity",
    { hidden: true, "aria-label": "Agent activity" },
    messages,
    status,
  );

  /** Message blocks by identity: the node and what it currently says. */
  const blocks = new Map();
  /** The statuses in the order they happened, by identity. */
  const statuses = new Map();
  const statusOrder = [];
  let fallbackAuthor = "";

  const refresh = () => {
    const hasMessages = messages.childNodes.length > 0;
    const hasStatus = statusOrder.length > 0;
    el.hidden = !hasMessages && !hasStatus;
    status.hidden = !hasStatus;
  };

  const paintStatus = () => {
    const latest = statuses.get(statusOrder.at(-1));
    label.textContent = latest ? latest.content : "";
    count.textContent = statuses.size > 1 ? `${statuses.size} steps` : "";
    status.dataset.status = latest?.status ?? "";
    status.title = statusOrder.map((key) => statuses.get(key).content).join("\n");
    refresh();
  };

  const addMessage = (event) => {
    const key = communicationKey(event);
    const content = String(event.content ?? "");
    if (!content) return;
    // An event names its author when it can; otherwise the message's own agent
    // is the fallback, and it must follow routing (`setAuthor`).
    const own = authorOf(event, "");
    const author = own || fallbackAuthor;
    const known = blocks.get(key);
    if (known) {
      if (known.content !== content) {
        known.content = content;
        known.text.innerHTML = renderMarkdown(content);
      }
      if (own && known.author !== own) {
        known.own = own;
        known.author = own;
        known.authorNode.textContent = own;
        known.authorNode.hidden = false;
      }
      return;
    }
    const authorNode = h("span.msg__activityAuthor", { text: author });
    authorNode.hidden = !author;
    const text = h("div.msg__activityText", { html: renderMarkdown(content) });
    const node = h("div.msg__activityMessage", { dataset: { kind: "message" } }, authorNode, text);
    blocks.set(key, { el: node, text, authorNode, content, author, own });
    messages.append(node);
    refresh();
  };

  const addStatus = (event) => {
    const key = communicationKey(event);
    const content = String(event.content ?? "");
    if (!content) return;
    const known = statuses.get(key);
    if (known) {
      known.content = content;
      known.status = String(event.status ?? known.status);
    } else {
      statuses.set(key, { content, status: String(event.status ?? "done") });
      statusOrder.push(key);
    }
    paintStatus();
  };

  refresh();

  return {
    el,

    /** How many narration blocks are on screen. */
    get size() {
      return blocks.size;
    },

    /** The newest status, as the compact line says it. */
    get statusText() {
      return label.textContent;
    },

    get statusCount() {
      return statuses.size;
    },

    /** Record one event; false when it is internal or silent. */
    add(event) {
      if (!isPublicCommunication(event)) return false;
      if (event.kind === "message") {
        // The answer itself is the message's body: only what came before it is
        // a block here, or the answer would be drawn twice.
        if (!event.step_id) return false;
        addMessage(event);
        return true;
      }
      addStatus(event);
      return true;
    },

    /** Record a stored message's `events` (a reload, a reconcile, an adopt). */
    apply(events) {
      for (const event of events ?? []) this.add(event);
      return this;
    },

    /** Record what a live step frame says; null for a silent step. */
    observeStep(step) {
      const event = eventFromStep(step);
      return event ? this.add(event) : false;
    },

    /**
     * The agent running this turn, once routing knows it. Narration that came
     * without an author of its own takes this name, before and after routing.
     */
    setAuthor(name) {
      fallbackAuthor = String(name ?? "");
      for (const block of blocks.values()) {
        if (block.own) continue;
        const author = fallbackAuthor;
        block.authorNode.textContent = author;
        block.authorNode.hidden = !author;
      }
      return this;
    },

    clear() {
      blocks.clear();
      statuses.clear();
      statusOrder.length = 0;
      messages.replaceChildren();
      paintStatus();
      return this;
    },
  };
}
