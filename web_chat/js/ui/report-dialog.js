/**
 * "Report a problem with this answer": what went wrong, and the user's consent.
 *
 * Sending freezes the conversation up to this answer - with the agent's
 * steps, tool calls and settings - for the server's admins to examine
 * (web_chat/review). The dialog says so before the user sends; sending is the
 * consent. A native <dialog>: focus stays inside, Escape closes it.
 */

import { h } from "../lib/dom.js";

const MAX_NOTE = 2000;

/**
 * @param {{send: (note: string) => Promise<void>}} options
 *   send files the report; a rejection is shown in the dialog, which stays open
 * @returns {HTMLDialogElement} the open dialog; it removes itself once closed
 */
export function openReportDialog({ send }) {
  const note = h("textarea.reportDialog__note", {
    rows: 5,
    maxLength: MAX_NOTE,
    placeholder: "What went wrong? What did you expect instead?",
    "aria-label": "What went wrong",
  });
  const status = h("p.reportDialog__status", { role: "status" });
  const submit = h("button.btn.btn--primary", { type: "submit", text: "Send report" });
  const cancel = h("button.btn.btn--ghost", { type: "button", text: "Cancel" });

  const form = h(
    "form.reportDialog__form",
    { method: "dialog" },
    h("h2.reportDialog__title", { text: "Report a problem with this answer" }),
    note,
    h("p.reportDialog__consent", {
      text:
        "The admins of this server will see this conversation up to this answer, with the agent's steps, "
        + "tool calls and settings. Secrets are removed before it is stored.",
    }),
    status,
    h("div.reportDialog__actions", {}, cancel, submit),
  );
  const dialog = h("dialog.reportDialog", { "aria-label": "Report a problem" }, form);

  cancel.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => dialog.remove());
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    status.dataset.tone = "";
    status.textContent = "Sending…";
    try {
      await send(note.value.trim());
      dialog.close();
    } catch (error) {
      status.dataset.tone = "error";
      status.textContent = error?.message ?? "The report could not be sent.";
      submit.disabled = false;
    }
  });

  document.body.append(dialog);
  dialog.showModal();
  note.focus();
  return dialog;
}
