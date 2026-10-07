/**
 * "Working directory": the directory the agents of a chat work in.
 *
 * A one-user server lets every chat work in a directory of its own - with its
 * own container when isolation is on - so chats in different projects run
 * side by side without touching each other. The directory is chosen before a
 * chat's first message; for a chat already under way, a new chat opens in the
 * chosen directory. A native <dialog>: focus stays inside, Escape closes it.
 */

import { h } from "../lib/dom.js";

/**
 * @param {{current: string, started: boolean, choose: (path: string) => Promise<void>}} options
 *   current is the directory the chat works in now; started says the chat has
 *   messages already; choose sets the directory - a rejection shows in the
 *   dialog, which stays open
 * @returns {HTMLDialogElement} the open dialog; it removes itself once closed
 */
export function openWorkspaceDialog({ current, started, choose }) {
  const path = h("input.workspaceDialog__path", {
    type: "text",
    value: current,
    spellcheck: false,
    autocomplete: "off",
    placeholder: "/home/me/projects/app",
    "aria-label": "Full path of the directory",
  });
  const status = h("p.workspaceDialog__status", { role: "status" });
  const submit = h("button.btn.btn--primary", { type: "submit", text: started ? "Open a new chat there" : "Work here" });
  const cancel = h("button.btn.btn--ghost", { type: "button", text: "Cancel" });

  const form = h(
    "form.workspaceDialog__form",
    { method: "dialog" },
    h("h2.workspaceDialog__title", { text: "Working directory" }),
    h("p.workspaceDialog__hint", {
      text: started
        ? `This chat works in ${current}. A chat keeps its directory once it has messages, so the one you choose opens in a new chat.`
        : "The agents of this chat read, write and run commands in this directory. Each directory has its own container, so chats in different directories never get in each other's way.",
    }),
    path,
    status,
    h("div.workspaceDialog__actions", {}, cancel, submit),
  );
  const dialog = h("dialog.workspaceDialog", { "aria-label": "Working directory" }, form);

  cancel.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => dialog.remove());
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    status.dataset.tone = "";
    status.textContent = "Preparing…";
    try {
      await choose(path.value.trim());
      dialog.close();
    } catch (error) {
      status.dataset.tone = "error";
      status.textContent = error?.message ?? "The directory could not be chosen.";
      submit.disabled = false;
    }
  });

  document.body.append(dialog);
  dialog.showModal();
  path.focus();
  path.select();
  return dialog;
}
