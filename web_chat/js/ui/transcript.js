/**
 * The message list: owns scroll behaviour and the empty state.
 *
 * Scrolling follows the stream only while the reader is already at the bottom;
 * scroll up to read something and the view stays put, with a jump pill offering
 * the way back. That rule is why every append goes through here.
 */

import { onFrame } from "../lib/dom.js";
import { createMessage } from "./message.js";

const STICK_THRESHOLD_PX = 140;

export function createTranscript({ container, emptyState, jumpButton, onEditMessage, onSpeakMessage }) {
  /** Live message handles by id - the speech player reports state by id. */
  const messages = new Map();

  const atBottom = () =>
    container.scrollHeight - container.scrollTop - container.clientHeight < STICK_THRESHOLD_PX;

  const syncJumpButton = () => {
    const hasMessages = container.querySelector(".msg") !== null;
    jumpButton.classList.toggle("is-visible", hasMessages && !atBottom());
  };

  const scrollToBottom = (force = false) => {
    if (force || atBottom()) container.scrollTop = container.scrollHeight;
    syncJumpButton();
  };
  const followStream = onFrame(() => scrollToBottom());

  container.addEventListener("scroll", syncJumpButton, { passive: true });
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

    count() {
      return container.querySelectorAll(".msg").length;
    },

    /** Called on every streamed fragment; cheap because it coalesces per frame. */
    follow: followStream,
    scrollToBottom,
  };
}
