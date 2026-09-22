/**
 * The composer: auto-growing input, send/stop, and the keyboard contract.
 *
 * Send and stop are the same slot in the layout - only one of them is ever a
 * valid action - so the primary button stays exactly where the hand already is
 * when a turn needs interrupting.
 */

const MAX_HEIGHT_PX = 240;

export function createComposer({ input, sendButton, stopButton, store, onSend, onStop }) {
  const resize = () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, MAX_HEIGHT_PX)}px`;
  };

  const syncButtons = ({ streaming }) => {
    sendButton.hidden = streaming;
    stopButton.hidden = !streaming;
    sendButton.disabled = !input.value.trim() || streaming;
    input.setAttribute("aria-busy", String(streaming));
  };

  const submit = () => {
    const text = input.value.trim();
    if (!text || store.get().streaming) return;
    input.value = "";
    resize();
    syncButtons(store.get());
    onSend(text);
  };

  input.addEventListener("input", () => {
    resize();
    syncButtons(store.get());
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      submit();
    }
  });

  sendButton.addEventListener("click", submit);
  stopButton.addEventListener("click", onStop);
  store.subscribe(syncButtons);

  return {
    focus: () => input.focus(),

    /** Prefill the composer - prompt chips and "edit message" both land here. */
    setValue(value) {
      input.value = value ?? "";
      resize();
      syncButtons(store.get());
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    },

    get value() {
      return input.value;
    },
  };
}
