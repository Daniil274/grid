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
    /** Put the mode back as it was: a re-render is not reader input. */
    setFollowing(value) {
      following = Boolean(value);
    },
    scrollToBottom(force = false) {
      if (!force && !following) return;
      following = true;
      container.scrollTop = container.scrollHeight;
    },
  };
}

/**
 * Where the reader is looking: the message at the top edge of the viewport,
 * how far its own top sits above that edge, and whether the stream is followed.
 * Null when the host has no layout to measure (a test harness, a detached page):
 * then nothing may be assumed about the place, and nothing is restored.
 */
function readPlace(container, messages) {
  const box = container.getBoundingClientRect?.();
  if (!box) return null;
  const list = [...container.querySelectorAll(".msg")];
  if (!list.length) return null;
  let index = 0;
  for (let at = 0; at < list.length; at += 1) {
    const rect = list[at].getBoundingClientRect?.();
    if (!rect) return null;
    if (rect.top <= box.top + 1) index = at;
    else break;
  }
  const rect = list[index].getBoundingClientRect();
  const handle = messages.get(list[index].getAttribute("id"));
  return { index, messageId: handle?.messageId ?? null, offset: rect.top - box.top };
}

/**
 * A reveal that could not be applied yet, and the frames spent settling one.
 *
 * The end of the list is not a number one can read once: an off-screen message
 * is laid out at an estimated height (styles/chat.css keeps it out of layout
 * until it comes into view), so `scrollHeight` is an estimate that changes as
 * boxes are rendered. And a transcript in a container without layout - a hidden
 * pane, a tab that has not been shown yet - reports zero for everything. Hence
 * a reveal is applied, then re-applied for a few frames until the position is
 * stable, and held until the container can be measured.
 */
const SETTLE_FRAMES = 8;
/** Passes allowed to land on a place: layout settles over a few frames. */
const RESTORE_FRAMES = 8;

export function createTranscript({
  container, emptyState, jumpButton, onEditMessage, onSpeakMessage, onReportMessage, onContinue, onSwitchVersion,
  onDeleteMessage, onForkMessage, onContinueInNewChat, onRestoreCheckpoint,
}) {
  /** Live message handles by id - the speech player reports state by id. */
  const messages = new Map();
  const scrollFollower = createScrollFollower(container);
  /** >0 while a whole conversation is being built: appending must not scroll. */
  let batching = 0;
  /** A reveal held back because the container had no layout to scroll yet. */
  let pendingReveal = false;
  /** Frames left in settling a reveal; zero when none is in flight. */
  let settling = 0;
  let lastHeight = -1;
  /** The place being restored right now, and the frames it may take. */
  let restoring = null;
  let restoreFrames = 0;
  let stablePasses = 0;
  /**
   * Bumped whenever a landing is decided anew. Frame callbacks of the landing
   * before it (a chat replaced while its frames were pending) stop there.
   */
  let landing = 0;

  /** The reader's place on screen right now, or null when it cannot be read. */
  const currentPlace = () => {
    const found = readPlace(container, messages);
    return found ? { ...found, following: scrollFollower.following } : null;
  };

  /** The message a place anchors to: by stored id first, by position otherwise. */
  const anchorOf = (place) => {
    const list = [...container.querySelectorAll(".msg")];
    if (!list.length) return null;
    return (place.messageId
      ? list.find((node) => messages.get(node.getAttribute("id"))?.messageId === place.messageId)
      : null) ?? list[Math.min(place.index, list.length - 1)] ?? null;
  };

  /** One pass at the place: scroll by exactly the distance the anchor is off. */
  const stepToPlace = (place) => {
    const box = container.getBoundingClientRect?.();
    const anchor = anchorOf(place);
    const rect = anchor?.getBoundingClientRect?.();
    if (!box || !rect) return true;
    // Same expression as when the place was read: any difference is scroll.
    const delta = (rect.top - box.top) - place.offset;
    if (Math.abs(delta) <= 1) return true;
    container.scrollTop += delta;
    return false;
  };

  /**
   * Put the reader back where *place* found them after the transcript was
   * rebuilt.
   *
   * One pass is not enough in a real chat: an off-screen message is laid out at
   * an estimated height (styles/chat.css), so the anchor's position is an
   * estimate until the boxes around it are rendered, and the first correction
   * moves them. A few passes converge on the same pixels the reader left.
   */
  const restoreStep = (mine) => {
    if (!restoring || mine !== landing) return;
    restoreFrames -= 1;
    // Landing once is not proof: the boxes around the anchor are rendered as
    // they come into view, and the last of them can shift the place again.
    // Two passes that agree are what settles it.
    stablePasses = stepToPlace(restoring) ? stablePasses + 1 : 0;
    if (stablePasses >= 2 || restoreFrames <= 0) {
      restoring = null;
      stablePasses = 0;
      syncJumpButton();
      return;
    }
    nextFrame(() => restoreStep(mine));
  };

  const restorePlace = (place) => {
    if (!place) return;
    // A place outranks a reveal that is still settling or waiting for layout.
    pendingReveal = false;
    settling = 0;
    lastHeight = -1;
    if (!anchorOf(place)) return;
    scrollFollower.setFollowing(place.following);
    restoring = place;
    restoreFrames = RESTORE_FRAMES;
    stablePasses = 0;
    landing += 1;
    const mine = landing;
    if (stepToPlace(place)) { restoring = null; syncJumpButton(); return; }
    syncJumpButton();
    nextFrame(() => restoreStep(mine));
  };

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

  const nextFrame = globalThis.requestAnimationFrame ?? ((fn) => setTimeout(fn, 16));

  /** Whether the container is on screen with a box that can be scrolled. */
  const measurable = () => {
    if (typeof container.getClientRects === "function" && container.getClientRects().length === 0) return false;
    return container.clientHeight > 0;
  };

  /** One anchored step towards the end; true while settling may continue. */
  const settleStep = (mine) => {
    if (mine !== landing) return;
    if (!scrollFollower.following || settling <= 0) {
      settling = 0;
      lastHeight = -1;
      return;
    }
    settling -= 1;
    const height = container.scrollHeight;
    const gap = height - container.scrollTop - container.clientHeight;
    // The list stopped moving under the reveal: this is the end.
    if (height === lastHeight && gap <= 1) {
      settling = 0;
      lastHeight = -1;
      return;
    }
    lastHeight = height;
    scrollToBottom(true);
    nextFrame(() => settleStep(mine));
  };

  /**
   * Show the end of the list.
   *
   * One `scrollTop = scrollHeight` is not enough to call a chat open: an
   * off-screen message is laid out at an estimated height (styles/chat.css),
   * so the height is an estimate until the boxes near the end are rendered,
   * and it shifts under the write. The reveal is therefore applied and
   * re-anchored for a few frames, and if the container has no layout at all
   * (a hidden pane, a tab shown for the first time) it is held until it does.
   */
  const revealEnd = () => {
    settling = 0;
    lastHeight = -1;
    landing += 1;
    const mine = landing;
    if (!measurable()) {
      pendingReveal = true;
      return;
    }
    pendingReveal = false;
    scrollToBottom(true);
    settling = SETTLE_FRAMES;
    nextFrame(() => settleStep(mine));
  };

  // Content that arrives after the render - the chat's stored plan, a lazy
  // trace step, an image that finishes loading - grows the list under the
  // reader. A reader at the end keeps up with it; one further up is left alone.
  if (typeof MutationObserver === "function") {
    new MutationObserver(() => {
      if (scrollFollower.following && !batching) followStream();
    }).observe(container, { childList: true, subtree: true, characterData: true });
  }
  container.addEventListener("load", () => {
    if (scrollFollower.following) followStream();
  }, true);
  // A container that had no box when it was revealed (hidden, not laid out yet).
  if (typeof ResizeObserver === "function") {
    new ResizeObserver(() => {
      if (pendingReveal) revealEnd();
    }).observe(container);
  }

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

    /**
     * `reveal: false` appends without moving the reader: a resync attaches, it
     * does not open the chat.
     * @returns the message handle, so the caller can stream into it.
     */
    add(options, { reveal = true } = {}) {
      setEmpty(false);
      const message = createMessage({ ...options, onContinueInNewChat, onEdit: onEditMessage, onSpeak: onSpeakMessage, onReport: onReportMessage, onContinue, onSwitchVersion, onDelete: onDeleteMessage, onFork: onForkMessage, onRestore: onRestoreCheckpoint });
      messages.set(message.id, message);
      container.append(message.el);
      if (options.role === "assistant" && !options.content) makeAnswerLive(message);
      if (reveal && !batching) revealEnd();
      offersContinue();
      return message;
    },

    get(id) {
      return messages.get(id) ?? null;
    },

    setSpeechState(id, state) {
      messages.get(id)?.setSpeechState(state);
    },

    /**
     * Replace the whole transcript with a stored conversation.
     *
     * `reveal` is the explicit way in: a chat that was never read shows its
     * newest message. A chat the reader has been in is opened with the `place`
     * they left it at, so switching between chats does not push every one of
     * them to the same spot. A re-render of the chat already on screen (a
     * resync, post-processing) passes `reveal: false` and keeps the reader's
     * place - unless the stream was being followed, which still lands at the
     * bottom.
     * `loadStep(messageId, stepId)` fetches a lazy trace step of a stored message.
     * `keep` is an answer whose turn the server has not stored yet (it is
     * still running, or ended without a written answer): it trails the stored
     * messages, keeping its element, its trace and the reader's place, until
     * the snapshot of the running turn refills it. The turn's request rides
     * with it as a pair `[request, answer]` while the stored thread holds
     * neither (the turn registers before its user message is written): the
     * render must not erase the reader's own message out of the thread.
     */
    render(stored, { loadStep = null, reveal = true, place = null, keep = null } = {}) {
      // Where the reader is to end up: the place this chat was left at, else
      // the place of the chat on screen (a resync). A chat with neither - one
      // that was never read - opens at its newest message.
      const target = place ?? (reveal ? null : currentPlace());
      batching += 1;
      try {
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
            // Public typed events of the turn: narration and compact statuses.
            events: message.events ?? [],
          });
          handle.reasoning?.hydrate(message.trace, {
            loadStep: loadStep && message.id ? (stepId) => loadStep(message.id, stepId) : null,
          });
          if (message.interruption) {
            handle.setInterrupted(message.interruption, { resumable: message.interruption.resumable });
          }
        }
        for (const handle of keep ? (Array.isArray(keep) ? keep : [keep]) : []) {
          messages.set(handle.id, handle);
          container.append(handle.el);
        }
      } finally {
        batching -= 1;
      }
      offersContinue();
      // Every stored plan starts folded; the newest one is the one to follow.
      [...messages.values()].reverse().find((handle) => handle.reasoning?.hasPlan)?.reasoning.setPlanCollapsed(false);
      setEmpty(stored.length === 0 && !keep);
      // A chat left at its end opens at its end: the messages after the reader
      // are the reason they were following. Any other place is restored.
      if (target && target.following === false) restorePlace(target);
      else revealEnd();
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
      // Nothing to land on any more: a landing still in flight stops here.
      landing += 1;
      restoring = null;
      settling = 0;
      pendingReveal = false;
      lastHeight = -1;
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
      // Messages are matched by id, not by count: a different conversation, a
      // branch switch or an edited answer all keep the count and would
      // otherwise be adopted silently, leaving the wrong text on screen.
      for (let index = 0; index < handles.length; index += 1) {
        const handle = handles[index];
        const message = stored[index];
        if (handle.messageId && message.id && handle.messageId !== message.id) return false;
        if (handle.role !== message.role) return false;
        if ((handle.kind ?? null) !== (message.kind ?? null)) return false;
        // Only a bubble shows the text it was built from; a marker's is derived.
        if ((handle.role === "user" || handle.role === "assistant")
          && (handle.text ?? "") !== (message.content ?? "")) return false;
      }
      handles.forEach((handle, index) => handle.adopt({
        id: stored[index].id, versions: stored[index].versions, checkpoint: stored[index].checkpoint ?? null,
        events: stored[index].events ?? [],
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

    /** Take a message off the screen (a submit the server already honoured). */
    drop(handle) {
      if (!handle) return;
      messages.delete(handle.id);
      handle.el.remove();
      offersContinue();
    },

    /**
     * Tell the widget of a stored message what the server answered for one
     * request: its new state revision, or that the press was refused.
     */
    acknowledgeUi(messageId, uiId, patch) {
      if (!messageId || !uiId) return;
      const handle = [...messages.values()].find((message) => message.messageId === messageId);
      handle?.acknowledgeUi?.(uiId, patch);
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
    /** Put the list at its end now, and keep it there while layout settles. */
    reveal: revealEnd,
    /** Where the reader is, for a caller that keeps one place per chat. */
    place: currentPlace,
  };
}
