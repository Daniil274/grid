/**
 * The project dialog: make a project, or rename, describe, archive and delete
 * one.
 *
 * A project is a named node over chats (web_chat/projects.py). This dialog
 * edits its title, description and archive flag; deleting one explains what
 * happens first - its sub-projects and its chats move up to its parent, so
 * nothing is lost with the node. A parent is fixed here (a sub-project is
 * created from its parent's menu), so the tree can only be built where the
 * store allows it. A native <dialog>: focus stays inside, Escape closes it.
 */

import { h } from "../lib/dom.js";

/** How deeply projects nest; the store's own limit (web_chat/projects.py). */
export const MAX_DEPTH = 3;

const plural = (count, one, many) => `${count} ${count === 1 ? one : many}`;

/**
 * @param {object} options
 * @param {"create"|"edit"} options.mode
 * @param {object|null} [options.project] the project being edited
 * @param {object|null} [options.parent] the project a new one nests under
 * @param {object[]} [options.projects] every project, for the delete's consequences
 * @param {(fields: object) => Promise<void>} options.onSubmit create gets
 *   `{title, description, parent_id}`, edit gets `{title, description, archived}`
 * @param {() => Promise<void>} [options.onDelete] deleting an existing project
 * @returns {HTMLDialogElement} the open dialog; it removes itself once closed
 */
export function openProjectDialog({
  mode,
  project = null,
  parent = null,
  projects = [],
  onSubmit,
  onDelete,
}) {
  const editing = mode === "edit";

  const title = h("input.control.projectDialog__title", {
    type: "text",
    maxlength: 120,
    required: true,
    autocomplete: "off",
    placeholder: "Project name",
    "aria-label": "Project name",
  });
  title.value = project?.title ?? "";
  const description = h("textarea.control.control--area.projectDialog__description", {
    rows: 4,
    maxlength: 4000,
    placeholder: "What this project is about (optional)",
    "aria-label": "Project description",
  });
  description.value = project?.description ?? "";
  const archived = h("input.projectDialog__archived", { type: "checkbox", "aria-label": "Archived" });
  archived.checked = Boolean(project?.archived);

  const status = h("p.projectDialog__status", { role: "status", "aria-live": "polite" });
  const submit = h("button.btn.btn--primary", { type: "submit", text: editing ? "Save" : "Create" });
  const cancel = h("button.btn.btn--ghost", { type: "button", text: "Cancel" });

  // What deleting this project does, in the store's own terms: children and
  // chats re-parent to the project's parent, never disappearing with it.
  const children = project ? projects.filter((other) => other.parent_id === project.id) : [];
  const parentTitle = project?.parent_id
    ? projects.find((other) => other.id === project.parent_id)?.title
    : null;
  const consequence = project
    ? `Deleting “${project.title}” moves its ${plural(children.length, "sub-project", "sub-projects")} ` +
      `and ${plural(project.chat_count ?? 0, "chat", "chats")} to ${parentTitle ? `“${parentTitle}”` : "the top level"}. ` +
      "This cannot be undone."
    : "";

  const deleteButton = editing && onDelete
    ? h("button.btn.btn--danger.projectDialog__delete", { type: "button", text: "Delete project" })
    : null;

  const where = parent
    ? h("p.projectDialog__parent", {}, "Inside: ", h("strong", { text: parent.title }))
    : (editing ? h("p.projectDialog__parent", { text: parentTitle ? `Inside: ${parentTitle}` : "At the top level" }) : null);

  const form = h(
    "form.projectDialog__form",
    { method: "dialog" },
    h("h2.projectDialog__heading", {
      text: editing ? "Edit project" : parent ? `New sub-project in “${parent.title}”` : "New project",
    }),
    where,
    h("label.field", {}, h("span.field__label", { text: "Name" }), title),
    h(
      "label.field",
      {},
      h("span.field__label", { text: "Description" }),
      description,
      h("span.field__hint", { text: "Markdown; shown with the project." }),
    ),
    editing
      ? h(
          "label.field.field--switch",
          {},
          archived,
          h("span.field__label", { text: "Archived" }),
          h("span.field__hint", { text: "Kept, but set aside in the rail." }),
        )
      : null,
    editing ? h("p.projectDialog__consequence", { text: consequence }) : null,
    status,
    h("div.projectDialog__actions", {}, deleteButton, cancel, submit),
  );
  const dialog = h("dialog.projectDialog", { "aria-label": editing ? "Edit project" : "New project" }, form);

  cancel.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => dialog.remove());

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const name = title.value.trim();
    if (!name) {
      status.dataset.tone = "error";
      status.textContent = "A project needs a name.";
      title.focus();
      return;
    }
    submit.disabled = true;
    status.dataset.tone = "";
    status.textContent = "Saving…";
    try {
      await onSubmit(
        editing
          ? { title: name, description: description.value.trim(), archived: archived.checked }
          : { title: name, description: description.value.trim(), parent_id: parent?.id ?? null },
      );
      dialog.close();
    } catch (error) {
      status.dataset.tone = "error";
      status.textContent = error?.message ?? "The project could not be saved.";
      submit.disabled = false;
    }
  });

  deleteButton?.addEventListener("click", async () => {
    if (!confirm(`${consequence}\n\nDelete “${project.title}”?`)) return;
    deleteButton.disabled = true;
    submit.disabled = true;
    status.dataset.tone = "";
    status.textContent = "Deleting…";
    try {
      await onDelete();
      dialog.close();
    } catch (error) {
      status.dataset.tone = "error";
      status.textContent = error?.message ?? "The project could not be deleted.";
      deleteButton.disabled = false;
      submit.disabled = false;
    }
  });

  document.body.append(dialog);
  dialog.showModal();
  title.focus();
  if (editing) title.select();
  return dialog;
}
