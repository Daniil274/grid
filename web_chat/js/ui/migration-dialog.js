/**
 * "Reference-only copy": the owner's view of a migration dry run.
 *
 * An old chat - from another Grid version, another system, or before revision
 * pinning - cannot always be continued here. The server offers a new *empty*
 * chat pinned to this runtime that points at the old one as reference data.
 * Nothing is imported, replayed or restored: this dialog states that before the
 * owner confirms, and sending the preview's bound token is the confirmation.
 * `web_chat.server` refuses the POST without both.
 */

import { h } from "../lib/dom.js";

export const REFERENCE_SOURCE_PATH = (id) => `/?conversation=${encodeURIComponent(id)}`;

/**
 * The conversation a rail row stands for: the branch it shows (``open_id``)
 * when it is one, else its own id. The source of a reference copy is what the
 * user is reading, not the root the row belongs to.
 */
export function sourceConversationId(conversation = {}) {
  return conversation?.open_id || conversation?.id || "";
}

/** The source's name for people: its title, else its id, else "unknown". */
export function sourceLabelOf(source = {}, sourceTitle = "") {
  return String(sourceTitle || "").trim() || String(source?.context_id || "").trim() || "unknown source";
}

/** "unknown (legacy chat)" or "recorded". */
export function provenanceLabelOf(source = {}) {
  return source?.provenance === "unknown" ? "unknown (legacy chat)" : source?.provenance ? "recorded" : "unknown";
}

/**
 * The report as short labelled facts a person can read.
 *
 * @returns {{label: string, text: string}[]}
 */
export function migrationFacts(report = {}, sourceTitle = "") {
  const source = report.source ?? {};
  const reference = report.reference ?? {};
  const history = reference.history ?? {};
  const messages = Number(history.messages ?? source.messages ?? 0);
  const executions = Number(history.executions ?? source.executions ?? 0);
  const excluded = reference.excluded ?? {};
  const queues = Number(excluded.queue ?? excluded.pending_messages ?? 0);
  const pending = excluded.pending_agent_run ? 1 : 0;
  return [
    {
      label: "Source",
      text: `${sourceLabelOf(source, sourceTitle)} — provenance ${provenanceLabelOf(source)}`,
    },
    {
      label: "History",
      text: `referenced only (${messages} message(s), ${executions} execution(s) stay in the source)`,
    },
    {
      label: "Not imported",
      text: `no messages, answers, tool results, sessions, checkpoints, queues (${queues}) or pending runs (${pending})`,
    },
    {
      label: "No replay",
      text: "old instructions and tools are not carried over and nothing is replayed or re-run",
    },
    {
      label: "No workspace restore",
      text: reference?.artifacts?.workspace === "referenced-not-copied"
        ? "files stay where they are; nothing is copied or restored"
        : "no workspace files are copied or restored",
    },
  ];
}

/**
 * Open the dry-run report for owner review.
 *
 * @param {object} options
 * @param {object} options.report  the GET /migration report
 * @param {string} [options.sourceTitle] the old chat's title
 * @param {(token: string) => Promise<object>} [options.apply]
 *   POSTs the bound token and returns the applied report
 * @param {(contextId: string) => void} [options.onOpened] opens the new chat
 * @returns {HTMLDialogElement}
 */
export function openMigrationDialog({ report, sourceTitle = "", apply, onOpened }) {
  const source = report?.source ?? {};
  const sourceHref = source.context_id ? REFERENCE_SOURCE_PATH(source.context_id) : null;
  const refused = report?.status === "blocked";
  // A refusal has no "what will be imported" to promise; its reason is the report.
  const facts = refused ? migrationFacts(report, sourceTitle).slice(0, 1) : migrationFacts(report, sourceTitle);
  const status = h("p.migrationDialog__status", { role: "status" });
  const cancel = h("button.btn.btn--ghost", { type: "button", text: "Cancel" });
  const actions = h("div.migrationDialog__actions", {}, cancel);
  const title = h("h2.migrationDialog__title", {
    text: report?.status === "completed" ? "Reference-only chat already exists" : "Create a reference-only copy",
  });

  const intro = h("p.migrationDialog__intro", {
    text: refused
      ? "The dry run was refused, so nothing was created and the old chat is untouched."
      : "This old chat cannot be continued directly here. Grid can make a new empty chat that points to it "
        + "as reference data only. The old chat is kept as it is; nothing from it is imported or run.",
  });
  const factList = h("dl.migrationDialog__facts", {});
  for (const fact of facts) {
    factList.append(
      h("div.migrationDialog__fact", {}, h("dt", { text: fact.label }), h("dd", { text: fact.text })),
    );
  }
  const reason = h("p.migrationDialog__reason", { text: report?.reason ?? "" });
  const sourceLink = sourceHref
    ? h(
        "a.migrationDialog__sourceLink",
        { href: sourceHref, target: "_blank", rel: "noopener", text: "Open the old chat to read and copy from it ↗" },
      )
    : null;

  const content = h("div.migrationDialog__content", {}, title, intro, factList, sourceLink, reason, status);
  let closeDialog = () => {};
  let confirmBox = null;

  const close = () => {
    if (typeof closeDialog === "function") closeDialog();
  };
  cancel.addEventListener("click", close);

  if (report?.status === "completed" && report.target?.context_id) {
    const target = report.target.context_id;
    actions.append(h("button.btn.btn--primary", {
      type: "button",
      text: "Open reference-only chat",
      on: { click: () => { onOpened?.(target); close(); } },
    }));
  } else if (report?.status === "ready" && report.confirmation?.token) {
    confirmBox = h("input", { type: "checkbox" });
    const consent = h(
      "label.migrationDialog__consent",
      {},
      confirmBox,
      h("span", {
        text:
          "I understand this creates a new empty chat. It imports no messages, tools or instructions, "
          + "replays nothing and restores no files.",
      }),
    );
    const submit = h("button.btn.btn--primary", {
      type: "submit",
      text: "Create reference-only chat",
    });
    submit.disabled = true;
    confirmBox.addEventListener("change", () => { submit.disabled = !confirmBox.checked; });
    status.remove();
    content.append(consent, status);
    actions.append(submit);

    const form = h("form.migrationDialog__form", {}, content, actions);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!confirmBox?.checked) return;
      submit.disabled = true;
      status.dataset.tone = "";
      status.textContent = "Creating the empty reference-only chat…";
      try {
        const applied = await apply?.(report.confirmation.token);
        if (!applied || !["applied", "completed"].includes(applied.status) || !applied.target?.context_id) {
          throw Object.assign(new Error(applied?.reason ?? "The reference-only chat was not created."), {
            payload: applied,
          });
        }
        onOpened?.(applied.target.context_id);
        close();
      } catch (error) {
        const refused = error?.payload ?? {};
        status.dataset.tone = "error";
        status.textContent = refused.reason
          ?? (refused.code === "migration.confirmation_stale"
            ? "The old chat changed since the preview; nothing was created. Close and start the preview again."
            : error?.message ?? "The reference-only chat could not be created.");
        submit.disabled = !confirmBox.checked;
      }
    });

    const dialog = h("dialog.migrationDialog", { "aria-label": "Reference-only copy" }, form);
    closeDialog = () => {
      if (typeof dialog.close === "function") dialog.close();
      else dialog.remove();
    };
    dialog.addEventListener("close", () => dialog.remove());
    document.body.append(dialog);
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    cancel.focus?.();
    return dialog;
  } else {
    // Blocked: there is nothing to confirm, only a reason to read.
    status.dataset.tone = "error";
    status.textContent = report?.reason ?? "This chat cannot be used as a reference source.";
  }

  const dialog = h("dialog.migrationDialog", { "aria-label": "Reference-only copy" }, content, actions);
  closeDialog = () => {
    if (typeof dialog.close === "function") dialog.close();
    else dialog.remove();
  };
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
  cancel.focus?.();
  return dialog;
}
