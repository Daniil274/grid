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
 * Images are shrunk and sent to the model; other files are uploaded into the
 * workspace and linked in the next message. Both can be picked, pasted or
 * dropped, previewed and removed before sending, with text optional.
 *
 * Continue appears while the conversation ends with a turn that stopped early
 * and can still be resumed (`store.resumable`).
 */

import { h, icon } from "../lib/dom.js";
import { ACCEPTED_TYPES, MAX_IMAGES, imageToDataUrl, rejectReason } from "../lib/images.js";
import { fileSize, withFiles } from "../lib/files.js";
import { api } from "../net/api.js";
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
  // Pending entries reserve a place immediately, including across overlapping drops.
  let attachments = [];
  let nextId = 0;
  const uploads = () => store.get().uploads ?? { enabled: false, max_file_mb: 25, max_files: 10 };
  const count = (kind) => attachments.filter((item) => item.kind === kind).length;

  const resize = () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, MAX_HEIGHT_PX)}px`;
  };

  // The label is the button's text node; the icon before it stays.
  const stopLabel = [...stopButton.childNodes].find((node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim())
    ?? stopButton.appendChild(document.createTextNode("Stop"));

  const hasContent = () => Boolean(input.value.trim()) || attachments.length > 0;
  const pending = () => attachments.some((item) => item.pending);

  const syncButtons = ({ streaming, stopping, resumable, restartPending }) => {
    // While the agent works a message can still be sent: the chosen delivery
    // (or the decision model) says when it reaches the agent.
    sendButton.hidden = false;
    deliverySelect.hidden = !streaming;
    stopButton.hidden = !streaming;
    stopLabel.textContent = stopping ? "Stop now" : "Stop";
    stopButton.title = stopping
      ? "The agent is finishing its current step - click to stop it right now"
      : "Stop after the current step";
    sendButton.disabled = restartPending || pending() || !hasContent();
    continueButton.disabled = Boolean(restartPending);
    continueButton.hidden = streaming || !resumable;
    attachButton.disabled = count("image") >= MAX_IMAGES && (!uploads().enabled || count("file") >= uploads().max_files);
    fileInput.setAttribute("accept", uploads().enabled ? "" : ACCEPTED_TYPES.join(","));
    attachButton.title = uploads().enabled ? "Attach files (or paste / drop them)" : "Attach images (or paste / drop them)";
    attachButton.setAttribute("aria-label", uploads().enabled ? "Attach files" : "Attach images");
    input.setAttribute("aria-busy", String(streaming));
  };
  const sync = () => syncButtons(store.get());

  const renderTray = () => {
    tray.replaceChildren(
      ...attachments.map(({ id, kind, name, url, bytes, pending: loading }) =>
        h(
          kind === "image" && !loading ? "div.attachment" : "div.attachment.attachment--file",
          { "aria-busy": String(loading) },
          kind === "image" && !loading
            ? h("img.attachment__img", { src: url, alt: name })
            : [icon(ICONS.file, { size: 22 }), h("div.attachment__info", {},
              h("span.attachment__name", { text: name, title: name }),
              h("span.attachment__size", { text: loading ? "Preparing…" : fileSize(bytes) }))],
          h(
            "button.attachment__remove",
            { type: "button", title: "Remove attachment", "aria-label": `Remove ${name}`, on: { click: () => remove(id) } },
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

  /** Preview supported images; upload other files and keep the server's actual path. */
  const attach = async (files) => {
    for (const file of files) {
      const kind = ACCEPTED_TYPES.includes(file.type) ? "image" : "file";
      const limits = uploads();
      const maximum = kind === "image" ? MAX_IMAGES : limits.max_files;
      const reason = kind === "image" ? rejectReason(file)
        : !limits.enabled ? "File uploads are off on this server."
        : file.size > limits.max_file_mb * 1024 * 1024 ? `${file.name} is larger than ${limits.max_file_mb} MB.` : null;
      if (reason) {
        toast(reason, { tone: "error" });
        continue;
      }
      if (count(kind) >= maximum) {
        toast(`A message can carry at most ${maximum} ${kind === "image" ? "images" : "files"}.`, { tone: "error" });
        continue;
      }
      const item = { id: (nextId += 1), kind, name: file.name || "Image", bytes: file.size, pending: true };
      attachments.push(item);
      renderTray();
      try {
        if (kind === "image") item.url = await imageToDataUrl(file);
        else {
          const result = await api.uploadFiles([file]);
          Object.assign(item, result.files[0]);
        }
        item.pending = false;
      } catch (error) {
        attachments = attachments.filter(({ id }) => id !== item.id);
        toast(kind === "image" ? `${file.name || "The image"} could not be read.` : error.message, { tone: "error" });
      } finally {
        renderTray();
      }
    }
  };

  const submit = () => {
    if (store.get().restartPending || pending() || !hasContent()) return;
    const text = withFiles(input.value.trim(), attachments.filter((item) => item.kind === "file"));
    const images = attachments.filter((item) => item.kind === "image").map((item) => item.url);
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

  // Pasted files are attachments; a plain text paste stays text.
  input.addEventListener("paste", (event) => {
    const files = [...(event.clipboardData?.files ?? [])];
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
