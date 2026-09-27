/**
 * The composer: auto-growing input, attachments, send/stop/continue, and the
 * keyboard contract.
 *
 * While the agent works, Send stays next to Stop: a message sent then is
 * delivered now, at the agent's next step or after its turn - as picked in the
 * delivery menu, or by the decision model on "Auto" (web_chat/delivery.py).
 *
 * The first Stop lets the agent finish its current step; while it does, the
 * button reads "Stop now" and a second press cancels the turn outright.
 *
 * Images come from the attach button, a paste or a drop onto the composer.
 * Each is shrunk in the browser (lib/images.js) and shown as a thumbnail that
 * can be removed; they go out with the next message, text optional.
 *
 * Continue appears while the conversation ends with a turn that stopped early
 * and can still be resumed (`store.resumable`).
 */

import { h, icon } from "../lib/dom.js";
import { MAX_IMAGES, imageToDataUrl, rejectReason } from "../lib/images.js";
import { ICONS } from "./icons.js";
import { toast } from "./toast.js";

const MAX_HEIGHT_PX = 240;

export function createComposer({
  input,
  sendButton,
  stopButton,
  deliverySelect,
  continueButton,
  attachButton,
  fileInput,
  tray,
  store,
  onSend,
  onStop,
  onContinue,
}) {
  const root = input.closest(".composer") ?? input.parentElement;
  /** @type {{id: number, url: string}[]} attached images, in order */
  let attachments = [];
  let converting = 0; // images still being read; Send waits for them
  let nextId = 0;

  const resize = () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, MAX_HEIGHT_PX)}px`;
  };

  // The label is the button's text node; the icon before it stays.
  const stopLabel = [...stopButton.childNodes].find((node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim())
    ?? stopButton.appendChild(document.createTextNode("Stop"));

  const hasContent = () => Boolean(input.value.trim()) || attachments.length > 0;

  const syncButtons = ({ streaming, stopping, resumable }) => {
    // While the agent works a message can still be sent: the chosen delivery
    // (or the decision model) says when it reaches the agent.
    sendButton.hidden = false;
    deliverySelect.hidden = !streaming;
    stopButton.hidden = !streaming;
    stopLabel.textContent = stopping ? "Stop now" : "Stop";
    stopButton.title = stopping
      ? "The agent is finishing its current step - click to stop it right now"
      : "Stop after the current step";
    sendButton.disabled = converting > 0 || !hasContent();
    continueButton.hidden = streaming || !resumable;
    attachButton.disabled = attachments.length + converting >= MAX_IMAGES;
    input.setAttribute("aria-busy", String(streaming));
  };
  const sync = () => syncButtons(store.get());

  const renderTray = () => {
    tray.replaceChildren(
      ...attachments.map(({ id, url }) =>
        h(
          "div.attachment",
          {},
          h("img.attachment__img", { src: url, alt: "Attached image" }),
          h(
            "button.attachment__remove",
            { type: "button", title: "Remove image", "aria-label": "Remove image", on: { click: () => remove(id) } },
            icon(ICONS.close, { size: 12 }),
          ),
        ),
      ),
    );
    tray.hidden = attachments.length === 0;
    sync();
  };

  const remove = (id) => {
    attachments = attachments.filter((item) => item.id !== id);
    renderTray();
  };

  /** Read, shrink and show the images among *files*; others are refused with a reason. */
  const attach = async (files) => {
    for (const file of files) {
      const reason = rejectReason(file);
      if (reason) {
        toast(reason, { tone: "error" });
        continue;
      }
      if (attachments.length + converting >= MAX_IMAGES) {
        toast(`A message can carry at most ${MAX_IMAGES} images.`, { tone: "error" });
        break;
      }
      converting += 1;
      sync();
      try {
        attachments.push({ id: (nextId += 1), url: await imageToDataUrl(file) });
      } catch (error) {
        toast(`${file.name || "The image"} could not be read.`, { tone: "error" });
      } finally {
        converting -= 1;
        renderTray();
      }
    }
  };

  const submit = () => {
    if (converting > 0 || !hasContent()) return;
    const text = input.value.trim();
    const images = attachments.map((item) => item.url);
    const delivery = store.get().streaming && deliverySelect.value !== "auto" ? deliverySelect.value : null;
    input.value = "";
    attachments = [];
    resize();
    renderTray();
    onSend(text, images, delivery);
  };

  input.addEventListener("input", () => {
    resize();
    sync();
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      submit();
    }
  });

  // A pasted screenshot is an attachment; pasted text stays text.
  input.addEventListener("paste", (event) => {
    const files = [...(event.clipboardData?.files ?? [])].filter((file) => file.type.startsWith("image/"));
    if (!files.length) return;
    event.preventDefault();
    void attach(files);
  });

  root.addEventListener("dragover", (event) => {
    if (![...(event.dataTransfer?.types ?? [])].includes("Files")) return;
    event.preventDefault();
    root.classList.add("is-dropping");
  });
  root.addEventListener("dragleave", (event) => {
    if (!root.contains(event.relatedTarget)) root.classList.remove("is-dropping");
  });
  root.addEventListener("drop", (event) => {
    root.classList.remove("is-dropping");
    const files = [...(event.dataTransfer?.files ?? [])];
    if (!files.length) return;
    event.preventDefault();
    void attach(files);
  });

  attachButton.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    const files = [...fileInput.files];
    fileInput.value = ""; // the same file can be picked again
    void attach(files);
  });

  sendButton.addEventListener("click", submit);
  stopButton.addEventListener("click", onStop);
  continueButton.addEventListener("click", () => onContinue());
  store.subscribe(syncButtons);
  renderTray();

  return {
    focus: () => input.focus(),

    /** Prefill the composer - prompt chips and "edit message" both land here. */
    setValue(value) {
      input.value = value ?? "";
      resize();
      sync();
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    },

    get value() {
      return input.value;
    },
  };
}
