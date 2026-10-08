/**
 * The tracker page (web_chat/tasks_api.py): the user's tasks as the tracker
 * itself holds them, not only the plan a chat's turns left (that plan stays
 * in the chat, as its plan card). Two views over one list - a kanban board by
 * state and a table - plus the task's own dialog, where a task is read,
 * edited, closed, given dependencies, and where a new one is made.
 *
 * Every change goes through the API, which runs bd in the asking user's
 * workspace; nothing here touches a tracker file. A server whose bd cannot
 * run, or a workspace without a tracker, is shown as that - the page degrades
 * instead of pretending, and offers to start a tracker when there is none.
 *
 * The task states are the plan card's (web_chat/js/ui/plan-card.js), so a
 * task reads the same in the chat and here.
 */

import { h, icon, replace } from "../lib/dom.js";
import { api } from "../net/api.js";
import { toast } from "../ui/toast.js";
import { createThemeToggle } from "../ui/theme.js";
import { ICONS } from "../ui/icons.js";

/** How each state reads, in the order the board lists them. */
export const STATES = {
  closed: { label: "Closed", mark: "✓" },
  in_progress: { label: "In progress", mark: "●" },
  ready: { label: "Ready", mark: "○" },
  waiting: { label: "Waiting", mark: "◌" },
  blocked: { label: "Blocked", mark: "!" },
};

export const BOARD_ORDER = ["ready", "waiting", "in_progress", "blocked", "closed"];

/** bd's own statuses; a task closes with its reason, never as a quiet change. */
export const STATUSES = ["open", "in_progress", "blocked", "deferred"];
export const TYPES = ["task", "bug", "feature", "epic", "chore", "decision", "spike", "story", "milestone"];
export const PRIORITIES = [0, 1, 2, 3, 4];

const priorityTag = (priority) =>
  priority == null ? "" : `P${priority}`;

/** P0 first, a task without a priority last, then the freshest change. */
export function sortTasks(tasks) {
  return [...tasks].sort(
    (a, b) =>
      (a.priority ?? 9) - (b.priority ?? 9) ||
      String(b.updated_at ?? "").localeCompare(String(a.updated_at ?? "")),
  );
}

/** Whether a task passes the page's text, type and priority filters. */
export function matchesFilters(task, { search = "", type = "", priority = "" } = {}) {
  if (type && (task.type ?? "") !== type) return false;
  if (priority !== "" && priority != null && String(task.priority ?? "") !== String(priority)) return false;
  const needle = String(search ?? "").trim().toLowerCase();
  if (!needle) return true;
  return [task.title, task.id, task.notes, task.description].some((text) =>
    String(text ?? "").toLowerCase().includes(needle),
  );
}

const when = (stamp) => (stamp ? String(stamp).replace("T", " ").slice(0, 16) : "—");

/**
 * A jump from a task back to a chat of its project - the chat whose turns
 * planned it (`/?conversation=<id>` opens it in the chat UI).
 */
export function chatChip(chatId, { title = "" } = {}) {
  return h(
    "a.tasksChip",
    { href: `/?conversation=${encodeURIComponent(chatId)}`, title: title || `Open chat ${chatId}` },
    icon(ICONS.message, { size: 12 }),
    h("span", { text: title || chatId }),
  );
}

/** The task's card on the board: everything at a glance, and a way to open it. */
function taskCard(task, onOpen) {
  const state = STATES[task.state] ?? STATES.ready;
  return h(
    "li",
    {},
    h(
      "button.tasksCard",
      {
        type: "button",
        dataset: { state: task.state },
        "aria-haspopup": "dialog",
        on: { click: () => onOpen(task) },
      },
      h("span.tasksCard__title", { text: task.title }),
      h(
        "span.tasksCard__meta",
        {},
        h("span.tasksTag", { dataset: { kind: "state" }, text: `${state.mark} ${state.label}` }),
        task.type ? h("span.tasksTag", { text: task.type }) : null,
        task.priority != null ? h("span.tasksTag", { dataset: { kind: "priority", priority: String(task.priority) }, text: priorityTag(task.priority) }) : null,
        task.waits_for?.length ? h("span.tasksTag", { dataset: { kind: "waits" }, text: `waits for ${task.waits_for.length}` }) : null,
        h("span.tasksTag", { text: task.id }),
        task.source ? h("span.tasksTag", { dataset: { kind: "source", source: task.source }, text: task.source }) : null,
      ),
    ),
    (task.chat_ids ?? []).length
      ? h("div.tasksCard__chats", {}, (task.chat_ids ?? []).map((chatId) => chatChip(chatId)))
      : null,
  );
}

/** One row of the list view; a click opens the same dialog the card does. */
function taskRow(task, onOpen) {
  const state = STATES[task.state] ?? STATES.ready;
  return h(
    "tr",
    {},
    h("td", {}, h("span", { text: `${state.mark} ${state.label}`, title: state.label })),
    h(
      "td",
      {},
      h(
        "button.tasksRow",
        { type: "button", on: { click: () => onOpen(task) } },
        h("span.tasksRow__title", { text: task.title }),
        h("span.tasksRow__id", { text: task.id }),
      ),
    ),
    h("td", {}, task.type ?? "—"),
    h("td", {}, priorityTag(task.priority) || "—"),
    h("td", {}, task.assignee ?? "—"),
    h("td", {}, task.depends_on?.length ? task.depends_on.join(", ") : "—"),
    h("td", {}, when(task.updated_at)),
  );
}

/**
 * The page. `nodes` are the shell's elements (web_chat/tasks.html); tests
 * pass their own, the same way the account drawer's tests do.
 */
export class TasksPage {
  /** @param {object} nodes status, statusFilter, metrics, board, listWrap, listRows, search, type, priority, viewBoard, viewList, newTask, refresh */
  constructor(nodes) {
    this.nodes = nodes;
    this.tasks = [];
    this.available = null;
    this.reason = "";
    // The chats of the project in view (id → title), empty in the global mode.
    this.chats = {};
    // The project whose aggregated plan is shown, or null for the whole tracker.
    this.project = null;
    this.view = "board";
    this.dialog = this._dialog();
    nodes.board?.after?.(this.dialog.el); // the page shell; tests place it themselves

    const rerender = () => this.render();
    nodes.search?.addEventListener?.("input", rerender);
    nodes.type?.addEventListener?.("change", rerender);
    nodes.priority?.addEventListener?.("change", rerender);
    nodes.statusFilter?.addEventListener?.("change", () => void this.load());
    nodes.project?.addEventListener?.("change", () => void this.load());
    nodes.viewBoard?.addEventListener?.("click", () => this.setView("board"));
    nodes.viewList?.addEventListener?.("click", () => this.setView("list"));
    nodes.newTask?.addEventListener?.("click", () => this.dialog.openCreate(this.tasks));
    nodes.refresh?.addEventListener?.("click", () => void this.load());
  }

  setView(view) {
    this.view = view;
    this.nodes.viewBoard?.setAttribute?.("aria-selected", String(view === "board"));
    this.nodes.viewList?.setAttribute?.("aria-selected", String(view === "list"));
    if (this.nodes.listWrap) this.nodes.listWrap.hidden = view !== "list";
    if (this.nodes.board) this.nodes.board.hidden = view !== "board";
    this.render();
  }

  filters() {
    return {
      search: this.nodes.search?.value ?? "",
      type: this.nodes.type?.value ?? "",
      priority: this.nodes.priority?.value ?? "",
    };
  }

  shown() {
    return sortTasks(this.tasks.filter((task) => matchesFilters(task, this.filters())));
  }

  async load() {
    const projectId = this.nodes.project?.value || "";
    this.project = projectId || null;
    try {
      if (projectId) {
        // The project's aggregated plan: the tasks its chats planned, merged -
        // bd's statuses win, a task only a chat's trace knows is marked "trace".
        const report = await api.projectPlan(projectId);
        this.available = true;
        this.reason = "";
        this.tasks = report.tasks ?? [];
        this.chats = report.chats ?? {};
        this._syncUrl(projectId);
        this.render();
        return;
      }
      this.chats = {};
      this._syncUrl("");
      const report = await api.tasks(this.nodes.statusFilter?.value || "");
      this.available = Boolean(report.available);
      this.reason = report.reason || "";
      this.tasks = report.tasks ?? [];
      if (!this.available) this._unavailable();
      else this.render();
    } catch (error) {
      this._failed(error.message);
    }
  }

  /** Reflect the chosen project in the address bar (/tasks?project=<id>). */
  _syncUrl(projectId) {
    if (typeof history === "undefined" || !history.replaceState) return;
    history.replaceState(null, "", projectId ? `/tasks?project=${encodeURIComponent(projectId)}` : "/tasks");
  }

  /** Fill the project filter from the projects API; ?project=<id> picks one. */
  async loadProjects() {
    const select = this.nodes.project;
    if (!select) return;
    try {
      const { projects } = await api.projects();
      replace(
        select,
        h("option", { value: "", text: "All projects" }),
        ...(projects ?? []).map((project) => h("option", { value: project.id, text: project.title })),
      );
      if (typeof location !== "undefined") {
        const wanted = new URLSearchParams(location.search).get("project");
        if (wanted && (projects ?? []).some((project) => project.id === wanted)) select.value = wanted;
      }
    } catch (error) {
      console.warn("Could not list projects", error);
    }
  }

  /** The dialog needs to know the project's chats when a chat chip is shown. */
  chatTitle(chatId) {
    return this.chats?.[chatId]?.title ?? this.chats?.[chatId] ?? "";
  }

  _status(text, { error = false } = {}) {
    const node = this.nodes.status;
    if (!node) return;
    node.dataset.error = String(error);
    replace(node, h("span", { text }));
  }

  _unavailable() {
    const node = this.nodes.status;
    if (!node) return;
    node.dataset.error = "false";
    const init = h("button.btn.btn--ghost", { type: "button", text: "Initialize tracker" });
    init.addEventListener("click", async () => {
      init.disabled = true;
      try {
        await api.initTracker();
        toast("Tracker initialized", { tone: "success" });
        await this.load();
      } catch (error) {
        toast(error.message, { tone: "error" });
        init.disabled = false;
      }
    });
    replace(node, h("span", { text: `${this.reason} ` }), init);
    this.tasks = [];
    this.render();
  }

  _failed(message) {
    this._status(message, { error: true });
  }

  render() {
    if (this.available === false) return;
    const counts = Object.fromEntries(BOARD_ORDER.map((state) => [state, 0]));
    for (const task of this.tasks) counts[task.state] = (counts[task.state] ?? 0) + 1;
    if (this.nodes.metrics) {
      replace(
        this.nodes.metrics,
        BOARD_ORDER.map((state) =>
          h(
            "div.tasksMetric",
            { dataset: { state } },
            h("span", { text: STATES[state].label }),
            h("strong", { text: String(counts[state] ?? 0) }),
            h("small", { text: `${this.tasks.length} task${this.tasks.length === 1 ? "" : "s"} in the tracker` }),
          ),
        ),
      );
    }
    const shown = this.shown();
    this._status(shown.length
      ? `${shown.length} of ${this.tasks.length} tasks shown`
      : this.tasks.length
        ? "No task passes these filters."
        : "No tasks in this tracker yet — make the first one.");
    if (this.nodes.board) {
      replace(
        this.nodes.board,
        BOARD_ORDER.map((state) => {
          const column = shown.filter((task) => task.state === state);
          return h(
            "section.tasksColumn",
            { dataset: { state } },
            h("div.tasksColumnHead", {}, h("h2", { text: STATES[state].label }), h("span", { text: String(column.length) })),
            h("ul", {}, column.map((task) => taskCard(task, (chosen) => this.dialog.openTask(chosen, this.tasks)))),
          );
        }),
      );
    }
    if (this.nodes.listRows) {
      replace(
        this.nodes.listRows,
        shown.length
          ? shown.map((task) => taskRow(task, (chosen) => this.dialog.openTask(chosen, this.tasks)))
          : [h("tr", {}, h("td", { colspan: 7 }, h("p.tasksEmpty", { text: "No tasks." })))],
      );
    }
  }

  // -- the task dialog ---------------------------------------------------------

  _dialog() {
    const stateBadge = h("span.taskDialog__state");
    const idBadge = h("span.taskDialog__id");
    const closeButton = h(
      "button.taskDialog__close",
      { type: "button", "aria-label": "Close task details" },
      icon(ICONS.close, { size: 15 }),
    );
    const title = h("h2.taskDialog__title");
    const titleInput = h("input", { type: "text", "aria-label": "Title", required: "" });
    const statusSelect = h("select", { "aria-label": "Status" }, STATUSES.map((value) => h("option", { value, text: value })));
    const prioritySelect = h("select", { "aria-label": "Priority" },
      [h("option", { value: "", text: "—" }), ...PRIORITIES.map((value) => h("option", { value: String(value), text: `P${value}` }))]);
    const typeSelect = h("select", { "aria-label": "Type" }, [h("option", { value: "", text: "—" }), ...TYPES.map((value) => h("option", { value, text: value }))]);
    const assigneeInput = h("input", { type: "text", "aria-label": "Assignee", placeholder: "unassigned" });
    const description = h("textarea", { "aria-label": "Description", placeholder: "What the task asks for" });
    const acceptance = h("textarea", { "aria-label": "Acceptance criteria", placeholder: "How to tell it is done" });
    const notes = h("textarea", { "aria-label": "Notes", placeholder: "Notes replace what the tracker held" });
    const deps = h("ul.taskDialog__deps");
    const depSelect = h("select", { "aria-label": "Depends on" });
    const depAdd = h("button.btn.btn--ghost", { type: "button", text: "Depends on" });
    const reasonInput = h("input", { type: "text", "aria-label": "Close reason", placeholder: "Completed" });
    const closeTask = h("button.btn.btn--danger", { type: "button", text: "Close task" });
    const cancel = h("button.btn.btn--ghost", { type: "button", text: "Cancel" });
    const save = h("button.btn.btn--primary", { type: "submit", text: "Save" });
    // The chats of the project whose turns planned this task; empty in the global mode.
    const sourceList = h("div.taskDialog__sources");
    const readOnlyNote = h("p.taskDialog__missing", {
      text: "This task came from a chat's plan (trace), not from the tracker; it is read-only here.",
    });
    readOnlyNote.hidden = true;
    const el = h(
      "dialog.taskDialog",
      { "aria-labelledby": "task-dialog-title" },
      h("header.taskDialog__head", {}, h("span.taskDialog__badges", {}, stateBadge, idBadge), closeButton),
      title,
      h(
        "form.taskDialog__form",
        {},
        h("div.taskDialog__field", {}, h("span", { text: "Title" }), titleInput),
        h("div.taskDialog__grid", {},
          h("div.taskDialog__field", {}, h("span", { text: "Status" }), statusSelect),
          h("div.taskDialog__field", {}, h("span", { text: "Priority" }), prioritySelect),
          h("div.taskDialog__field", {}, h("span", { text: "Type" }), typeSelect)),
        h("div.taskDialog__field", {}, h("span", { text: "Assignee" }), assigneeInput),
        h("div.taskDialog__field", {}, h("span", { text: "Description" }), description),
        h("div.taskDialog__field", {}, h("span", { text: "Acceptance criteria" }), acceptance),
        h("div.taskDialog__field", {}, h("span", { text: "Notes" }), notes),
        h("div.taskDialog__field", {}, h("span", { text: "Source chats" }), sourceList),
        readOnlyNote,
        h("div.taskDialog__field", {}, h("span", { text: "Depends on" }), deps,
          h("div.taskDialog__add", {}, depSelect, depAdd)),
        h("footer.taskDialog__foot", {},
          h("div.taskDialog__footActions", {}, closeTask),
          h("div.taskDialog__reason", {}, reasonInput),
          h("div.taskDialog__footActions", {}, cancel, save)),
      ),
    );

    const page = this;
    let task = null; // the task the dialog shows; null while it is closed
    let mode = "edit"; // "edit" | "create"
    let pendingDeps = []; // dependencies chosen for a task not made yet

    const dismiss = () => {
      task = null;
      el.close?.();
    };
    closeButton.addEventListener("click", dismiss);
    cancel.addEventListener("click", dismiss);
    el.addEventListener?.("close", () => {
      task = null;
    });

    const fill = (chosen, tasks, creating = false) => {
      mode = creating ? "create" : "edit";
      task = chosen;
      pendingDeps = [];
      const state = creating ? null : STATES[chosen.state] ?? STATES.ready;
      replace(stateBadge, creating ? null : h("span", { text: `${state.mark} ${state.label}` }));
      idBadge.textContent = creating ? "new task" : chosen.id ?? "";
      title.textContent = creating ? "New task" : chosen.title;
      titleInput.value = creating ? "" : chosen.title ?? "";
      statusSelect.value = creating ? "open" : (STATUSES.includes(chosen.status) ? chosen.status : "open");
      prioritySelect.value = creating ? "2" : chosen.priority == null ? "" : String(chosen.priority);
      typeSelect.value = creating ? "task" : chosen.type ?? "";
      assigneeInput.value = creating ? "" : chosen.assignee ?? "";
      description.value = creating ? "" : chosen.description ?? "";
      acceptance.value = creating ? "" : chosen.acceptance ?? "";
      notes.value = creating ? "" : chosen.notes ?? "";
      reasonInput.value = "";
      closeTask.hidden = creating;
      // A task only a chat's trace knows is read-only here: the tracker holds
      // no record of it to change (the plan card in the chat still shows it).
      const traceOnly = !creating && (chosen.source === "trace" || chosen.readonly === true);
      save.hidden = traceOnly;
      readOnlyNote.hidden = !traceOnly;
      if (creating) {
        replace(sourceList);
      } else {
        const chatIds = chosen.chat_ids ?? [];
        replace(
          sourceList,
          chatIds.length
            ? chatIds.map((chatId) => chatChip(chatId, { title: page.chatTitle(chatId) }))
            : h("p.taskDialog__missing", { text: "No chat recorded this task." }),
        );
      }
      renderDeps(tasks);
    };

    const renderDeps = (tasks) => {
      const dependsOn = mode === "create" ? pendingDeps : task?.depends_on ?? [];
      const known = (id) => tasks.find((other) => other.id === id);
      replace(
        deps,
        dependsOn.length
          ? dependsOn.map((dep) =>
              h(
                "li.taskDialog__dep",
                {},
                h("span", { text: known(dep) ? known(dep).title : dep }),
                h("span", {}, h("span", { text: dep })),
                h("button", {
                  type: "button",
                  text: "remove",
                  on: {
                    click: async () => {
                      if (mode === "create") {
                        pendingDeps = pendingDeps.filter((dep_id) => dep_id !== dep);
                        renderDeps(tasks);
                        return;
                      }
                      try {
                        await api.taskDep(task.id, "remove", dep);
                        toast(`No longer waits for ${known(dep) ? known(dep).title : dep}`);
                        await page.load();
                        fill(page.tasks.find((fresh) => fresh.id === task.id) ?? task, page.tasks);
                      } catch (error) {
                        toast(error.message, { tone: "error" });
                      }
                    },
                  },
                }),
              ),
            )
          : [h("p.taskDialog__missing", { text: "No dependencies." })],
      );
      replace(
        depSelect,
        h("option", { value: "", text: "Add a dependency…" }),
        sortTasks(tasks.filter((other) => other.id !== task?.id && other.state !== "closed"))
          .map((other) => h("option", { value: other.id, text: `${other.title} · ${other.id}` })),
      );
    };

    depAdd.addEventListener("click", async () => {
      const parentId = depSelect.value;
      if (!parentId || (mode !== "create" && !task?.id)) return;
      if (mode === "create") {
        if (!pendingDeps.includes(parentId)) pendingDeps.push(parentId);
        renderDeps(this.tasks);
        return;
      }
      try {
        await api.taskDep(task.id, "add", parentId);
        toast("Dependency added");
        await page.load();
        fill(page.tasks.find((fresh) => fresh.id === task.id) ?? task, page.tasks);
      } catch (error) {
        toast(error.message, { tone: "error" });
      }
    });

    closeTask.addEventListener("click", async () => {
      if (!task) return;
      try {
        await api.closeTask(task.id, reasonInput.value.trim() || "Completed");
        toast("Task closed", { tone: "success" });
        await page.load();
        dismiss();
      } catch (error) {
        toast(error.message, { tone: "error" });
      }
    });

    el.querySelector("form").addEventListener("submit", async (event) => {
      event.preventDefault?.();
      const fields = {
        title: titleInput.value.trim(),
        status: statusSelect.value,
        priority: prioritySelect.value === "" ? null : Number(prioritySelect.value),
        type: typeSelect.value || null,
        assignee: assigneeInput.value.trim() || null,
        description: description.value,
        acceptance: acceptance.value,
        notes: notes.value,
      };
      try {
        if (mode === "create") {
          await api.createTask({ ...fields, depends_on: pendingDeps });
          toast("Task created", { tone: "success" });
        } else if (task) {
          const patch = {};
          for (const [key, value] of Object.entries(fields)) {
            if (String(value ?? "") !== String(task[key] ?? "")) patch[key] = value;
          }
          // The tracker replaces notes only when the dialog brought new ones.
          if (fields.notes === (task.notes ?? "")) delete patch.notes;
          if (Object.keys(patch).length) await api.updateTask(task.id, patch);
        }
        await page.load();
        dismiss();
      } catch (error) {
        toast(error.message, { tone: "error" });
      }
    });

    return {
      el,
      /** Show a task to read or change it. */
      openTask: (chosen, tasks) => {
        fill(chosen, tasks);
        el.dataset.open = "true";
        el.showModal?.();
        titleInput.focus?.();
      },
      /** A new task, from the same fields. */
      openCreate: (tasks) => {
        fill({}, tasks, true);
        el.dataset.open = "true";
        el.showModal?.();
        titleInput.focus?.();
      },
      /** The task the dialog is showing, if any (tests and the page's reloads). */
      showing: () => task,
    };
  }
}

// -- the page itself ---------------------------------------------------------------

const shell = typeof document !== "undefined" ? document.getElementById?.("tasks-page-root") : null;
if (shell) {
  const $ = (id) => document.getElementById(id);
  createThemeToggle($("theme"));
  const page = new TasksPage({
    status: $("status"),
    metrics: $("metrics"),
    board: $("board"),
    listWrap: $("list"),
    listRows: $("list-rows"),
    search: $("search"),
    type: $("type"),
    priority: $("priority"),
    statusFilter: null,
    project: $("project-filter"),
    viewBoard: $("view-board"),
    viewList: $("view-list"),
    newTask: $("new-task"),
    refresh: $("refresh"),
  });
  // The type filter lists what the tracker's types are.
  replace($("type"), h("option", { value: "", text: "All types" }), TYPES.map((value) => h("option", { value, text: value })));
  $("filters").addEventListener("submit", (event) => {
    event.preventDefault();
    void page.load();
  });
  // Projects first, so a ?project=<id> deep link picks its plan from the start.
  void page.loadProjects().then(() => page.load());
  setInterval(() => {
    if ($("live")?.checked && !document.hidden) void page.load();
  }, 30000);
}
