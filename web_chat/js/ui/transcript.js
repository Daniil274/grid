/**
 * The message list: owns scroll behaviour and the empty state.
 *
 * Scrolling follows the stream until the reader scrolls away. Content growth
 * must not count as an interruption: after a new step arrives, measuring the
 * distance to the bottom is already too late.
 */

import { onFrame } from "../lib/dom.js";
import { createMessage } from "./message.js";

const BOTTOM_TOLERANCE_PX = 4;

/**
 * Make an answer a polite live region that speaks once, when it settles.
 *
 * The answer body is repainted every frame while tokens stream in, so a plain
 * `aria-live` would narrate every fragment. `aria-busy` stays true for the
 * whole turn - the caller brackets it with `setWaiting(true)`/`setWaiting(false)`
 * (js/chat.js) - and only clears when the answer is whole, so a screen reader
 * announces the finished answer rather than each token.
 */
function makeAnswerLive(message) {
  const body = message.el.querySelector(".msg__body");
  if (!body) return;
  body.setAttribute("aria-live", "polite");
  body.setAttribute("aria-atomic", "false");
  body.setAttribute("aria-busy", "false");
  const setWaiting = message.setWaiting;
  message.setWaiting = (waiting) => {
    body.setAttribute("aria-busy", String(Boolean(waiting)));
    setWaiting(waiting);
  };
}

/** Only actual reader input can take scrolling away from the live stream. */
export function createScrollFollower(container) {
  let following = true;
  let draggingScrollbar = false;
  const atEnd = () =>
    container.scrollHeight - container.scrollTop - container.clientHeight <= BOTTOM_TOLERANCE_PX;

  return {
    get following() { return following; },
    onUserScrollIntent(away = false) {
      if (away) following = false;
    },
    onScrollbarDragStart() {
      draggingScrollbar = true;
      this.onUserScrollIntent(true);
    },
    onScrollbarDragEnd() {
      if (!draggingScrollbar) return;
      draggingScrollbar = false;
      if (atEnd()) following = true;
    },
    onScroll() {
      // Browser scroll events also fire after our own writes and content changes.
      // Only explicit upward input can pause following; reaching the end resumes it.
      if (!draggingScrollbar && !following && atEnd()) following = true;
    },
    scrollToBottom(force = false) {
      if (!force && !following) return;
      following = true;
      container.scrollTop = container.scrollHeight;
    },
  };
}

export function createTranscript({
  container, emptyState, jumpButton, onEditMessage, onSpeakMessage, onReportMessage, onContinue, onSwitchVersion,
  onDeleteMessage, onForkMessage, onContinueInNewChat, onRestoreCheckpoint,
}) {
  /** Live message handles by id - the speech player reports state by id. */
  const messages = new Map();
  const scrollFollower = createScrollFollower(container);

  /** Only the newest answer offers "Continue in new chat"; earlier ones hide it. */
  const offersContinue = () => {
    const last = [...messages.values()].reverse().find((handle) => handle.role === "assistant" && handle.text) ?? null;
    for (const handle of messages.values()) handle.setContinueInNewChat?.(handle === last);
  };

  const syncJumpButton = () => {
    const hasMessages = container.querySelector(".msg") !== null;
    jumpButton.classList.toggle("is-visible", hasMessages && !scrollFollower.following);
  };

  const scrollToBottom = (force = false) => {
    scrollFollower.scrollToBottom(force);
    syncJumpButton();
  };
  const followStream = onFrame(() => scrollToBottom());

  container.addEventListener("scroll", () => {
    scrollFollower.onScroll();
    syncJumpButton();
  }, { passive: true });
  container.addEventListener("wheel", (event) => {
    scrollFollower.onUserScrollIntent(event.deltaY < 0);
    syncJumpButton();
  }, { passive: true });
  let touchY = null;
  container.addEventListener("touchstart", (event) => {
    touchY = event.touches[0]?.clientY ?? null;
    scrollFollower.onUserScrollIntent();
  }, { passive: true });
  container.addEventListener("touchmove", (event) => {
    const nextY = event.touches[0]?.clientY ?? null;
    scrollFollower.onUserScrollIntent(touchY != null && nextY != null && nextY > touchY);
    touchY = nextY;
    syncJumpButton();
  }, { passive: true });
  container.addEventListener("touchend", () => { touchY = null; }, { passive: true });
  container.addEventListener("pointerdown", (event) => {
    const right = container.getBoundingClientRect().right;
    const scrollbarWidth = container.offsetWidth - container.clientWidth;
    if (scrollbarWidth > 0 && event.clientX >= right - scrollbarWidth) {
      scrollFollower.onScrollbarDragStart();
      syncJumpButton();
    }
  });
  const endScrollbarDrag = () => {
    scrollFollower.onScrollbarDragEnd();
    syncJumpButton();
  };
  document.addEventListener("pointerup", endScrollbarDrag);
  document.addEventListener("pointercancel", endScrollbarDrag);
  document.addEventListener("keydown", (event) => {
    if (event.target instanceof HTMLElement && event.target.closest("input, textarea, [contenteditable]")) return;
    if (!container.matches(":hover") && !container.contains(document.activeElement)) return;
    if (["ArrowUp", "PageUp", "Home"].includes(event.key)) scrollFollower.onUserScrollIntent(true);
    else if (["ArrowDown", "PageDown", "End", " "].includes(event.key)) scrollFollower.onUserScrollIntent();
    syncJumpButton();
  });
  jumpButton.addEventListener("click", () => scrollToBottom(true));

  const setEmpty = (empty) => {
    emptyState.hidden = !empty;
    if (empty) container.append(emptyState);
  };

  // Shown only when a load takes long enough to notice; a quick one never flickers.
  let loadingTimer = null;

  return {
    /** A chat is being fetched: what is on screen is not it yet. */
    setLoading(loading) {
      clearTimeout(loadingTimer);
      container.setAttribute("aria-busy", String(loading));
      if (!loading) {
        container.classList.remove("is-loading");
        return;
      }
      loadingTimer = setTimeout(() => container.classList.add("is-loading"), 120);
    },

    /** @returns the message handle, so the caller can stream into it. */
    add(options) {
      setEmpty(false);
      const message = createMessage({ ...options, onContinueInNewChat, onEdit: onEditMessage, onSpeak: onSpeakMessage, onReport: onReportMessage, onContinue, onSwitchVersion, onDelete: onDeleteMessage, onFork: onForkMessage, onRestore: onRestoreCheckpoint });
      messages.set(message.id, message);
      container.append(message.el);
      if (options.role === "assistant" && !options.content) makeAnswerLive(message);
      scrollToBottom(true);
      offersContinue();
      return message;
    },

    get(id) {
      return messages.get(id) ?? null;
    },

    setSpeechState(id, state) {
      messages.get(id)?.setSpeechState(state);
    },

    /** Replace the whole transcript with a stored conversation. */
    /** `loadStep(messageId, stepId)` fetches a lazy trace step of a stored message. */
    render(stored, { loadStep = null } = {}) {
      container.replaceChildren();
      messages.clear();
      for (const message of stored) {
        const handle = this.add({
          role: message.role,
          content: message.content,
          timestamp: message.timestamp,
          kind: message.kind,
          compaction: message.compaction,
          context: message.context ?? message.metadata ?? null,
          images: message.images,
          messageId: message.id,
          versions: message.versions,
          checkpoint: message.checkpoint ?? null,
        });
        handle.reasoning?.hydrate(message.trace, {
          loadStep: loadStep && message.id ? (stepId) => loadStep(message.id, stepId) : null,
        });
        if (message.interruption) {
          handle.setInterrupted(message.interruption, { resumable: message.interruption.resumable });
        }
      }
      offersContinue();
      // Every stored plan starts folded; the newest one is the one to follow.
      [...messages.values()].reverse().find((handle) => handle.reasoning?.hasPlan)?.reasoning.setPlanCollapsed(false);
      setEmpty(stored.length === 0);
      scrollToBottom(true);
    },

    /** Whether any answer on screen shows a plan card. */
    hasPlan() {
      return [...messages.values()].some((handle) => handle.reasoning?.hasPlan);
    },

    /** Fold every plan card on screen: a new request's plan is the one to follow. */
    collapsePlans() {
      for (const handle of messages.values()) handle.reasoning?.setPlanCollapsed(true);
    },

    clear() {
      container.replaceChildren();
      messages.clear();
      setEmpty(true);
    },

    /**
     * Bring a transcript that shows the same messages as *stored* up to date -
     * ids and versions - without re-rendering it. False when they differ.
     */
    adopt(stored) {
      const handles = [...messages.values()];
      if (handles.length !== stored.length) return false;
      handles.forEach((handle, index) => handle.adopt({
        id: stored[index].id, versions: stored[index].versions, checkpoint: stored[index].checkpoint ?? null,
      }));
      return true;
    },

    /** The newest message the user sent as a turn (it has an id), or null. */
    lastUserMessage() {
      return [...messages.values()].reverse().find((handle) => handle.role === "user" && handle.messageId) ?? null;
    },

    /** The newest assistant message with text, or null. */
    lastAnswer() {
      return [...messages.values()].reverse().find((handle) => handle.role === "assistant" && handle.text) ?? null;
    },

    /** Versions of the newest edited message on screen, or null. */
    lastVersions() {
      return [...messages.values()].reverse().find((handle) => handle.versions)?.versions ?? null;
    },

    /** Whether the newest message is an interruption Continue can resume;
     * a compaction marker after it does not end the dialogue. */
    resumable() {
      const turns = [...messages.values()].filter((handle) => handle.kind !== "compaction");
      return turns.at(-1)?.resumable === true;
    },

    /** Insert a message just before *anchor* - a message the user sent mid-turn. */
    addBefore(anchor, options) {
      const message = createMessage({ ...options, onContinueInNewChat, onEdit: onEditMessage, onSpeak: onSpeakMessage, onReport: onReportMessage, onContinue, onSwitchVersion, onDelete: onDeleteMessage, onFork: onForkMessage, onRestore: onRestoreCheckpoint });
      messages.set(message.id, message);
      container.insertBefore(message.el, anchor.el);
      offersContinue();
      return message;
    },

    /** A new turn is starting: no earlier interruption can be continued now. */
    retireContinue() {
      for (const message of messages.values()) message.retireContinue();
    },

    /** Recompute which answer offers "Continue in new chat" (the newest one). */
    refreshContinueInNewChat: offersContinue,

    /** The user's message (or Continue) that started the turn *answer* answers. */
    requestOf(answer) {
      const handles = [...messages.values()];
      for (let index = handles.indexOf(answer) - 1; index >= 0; index -= 1) {
        if (handles[index].role === "user") return handles[index];
        // A Continue started it: a marker, which offers nothing.
        if (handles[index].role === "marker" && handles[index].kind !== "compaction") return null;
      }
      return null;
    },

    /** The newest message handle, or null. */
    last() {
      return [...messages.values()].at(-1) ?? null;
    },

    count() {
      return container.querySelectorAll(".msg").length;
    },

    /** Called on every streamed fragment; cheap because it coalesces per frame. */
    follow: followStream,
    isFollowing: () => scrollFollower.following,
    scrollToBottom,
  };
}
