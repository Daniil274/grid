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

export function createTranscript({ container, emptyState, jumpButton, onEditMessage, onSpeakMessage }) {
  /** Live message handles by id - the speech player reports state by id. */
  const messages = new Map();
  const scrollFollower = createScrollFollower(container);

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

  return {
    /** @returns the message handle, so the caller can stream into it. */
    add(options) {
      setEmpty(false);
      const message = createMessage({ ...options, onEdit: onEditMessage, onSpeak: onSpeakMessage });
      messages.set(message.id, message);
      container.append(message.el);
      scrollToBottom(true);
      return message;
    },

    get(id) {
      return messages.get(id) ?? null;
    },

    setSpeechState(id, state) {
      messages.get(id)?.setSpeechState(state);
    },

    /** Replace the whole transcript with a stored conversation. */
    render(stored) {
      container.replaceChildren();
      messages.clear();
      for (const message of stored) {
        const handle = this.add({
          role: message.role,
          content: message.content,
          timestamp: message.timestamp,
        });
        handle.reasoning?.hydrate(message.trace);
      }
      setEmpty(stored.length === 0);
      scrollToBottom(true);
    },

    clear() {
      container.replaceChildren();
      messages.clear();
      setEmpty(true);
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
