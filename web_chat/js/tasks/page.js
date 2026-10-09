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
import { createTrackerTheme } from "./theme.js";
import { ICONS } from "../ui/icons.js";
import { renderMarkdown } from "../lib/markdown.js";

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

/**
 * Quick filter presets, each a named combination of the page's own controls.
 * Selecting one writes the same URL state a hand-made filter would, so a
 * preset is shareable and restored by Back like any other change.
 */
export const PRESETS = [
  { key: "all", label: "All tasks" },
  { key: "p0", label: "P0", priority: "0" },
  { key: "open", label: "Open", status: "open" },
  { key: "in_progress", label: "In progress", status: "in_progress" },
  { key: "blocked", label: "Blocked", status: "blocked" },
  { key: "closed", label: "Closed", status: "closed" },
];

/** The URL's view/status/type/priority/q/task/project/conversation, read back exactly as they were written. */
export function readUrlState(search) {
  const params = new URLSearchParams(search ?? "");
  return {
    project: params.get("project") ?? "",
    status: params.get("status") ?? "",
    type: params.get("type") ?? "",
    priority: params.get("priority") ?? "",
    q: params.get("q") ?? "",
    view: params.get("view") === "list" ? "list" : "board",
    task: params.get("task") ?? "",
    conversation: params.get("conversation") ?? "",
  };
}

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
export function matchesFilters(task, { search = "", type = "", priority = "", status = "" } = {}) {
  if (status && task.status !== status) return false;
  if (type && (task.type ?? "") !== type) return false;
  if (priority !== "" && priority != null && String(task.priority ?? "") !== String(priority)) return false;
  const needle = String(search ?? "").trim().toLowerCase();
  if (!needle) return true;
  // The long-form fields count too: a task is found by what it asks for
  // (acceptance) and by why it was closed (close_reason), not only its title.
  return [task.title, task.id, task.notes, task.description, task.assignee, task.acceptance, task.close_reason].some((text) =>
    String(text ?? "").toLowerCase().includes(needle),
  );
}

const when = (stamp) => (stamp ? String(stamp).replace("T", " ").slice(0, 16) : "—");

/**
 * The chain from *parentId* down to *childId* when the parent already waits for
 * the child, through the loaded tasks: adding child → parent would close a
 * ring. Returns ``[parentId, ..., childId]``, or null when the edge is safe.
 */
export function dependencyRing(tasks, childId, parentId) {
  if (!childId || !parentId) return null;
  if (childId === parentId) return [childId];
  const deps = new Map((tasks ?? []).map((task) => [task.id, task.depends_on ?? []]));
  const previous = new Map();
  const seen = new Set([parentId]);
  const queue = [parentId];
  while (queue.length) {
    const at = queue.shift();
    for (const dep of deps.get(at) ?? []) {
      if (seen.has(dep)) continue;
      seen.add(dep);
      previous.set(dep, at);
      if (dep === childId) {
        const path = [childId];
        while (previous.has(path.at(-1))) path.push(previous.get(path.at(-1)));
        return path.reverse();
      }
      queue.push(dep);
    }
  }
  return null;
}

/** Why an edge is refused, naming the would-be ring in the tasks' own titles. */
export function ringReason(tasks, childId, parentId, ring) {
  const title = (id) => (tasks ?? []).find((task) => task.id === id)?.title ?? id;
  const chain = [childId, ...ring].map(title).join(" → ");
  return `Cannot add this dependency: it would close a cycle (${chain}). Remove one of the links first.`;
}

/**
 * A note as the tracker keeps it: what the dialog brought is *added* to what
 * was recorded, never swapped for it, so nothing a task held is lost.
 */
export function notesWithAddition(existing, addition) {
  const add = String(addition ?? "").trim();
  if (!add) return existing ?? "";
  const base = String(existing ?? "").trim();
  return base ? `${base}\n\n${add}` : add;
}

/**
 * A jump from a task back to a chat of its project - the chat whose turns
 * planned it (`/?conversation=<id>` opens it in the chat UI). When *task* is
 * given the link also positions the chat's plan card on that task, so the
 * reader lands where they left off rather than at the top of the plan.
 */
export function chatChip(chatId, { title = "", task = "" } = {}) {
  const query = `conversation=${encodeURIComponent(chatId)}${task ? `&task=${encodeURIComponent(task)}` : ""}`;
  return h(
    "a.tasksChip",
    { href: `/?${query}`, title: title || `Open chat ${chatId}` },
    icon(ICONS.message, { size: 12 }),
    h("span", { text: title || chatId }),
  );
}

/**
 * The task's card on the board: everything at a glance, a way to open it, and
 * the quick actions that need no dialog - take it, close it, reopen it.
 */
function taskCard(task, onOpen, onQuick, selected = false) {
  const closed = task.status === "closed" || task.state === "closed";
  const readonly = task.source === "trace" || task.readonly === true;
  const quick = (action, label) =>
    h("button.tasksCard__action", {
      type: "button",
      dataset: { action },
      "aria-label": `${label} ${task.title}`,
      text: label,
      on: { click: () => onQuick(action, task) },
    });
  return h(
    "li.tasksCardItem",
    { dataset: { state: task.state, selected: String(selected) }, "data-task": task.id },
    h(
      "button.tasksCard",
      {
        type: "button",
        dataset: { state: task.state, selected: String(selected) },
        "aria-haspopup": "dialog",
        on: { click: () => onOpen(task) },
      },
      h("span.tasksCard__title", { text: task.title }),
      h(
        "span.tasksCard__meta",
        {},
        task.type ? h("span.tasksTag", { text: task.type }) : null,
        task.priority != null ? h("span.tasksTag", { dataset: { kind: "priority", priority: String(task.priority) }, text: priorityTag(task.priority) }) : null,
        task.waits_for?.length ? h("span.tasksTag", { dataset: { kind: "waits" }, text: `blocked by ${task.waits_for.length}` }) : null,
        h("span.tasksTag", { text: task.id }),
        task.source ? h("span.tasksTag", { dataset: { kind: "source", source: task.source }, text: task.source }) : null,
      ),
    ),
    (task.waits_for ?? []).length
      ? h(
          "div.tasksCard__blocked",
          { title: `Waiting for: ${task.waits_for.join(", ")}` },
          h("span.tasksCard__blockedLabel", { text: "blocked by" }),
          ...task.waits_for.map((name) => h("span.tasksTag", { dataset: { kind: "blocked" }, text: name })),
        )
      : null,
    readonly
      ? null
      : h(
          "div.tasksCard__actions",
          {},
          quick("take", "Take"),
          closed ? quick("reopen", "Reopen") : quick("close", "Close"),
        ),
    (task.chat_ids ?? []).length
      ? h("div.tasksCard__chats", {}, (task.chat_ids ?? []).map((chatId) => chatChip(chatId)))
      : null,
  );
}

/** One row of the list view; a click opens the same dialog the card does. */
function taskRow(task, onOpen, selected = false) {
  const state = STATES[task.state] ?? STATES.ready;
  return h(
    "tr",
    { dataset: { selected: String(selected), task: task.id } },
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
  /** @param {object} nodes status, presets, activeFilters, undo, metrics, board, listWrap, listRows, search, type, priority, statusFilter, project, viewBoard, viewList, newTask, refresh */
  constructor(nodes) {
    this.nodes = nodes;
    this.tasks = [];
    this.available = null;
    this.reason = "";
    // The chats of the project in view (id → title), empty in the global mode.
    this.chats = {};
    // The project whose aggregated plan is shown, or null for the whole tracker.
    this.project = null;
    // The chat whose plan is shown (`?conversation=<id>`), or "" for the tracker.
    this._conversation = "";
    this.view = "board";
    // URL state: a task id waiting to be opened once the list is loaded, the
    // last action that can still be undone, and the signed-in user (lazily).
    this._pendingTask = "";
    this._undo = null;
    this._me = null;
    this._applying = false;
    // Keyboard-first speed: the task the cursor is on (j/k), the open command
    // palette (Ctrl/⌘ K) and the shortcuts sheet (?).
    this.selectedId = "";
    this.dialog = this._dialog();
    nodes.board?.after?.(this.dialog.el); // the page shell; tests place it themselves
    this.palette = this._palette();
    nodes.board?.after?.(this.palette.el);
    this.cheatsheet = this._cheatsheet();
    nodes.board?.after?.(this.cheatsheet.el);

    const rerender = () => { this._updateUrl(); this.render(); };
    nodes.search?.addEventListener?.("input", rerender);
    nodes.type?.addEventListener?.("change", rerender);
    nodes.priority?.addEventListener?.("change", rerender);
    // A status change and a view switch are history entries of their own, so
    // Back returns through the filters the user moved through.
    nodes.statusFilter?.addEventListener?.("change", () => { this._updateUrl({ push: true }); void this.load(); });
    nodes.project?.addEventListener?.("change", () => void this.load());
    nodes.viewBoard?.addEventListener?.("click", () => this.setView("board", { push: true }));
    nodes.viewList?.addEventListener?.("click", () => this.setView("list", { push: true }));
    nodes.newTask?.addEventListener?.("click", () => this.dialog.openCreate(this.tasks));
    nodes.refresh?.addEventListener?.("click", () => void this.load());
    this._bindUrl();
    this._bindKeyboard();
  }

  setView(view, { push = false } = {}) {
    this.view = view;
    this.nodes.viewBoard?.setAttribute?.("aria-pressed", String(view === "board"));
    this.nodes.viewList?.setAttribute?.("aria-pressed", String(view === "list"));
    if (this.nodes.listWrap) this.nodes.listWrap.hidden = view !== "list";
    if (this.nodes.board) this.nodes.board.hidden = view !== "board";
    this._updateUrl({ push });
    this.render();
  }

  // -- URL state ---------------------------------------------------------------

  /**
   * The URL of what is on screen now: the project, the status/type/priority
   * filters, the search text, a non-default view and the open task. Only what
   * differs from the defaults is written, so `/tasks` and `/tasks?project=p1`
   * stay clean while a preset like «P0» becomes a real history entry.
   */
  _urlState() {
    const params = new URLSearchParams();
    const project = this.nodes.project?.value || "";
    if (project) params.set("project", project);
    const status = this.nodes.statusFilter?.value || "";
    if (status) params.set("status", status);
    const type = this.nodes.type?.value || "";
    if (type) params.set("type", type);
    const priority = this.nodes.priority?.value ?? "";
    if (priority !== "") params.set("priority", priority);
    const q = String(this.nodes.search?.value ?? "").trim();
    if (q) params.set("q", q);
    if (this.view !== "board") params.set("view", this.view);
    if (this._conversation) params.set("conversation", this._conversation);
    const taskId = this.dialog?.showing?.()?.id;
    if (taskId) params.set("task", taskId);
    return params;
  }

  _updateUrl({ push = false } = {}) {
    if (this._applying || typeof history === "undefined") return;
    const query = this._urlState().toString();
    const url = query ? `/tasks?${query}` : "/tasks";
    const current = typeof location !== "undefined" ? `${location.pathname ?? ""}${location.search ?? ""}` : null;
    if (push && history.pushState && current !== null && current !== url) {
      history.pushState(null, "", url);
      return;
    }
    if (history.replaceState) history.replaceState(null, "", url);
  }

  /** Read ?view/status/q/task on entry, and follow Back/Forward from then on. */
  _bindUrl() {
    if (typeof location !== "undefined") this._applyUrl(readUrlState(location.search));
    if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
      window.addEventListener("popstate", () => {
        if (typeof location === "undefined") return;
        this._onHistoryChange(readUrlState(location.search));
      });
    }
  }

  /** Put the controls where the URL says they are, without rewriting the URL. */
  _applyUrl(state) {
    this._applying = true;
    try {
      this._conversation = state.conversation || "";
      if (this.nodes.statusFilter) this.nodes.statusFilter.value = state.status;
      if (this.nodes.type) this.nodes.type.value = state.type ?? "";
      if (this.nodes.priority) this.nodes.priority.value = state.priority ?? "";
      if (this.nodes.search) this.nodes.search.value = state.q;
      if (this.nodes.project && state.project) this.nodes.project.value = state.project;
      this.setView(state.view);
      this._pendingTask = state.task || "";
    } finally {
      this._applying = false;
    }
  }

  /** A Back/Forward step: restore the filters it names, open or close the task. */
  _onHistoryChange(state) {
    this._applyUrl({ ...state, task: "" }); // setView already re-renders
    const open = this.dialog?.showing?.();
    if (state.task && open?.id !== state.task) {
      void this.openTaskById(state.task);
      return;
    }
    if (!state.task && open) {
      this.dialog.el.close?.();
      this.render();
      return;
    }
    void this.load();
  }

  /** Open a task from a link: from the loaded list, else fetched by its id. */
  async openTaskById(id) {
    if (!id) return;
    let task = this.tasks.find((candidate) => candidate.id === id || candidate.key === id);
    if (!task) {
      try {
        task = await api.task(id);
      } catch (error) {
        toast(error.message, { tone: "error" });
        return;
      }
    }
    if (task) this.dialog.openTask(task, this.tasks);
  }

  _openPendingTask() {
    const id = this._pendingTask;
    this._pendingTask = "";
    if (id) void this.openTaskById(id);
  }

  filters() {
    return {
      search: this.nodes.search?.value ?? "",
      type: this.nodes.type?.value ?? "",
      priority: this.nodes.priority?.value ?? "",
      status: this.nodes.statusFilter?.value ?? "",
    };
  }

  shown() {
    return sortTasks(this.tasks.filter((task) => matchesFilters(task, this.filters())));
  }

  async load({ background = false } = {}) {
    const request = this.request = (this.request ?? 0) + 1;
    this.loading = true;
    this.nodes.board?.setAttribute("aria-busy", "true");
    this._status("Loading tasks…");
    const projectId = this.nodes.project?.value || "";
    this.project = projectId || null;
    try {
      if (projectId) {
        this._conversation = "";
        // The project's aggregated plan: the tasks its chats planned, merged -
        // bd's statuses win, a task only a chat's trace knows is marked "trace".
        const report = await api.projectPlan(projectId);
        if (request !== this.request) return;
        this.available = true;
        this.reason = "";
        this.tasks = report.tasks ?? [];
        this.chats = report.chats ?? {};
        this._updateUrl();
        this.render();
        this._openPendingTask();
        return;
      }
      if (this._conversation) {
        // One chat's plan (`?conversation=<id>`): the tasks its turns planned,
        // each pointing back at the chat that made it, so the trip chat → task
        // → chat keeps its place in both directions.
        const plan = await api.conversationPlan(this._conversation);
        if (request !== this.request) return;
        this.available = true;
        this.reason = "";
        this.chats = {};
        this.tasks = (plan.tasks ?? []).map((task) => ({
          ...task,
          chat_ids: task.chat_ids ?? [this._conversation],
          source: task.source ?? "trace",
        }));
        this._updateUrl();
        this.render();
        this._openPendingTask();
        return;
      }
      this.chats = {};
      this._updateUrl();
      const report = await api.tasks(this.nodes.statusFilter?.value || "");
      if (request !== this.request) return;
      this.available = Boolean(report.available);
      this.reason = report.reason || "";
      this.tasks = report.tasks ?? [];
      if (!this.available) this._unavailable();
      else {
        this.render();
        this._openPendingTask();
      }
    } catch (error) {
      if (request !== this.request) return;
      // A background refresh (the page's own timer, or several tabs open) that
      // failed must not empty a board the user is reading: keep the last good
      // tasks and say what happened, with a retry. A first load still fails
      // honestly, into the page's own error state.
      if (background && this.available === true) {
        this._status(
          `Couldn't refresh: ${error.message} — showing the last known board.`,
          { error: true, retry: () => this.load({ background: true }) },
        );
      } else {
        this._failed(error.message);
      }
    } finally {
      if (request === this.request) {
        this.loading = false;
        if (this.nodes.newTask) this.nodes.newTask.disabled = this.available !== true;
        this.nodes.board?.setAttribute("aria-busy", "false");
      }
    }
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

  _status(text, { error = false, retry = null } = {}) {
    const node = this.nodes.status;
    if (!node) return;
    node.dataset.error = String(error);
    if (!retry) {
      replace(node, h("span", { text }));
      return;
    }
    const again = h("button.btn.btn--ghost", { type: "button", text: "Retry" });
    again.addEventListener("click", () => { again.disabled = true; void retry(); });
    replace(node, h("span", { text }), again);
  }

  _unavailable() {
    for (const node of [this.nodes.board, this.nodes.listRows, this.nodes.metrics]) {
      if (node) replace(node);
    }
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
    this.available = false;
    this.tasks = [];
    for (const node of [this.nodes.board, this.nodes.listRows, this.nodes.metrics]) {
      if (node) replace(node);
    }
    this._status(message, { error: true, retry: () => this.load() });
  }

  render() {
    if (this.available === false) return;
    if (this.nodes.metrics) {
      replace(this.nodes.metrics);
      this.nodes.metrics.hidden = true;
    }
    this._renderPresets();
    this._renderActiveFilters();
    this._renderUndo();
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
            { dataset: { state, empty: String(!column.length) } },
            h("div.tasksColumnHead", {}, h("h2", { text: STATES[state].label }), h("span", { text: String(column.length) })),
            h("ul", {}, column.map((task) => taskCard(
              task,
              (chosen) => this.dialog.openTask(chosen, this.tasks),
              (action, chosen) => void this.quickAction(action, chosen),
              task.id === this.selectedId,
            ))),
            column.length ? null : h("p.tasksColumnEmpty", { text: "No tasks in this state" }),
          );
        }),
      );
    }
    if (this.nodes.listRows) {
      replace(
        this.nodes.listRows,
        shown.length
          ? shown.map((task) => taskRow(task, (chosen) => this.dialog.openTask(chosen, this.tasks), task.id === this.selectedId))
          : [h("tr", {}, h("td", { colspan: 7 }, h("p.tasksEmpty", { text: "No tasks." })))],
      );
    }
  }

  // -- presets, active-filter chips and undo -----------------------------------

  /** The preset whose controls match what is set now, if any. */
  _currentPreset() {
    const status = this.nodes.statusFilter?.value ?? "";
    const priority = this.nodes.priority?.value ?? "";
    const type = this.nodes.type?.value ?? "";
    const q = String(this.nodes.search?.value ?? "").trim();
    if (type || q) return null;
    return PRESETS.find((preset) =>
      (preset.status ?? "") === status && (preset.priority ?? "") === priority)?.key ?? null;
  }

  _renderPresets() {
    const host = this.nodes.presets;
    if (!host) return;
    const active = this._currentPreset();
    replace(host, PRESETS.map((preset) => h("button.tasksPreset", {
      type: "button",
      dataset: { preset: preset.key },
      "aria-pressed": String(active === preset.key),
      text: preset.label,
      on: { click: () => this.applyPreset(preset) },
    })));
  }

  /** Apply a preset through the same controls a hand-made filter uses. */
  applyPreset(preset) {
    const set = (node, value) => { if (node) node.value = value; };
    set(this.nodes.statusFilter, preset.status ?? "");
    set(this.nodes.priority, preset.priority ?? "");
    set(this.nodes.type, "");
    set(this.nodes.search, "");
    this._updateUrl({ push: true });
    if (preset.status !== undefined) void this.load();
    else this.render();
  }

  _renderActiveFilters() {
    const host = this.nodes.activeFilters;
    if (!host) return;
    const chips = [];
    const status = this.nodes.statusFilter?.value;
    if (status) chips.push(this._filterChip("status", `Status: ${status}`, () => { this.nodes.statusFilter.value = ""; }, true));
    const type = this.nodes.type?.value;
    if (type) chips.push(this._filterChip("type", `Type: ${type}`, () => { this.nodes.type.value = ""; }));
    const priority = this.nodes.priority?.value;
    if (priority !== "" && priority != null) chips.push(this._filterChip("priority", `Priority: P${priority}`, () => { this.nodes.priority.value = ""; }));
    const q = String(this.nodes.search?.value ?? "").trim();
    if (q) chips.push(this._filterChip("q", `“${q}”`, () => { this.nodes.search.value = ""; }));
    host.hidden = !chips.length;
    replace(host, chips.length
      ? [h("span.tasksActiveFilters__label", { text: "Filters" }), ...chips]
      : null);
  }

  _filterChip(kind, label, clear, reload = false) {
    return h("button.tasksFilterChip", {
      type: "button",
      dataset: { kind },
      "aria-label": `Remove filter ${label}`,
      on: {
        click: () => {
          clear();
          this._updateUrl({ push: true });
          if (reload) void this.load();
          else this.render();
        },
      },
    }, h("span", { text: label }), icon(ICONS.close, { size: 11 }));
  }

  /** Offer the last change as an undo, in the page's own bar. */
  offerUndo(message, run) {
    this._undo = { message, run };
    this._renderUndo();
  }

  _renderUndo() {
    const host = this.nodes.undo;
    if (!host) return;
    if (!this._undo) {
      host.hidden = true;
      replace(host);
      return;
    }
    host.hidden = false;
    replace(
      host,
      h("span.tasksUndo__text", { text: this._undo.message }),
      h("button.tasksUndo__action", { type: "button", text: "Undo", on: { click: () => void this._runUndo() } }),
      h("button.tasksUndo__dismiss", { type: "button", "aria-label": "Dismiss undo", text: "×",
        on: { click: () => { this._undo = null; this._renderUndo(); } } }),
    );
  }

  async _runUndo() {
    const undo = this._undo;
    if (!undo) return;
    this._undo = null;
    this._renderUndo();
    try {
      await undo.run();
      toast("Undone", { tone: "success" });
      await this.load();
    } catch (error) {
      toast(error.message, { tone: "error" });
    }
  }

  /** The signed-in user's name, for "Take"; null when nobody is signed in. */
  async currentUser() {
    if (this._me) return this._me;
    try {
      const response = await fetch("/api/auth/me", { cache: "no-store" });
      if (!response.ok) return null;
      const payload = await response.json();
      this._me = payload?.username ?? payload?.name ?? null;
      return this._me;
    } catch {
      return null;
    }
  }

  /**
   * Change one task in place, at once, and redraw - the optimistic half of a
   * write. Returns the rollback that puts the old fields back when the API
   * refuses the change, so the card never keeps a state the tracker did not.
   */
  _patchTask(task, patch) {
    const previous = {};
    for (const key of Object.keys(patch)) previous[key] = task[key];
    Object.assign(task, patch);
    this.render();
    return () => {
      Object.assign(task, previous);
      this.render();
    };
  }

  /** A card's quick action: no dialog, the same API write, and an undo. */
  async quickAction(action, task) {
    if (!task || task.source === "trace" || task.readonly === true) return;
    let patch;
    let perform;
    let undo;
    let undoMessage;
    let done;
    if (action === "take") {
      const me = await this.currentUser();
      if (!me) {
        toast("Sign in to take a task", { tone: "error" });
        return;
      }
      const previous = task.assignee ?? "";
      patch = { assignee: me };
      perform = () => api.updateTask(task.id, { assignee: me });
      undo = () => api.updateTask(task.id, { assignee: previous });
      undoMessage = `Took “${task.title}” (assignee: ${me})`;
      done = `Took “${task.title}”`;
    } else if (action === "close") {
      patch = { status: "closed", state: "closed", close_reason: "Completed" };
      perform = () => api.closeTask(task.id, "Completed");
      undo = () => api.updateTask(task.id, { status: "open" });
      undoMessage = `Closed “${task.title}”`;
      done = "Task closed";
    } else if (action === "reopen") {
      const reason = task.close_reason || "Completed";
      patch = { status: "open", state: "ready", close_reason: null };
      perform = () => api.updateTask(task.id, { status: "open" });
      undo = () => api.closeTask(task.id, reason);
      undoMessage = `Reopened “${task.title}”`;
      done = "Task reopened";
    } else {
      return;
    }
    // Show the change now; the request follows, and an error takes it back.
    const rollback = this._patchTask(task, patch);
    try {
      await perform();
      this.offerUndo(undoMessage, undo);
      toast(done, { tone: "success" });
      await this.load();
    } catch (error) {
      rollback();
      toast(error.message, { tone: "error" });
    }
  }

  // -- the keyboard: Ctrl/⌘ K, j/k, Enter, N, ? --------------------------------

  /** The page's keystrokes, bound to the document once the page is built. */
  _bindKeyboard() {
    if (typeof document === "undefined" || typeof document.addEventListener !== "function") return;
    document.addEventListener("keydown", (event) => this._onKeyDown(event));
  }

  /** Whether a keystroke is landing in a field the user is typing into. */
  _typingIn(target) {
    const tag = target?.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target?.isContentEditable === true;
  }

  /**
   * One keydown for the whole page: the palette owns the keys while it is up;
   * otherwise j/k walk the shown tasks, Enter opens the cursor, N makes a task,
   * ? shows the sheet - and none of that fires while a field has the focus.
   */
  _onKeyDown(event) {
    if (!event || typeof event.key !== "string") return;
    if ((event.metaKey || event.ctrlKey) && (event.key === "k" || event.key === "K")) {
      event.preventDefault?.();
      this.palette.toggle();
      return;
    }
    if (this.palette.isOpen()) {
      this.palette.onKeyDown(event);
      return;
    }
    if (this.cheatsheet.isOpen()) {
      if (event.key === "Escape" || event.key === "?") {
        event.preventDefault?.();
        this.cheatsheet.close();
      }
      return;
    }
    // While a task's dialog is up, the page-level keys stay out of its way.
    if (this.dialog?.el?.dataset?.open === "true") return;
    if (this._typingIn(event.target)) return;
    if (event.key === "?") {
      event.preventDefault?.();
      this.cheatsheet.toggle();
      return;
    }
    if (event.key === "n" || event.key === "N") {
      event.preventDefault?.();
      this.dialog.openCreate(this.tasks);
      return;
    }
    if (event.key === "j" || event.key === "J") {
      event.preventDefault?.();
      this._moveSelection(1);
      return;
    }
    if (event.key === "k" || event.key === "K") {
      event.preventDefault?.();
      this._moveSelection(-1);
      return;
    }
    if (event.key === "Enter" && this.selectedId) {
      const task = this.shown().find((candidate) => candidate.id === this.selectedId);
      if (task) {
        event.preventDefault?.();
        this.dialog.openTask(task, this.tasks);
      }
    }
  }

  /** Walk the j/k cursor through the shown tasks and redraw its highlight. */
  _moveSelection(delta) {
    const shown = this.shown();
    if (!shown.length) return;
    const current = shown.findIndex((task) => task.id === this.selectedId);
    const at = current < 0
      ? (delta > 0 ? 0 : shown.length - 1)
      : Math.min(Math.max(current + delta, 0), shown.length - 1);
    this.selectedId = shown[at].id;
    this.render();
  }

  /**
   * A task made straight from the palette: it appears at once, the API follows,
   * and an error takes the draft back so the board never shows a task that is
   * not in the tracker.
   */
  async _createInline(title) {
    const clean = String(title ?? "").trim();
    if (!clean) return;
    const draft = {
      id: `new-${Date.now()}-${Math.round(Math.random() * 1e6)}`, title: clean, state: "ready", status: "open",
      priority: 2, type: "task", assignee: null, description: "", acceptance: "", notes: "", close_reason: null,
      depends_on: [], waits_for: [], updated_at: new Date().toISOString(), source: "bd",
    };
    this.tasks = [draft, ...this.tasks];
    this.selectedId = draft.id;
    this.render();
    try {
      const created = await api.createTask({ title: clean });
      toast(`Created “${clean}”`, { tone: "success" });
      if (created?.id) this.selectedId = created.id;
      await this.load();
    } catch (error) {
      this.tasks = this.tasks.filter((task) => task !== draft);
      this.render();
      toast(error.message, { tone: "error" });
    }
  }

  // -- the command palette (Ctrl/⌘ K) ------------------------------------------

  /**
   * Everything the tracker can do in one box: type to filter the tasks, Enter
   * to open the one under the cursor, and a query with no task carries a
   * "Create …" row that makes the task from the same line.
   */
  _palette() {
    const input = h("input.tasksPalette__input", {
      type: "text", "aria-label": "Command palette", placeholder: "Search tasks, or type a title to create one…", autocomplete: "off",
    });
    const list = h("ul.tasksPalette__list", { role: "listbox" });
    const el = h(
      "div.tasksPalette",
      { role: "dialog", "aria-modal": "true", "aria-label": "Command palette", hidden: true },
      h("div.tasksPalette__panel", {}, input, list),
    );
    const page = this;
    const state = { open: false, query: "", index: 0, rows: [] };

    const rows = () => {
      const found = sortTasks(page.tasks.filter((task) => matchesFilters(task, { search: state.query }))).slice(0, 50);
      const next = found.map((task) => ({ kind: "task", task, label: task.title, hint: task.id }));
      const query = state.query.trim();
      if (query) next.push({ kind: "create", title: query, label: `Create “${query}”`, hint: "new task" });
      state.rows = next;
      return next;
    };

    const draw = () => {
      const current = rows();
      if (state.index > current.length - 1) state.index = Math.max(current.length - 1, 0);
      replace(list, current.length
        ? current.map((row, index) => h(
            "li.tasksPalette__row",
            { dataset: { kind: row.kind, active: String(index === state.index) } },
            h("button.tasksPalette__item", { type: "button", on: { click: () => { state.index = index; activate(); } } },
              h("span.tasksPalette__label", { text: row.label }),
              h("span.tasksPalette__hint", { text: row.hint })),
          ))
        : [h("li.tasksPalette__empty", { text: page.tasks.length ? "No task matches." : "No tasks yet — type a title to create one." })]);
    };

    const activate = () => {
      const row = state.rows[state.index];
      if (!row) return;
      if (row.kind === "task") {
        close();
        page.selectedId = row.task.id;
        page.dialog.openTask(row.task, page.tasks);
        return;
      }
      if (row.kind === "create") {
        close();
        void page._createInline(row.title);
      }
    };

    const open = () => {
      state.open = true;
      state.query = "";
      state.index = 0;
      input.value = "";
      el.hidden = false;
      el.dataset.open = "true";
      draw();
      input.focus?.();
    };
    const close = () => {
      state.open = false;
      el.hidden = true;
      delete el.dataset.open;
    };

    input.addEventListener("input", () => {
      state.query = input.value;
      state.index = 0;
      draw();
    });

    const onKeyDown = (event) => {
      if (event.key === "Escape") { event.preventDefault?.(); close(); return; }
      if (event.key === "ArrowDown") { event.preventDefault?.(); state.index = Math.min(state.index + 1, Math.max(state.rows.length - 1, 0)); draw(); return; }
      if (event.key === "ArrowUp") { event.preventDefault?.(); state.index = Math.max(state.index - 1, 0); draw(); return; }
      if (event.key === "Enter") { event.preventDefault?.(); activate(); }
    };

    return {
      el,
      input,
      list,
      toggle: () => (state.open ? close() : open()),
      open,
      close,
      onKeyDown,
      isOpen: () => state.open,
      rows: () => state.rows,
    };
  }

  // -- the shortcuts sheet (?) -------------------------------------------------

  /** The keys the page listens for, in one place shown by ?. */
  _cheatsheet() {
    const SHORTCUTS = [
      ["Ctrl / ⌘ K", "Command palette"],
      ["j / k", "Next / previous task"],
      ["Enter", "Open the task under the cursor"],
      ["n", "New task"],
      ["?", "This sheet"],
      ["Esc", "Close the palette or this sheet"],
    ];
    const el = h(
      "div.tasksCheatsheet",
      { role: "dialog", "aria-modal": "true", "aria-label": "Keyboard shortcuts", hidden: true },
      h(
        "div.tasksCheatsheet__panel",
        {},
        h("h2", { text: "Keyboard shortcuts" }),
        h("dl.tasksCheatsheet__list", {}, SHORTCUTS.flatMap(([keys, what]) => [
          h("dt", {}, h("kbd", { text: keys })),
          h("dd", { text: what }),
        ])),
        h("button.btn.btn--ghost", { type: "button", text: "Close", on: { click: () => close() } }),
      ),
    );
    let open = false;
    const close = () => { open = false; el.hidden = true; delete el.dataset.open; };
    const toggle = () => {
      if (open) { close(); return; }
      open = true;
      el.hidden = false;
      el.dataset.open = "true";
    };
    return { el, toggle, close, isOpen: () => open };
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
    const title = h("h2.taskDialog__title", { id: "task-dialog-title", tabindex: "-1" });
    const documentBody = h("div.taskDialog__document");
    const edit = h("button.btn.btn--primary", { type: "button", text: "Edit" });
    const reopen = h("button.btn.btn--ghost", { type: "button", text: "Reopen" });
    const dirtyNote = h("p.taskDialog__dirty", { role: "status" });
    const closeSection = h("section.taskDialog__closeSection");
    const done = h("button.btn.btn--ghost", { type: "button", text: "Done" });
    const titleInput = h("input", { type: "text", "aria-label": "Title", required: "" });
    const statusSelect = h("select", { "aria-label": "Status" }, STATUSES.map((value) => h("option", { value, text: value })));
    const prioritySelect = h("select", { "aria-label": "Priority" },
      [h("option", { value: "", text: "—" }), ...PRIORITIES.map((value) => h("option", { value: String(value), text: `P${value}` }))]);
    const typeSelect = h("select", { "aria-label": "Type" }, [h("option", { value: "", text: "—" }), ...TYPES.map((value) => h("option", { value, text: value }))]);
    const assigneeInput = h("input", { type: "text", "aria-label": "Assignee", placeholder: "unassigned" });
    const description = h("textarea", { "aria-label": "Description", placeholder: "What the task asks for" });
    const acceptance = h("textarea", { "aria-label": "Acceptance criteria", placeholder: "How to tell it is done" });
    const notes = h("textarea", { "aria-label": "Notes", placeholder: "Write a note to add; nothing recorded is lost" });
    // What the tracker already holds, kept in view while a note is written.
    const notesExisting = h("div.taskDialog__notesExisting");
    const deps = h("ul.taskDialog__deps");
    const depError = h("p.taskDialog__error", { role: "alert" });
    depError.hidden = true;
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
      documentBody,
      h("div.taskDialog__readActions", {}, edit, reopen, done),
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
        h("div.taskDialog__field", {}, h("span", { text: "Notes" }), notesExisting, notes,
          h("small", { text: "What you write is added to the notes above — nothing recorded is lost." })),
        h("div.taskDialog__field", {}, h("span", { text: "Source chats" }), sourceList),
        readOnlyNote,
        h("div.taskDialog__field", {}, h("span", { text: "Depends on" }), deps,
          h("div.taskDialog__add", {}, depSelect, depAdd), depError),
        dirtyNote,
        h("footer.taskDialog__foot", {}, cancel, save),
      ),
    );
    el.append(closeSection);

    const page = this;
    let task = null; // the task the dialog shows; null while it is closed
    let mode = "read"; // "read" | "edit" | "create"
    let pendingDeps = [];
    let baseline = "";
    let busy = false;
    const form = el.querySelector("form");
    const inputs = [titleInput, statusSelect, prioritySelect, typeSelect, assigneeInput, description, acceptance, notes];
    const snapshot = () => JSON.stringify([inputs.map(input => input.value), pendingDeps]);
    const isDirty = () => mode !== "read" && snapshot() !== baseline;
    const updateDirty = () => {
      dirtyNote.textContent = isDirty() ? "Unsaved changes" : "No unsaved changes";
      save.disabled = busy || (mode !== "create" && !isDirty());
    };
    form.addEventListener("input", updateDirty);
    form.addEventListener("change", updateDirty);
    const setMode = (next) => {
      mode = next;
      form.hidden = next === "read";
      documentBody.hidden = next !== "read";
      edit.parentNode.hidden = next !== "read";
      stateBadge.hidden = next !== "read";
      title.textContent = next === "read" ? task?.title : next === "create" ? "New task" : "Edit task";
      closeSection.hidden = next !== "read" || task?.source === "trace" || task?.readonly === true || task?.status === "closed";
      updateDirty();
    };
    const mayDiscard = () => !busy && (!isDirty() || window.confirm("Discard unsaved changes?"));

    const dismiss = () => {
      if (!mayDiscard()) return;
      task = null;
      el.close?.();
      page._updateUrl();
    };
    closeButton.addEventListener("click", dismiss);
    done.addEventListener("click", dismiss);
    cancel.addEventListener("click", () => {
      if (!mayDiscard()) return;
      if (mode === "create") dismiss();
      else { fill(task, page.tasks); title.focus?.(); }
    });
    el.addEventListener("cancel", (event) => { event.preventDefault(); dismiss(); });
    edit.addEventListener("click", () => {
      if (task?.source === "trace" || task?.readonly === true) return;
      setMode("edit"); titleInput.focus?.();
    });
    el.addEventListener?.("close", () => {
      task = null;
    });

    const fill = (chosen, tasks, creating = false) => {
      mode = creating ? "create" : "read";
      task = chosen;
      pendingDeps = [...(chosen.depends_on ?? [])];
      const state = creating ? null : STATES[chosen.state] ?? STATES.ready;
      replace(stateBadge, creating ? null : h("span", { text: `${state.mark} ${state.label}` }));
      idBadge.textContent = creating ? "new task" : chosen.id ?? "";
      title.textContent = creating ? "New task" : chosen.title;
      titleInput.value = creating ? "" : chosen.title ?? "";
      // Preserve closed tasks on ordinary edits. Closing an active task still
      // uses the explicit close action/reason, not this status selector.
      const statuses = !creating && chosen.status === "closed" ? [...STATUSES, "closed"] : STATUSES;
      replace(statusSelect, statuses.map((value) => h("option", { value, text: value })));
      statusSelect.value = creating ? "open" : (statuses.includes(chosen.status) ? chosen.status : "open");
      prioritySelect.value = creating ? "2" : chosen.priority == null ? "" : String(chosen.priority);
      typeSelect.value = creating ? "task" : chosen.type ?? "";
      assigneeInput.value = creating ? "" : chosen.assignee ?? "";
      description.value = creating ? "" : chosen.description ?? "";
      acceptance.value = creating ? "" : chosen.acceptance ?? "";
      // The field holds what is being *added*: existing notes stay in view,
      // the box starts empty, and saving appends rather than replaces.
      notes.value = "";
      notesExisting.hidden = creating;
      replace(
        notesExisting,
        creating
          ? null
          : chosen.notes
            ? h("div.taskDialog__prose", { html: renderMarkdown(chosen.notes) })
            : h("p.taskDialog__missing", { text: "No notes recorded." }),
      );
      depError.hidden = true;
      depError.textContent = "";
      reasonInput.value = "";
      closeTask.hidden = creating;
      // A task only a chat's trace knows is read-only here: the tracker holds
      // no record of it to change (the plan card in the chat still shows it).
      const traceOnly = !creating && (chosen.source === "trace" || chosen.readonly === true);
      save.hidden = traceOnly;
      readOnlyNote.hidden = !traceOnly;
      closeTask.hidden = creating || traceOnly || chosen.status === "closed";
      reasonInput.hidden = closeTask.hidden;
      // A closed task offers an explicit Reopen beside Edit; there is no
      // silent status flip for it (the status selector stays for edits).
      reopen.hidden = creating || traceOnly || chosen.status !== "closed";
      for (const input of [titleInput, statusSelect, prioritySelect, typeSelect, assigneeInput, description, acceptance, notes, depSelect, depAdd]) {
        input.disabled = traceOnly;
      }
      if (creating) {
        replace(sourceList);
      } else {
        const chatIds = chosen.chat_ids ?? [];
        replace(
          sourceList,
          chatIds.length
            ? chatIds.map((chatId) => chatChip(chatId, { title: page.chatTitle(chatId), task: chosen.id }))
            : h("p.taskDialog__missing", { text: "No chat recorded this task." }),
        );
      }
      renderDeps(tasks);
      const section = (label, content) => h("section.taskDialog__section", {}, h("h3", { text: label }), content);
      const prose = (text, empty) => text ? h("div.taskDialog__prose", { html: renderMarkdown(text) }) : h("p.taskDialog__missing", { text: empty });
      replace(documentBody,
        h("dl.taskDialog__metadata", {},
          [["Priority", chosen.priority == null ? "—" : `P${chosen.priority}`], ["Type", chosen.type || "—"],
            ["Assignee", chosen.assignee || "Unassigned"], ["Source", chosen.source || "bd"],
            ["Created", when(chosen.created_at)], ["Updated", when(chosen.updated_at)]].map(([label, value]) =>
            h("div", {}, h("dt", { text: label }), h("dd", { text: value })))),
        traceOnly ? h("p.taskDialog__missing", { text: readOnlyNote.textContent }) : null,
        section("Description", prose(chosen.description, "No description recorded.")),
        section("Acceptance criteria", prose(chosen.acceptance, "No acceptance criteria recorded.")),
        section("Notes", prose(chosen.notes, "No notes recorded.")),
        // The run that took the task reported; the tracker shows it too, with
        // the answer only when the run completed (mirrors the plan card).
        chosen.report ? section("Result", chosen.report.status === "completed" && typeof chosen.report.final === "string" && chosen.report.final.trim()
          ? prose(chosen.report.final)
          : h("p.taskDialog__missing", { text: chosen.report.status === "completed" ? "The run completed, but no result text was recorded." : `The run did not complete (${chosen.report.status ?? "no status"}); its result was not recorded.` })) : null,
        chosen.status === "closed" || chosen.state === "closed" ? section("Close reason", prose(chosen.close_reason, "Closed without a recorded reason.")) : null,
        section("Depends on", h("ul.taskDialog__deps", {}, (chosen.depends_on ?? []).length ? chosen.depends_on.map(id => {
          const target = tasks.find(other => other.id === id || other.key === id);
          return h("li", {}, target ? h("button.taskDialog__depLink", { type: "button", text: target.title,
            on: { click: () => { fill(target, tasks); title.focus?.(); el.scrollTop = 0; } } }) : h("span", { text: `${id} (not in this view)` }));
        }) : h("li.taskDialog__missing", { text: "No dependencies." }))),
        section("Source chats", h("div.taskDialog__sources", {}, (chosen.chat_ids ?? []).length
          ? chosen.chat_ids.map(id => chatChip(id, { title: page.chatTitle(id), task: chosen.id })) : h("p.taskDialog__missing", { text: "No chat recorded this task." }))),
      );
      replace(closeSection, h("h3", { text: "Close task" }),
        h("label", {}, h("span", { text: "Close reason" }), reasonInput), closeTask);
      edit.hidden = traceOnly;
      baseline = snapshot();
      setMode(creating ? "create" : "read");
    };

    const renderDeps = (tasks) => {
      const dependsOn = pendingDeps;
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
                  disabled: task?.source === "trace" || task?.readonly === true,
                  type: "button",
                  text: "remove",
                  on: {
                    click: async () => {
                      pendingDeps = pendingDeps.filter((dep_id) => dep_id !== dep);
                      renderDeps(tasks);
                      updateDirty();
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

    depAdd.addEventListener("click", () => {
      const parentId = depSelect.value;
      if (!parentId || (mode !== "create" && !task?.id)) return;
      if (mode === "read" || busy) return;
      if (pendingDeps.includes(parentId)) return;
      // A ring is refused here, with the chain named, before it ever reaches
      // the tracker; the server refuses it too, for anything this view missed.
      const ring = dependencyRing(this.tasks, task?.id, parentId);
      if (ring) {
        depError.hidden = false;
        depError.textContent = ringReason(this.tasks, task?.id, parentId, ring);
        return;
      }
      depError.hidden = true;
      depError.textContent = "";
      pendingDeps.push(parentId);
      renderDeps(this.tasks);
      updateDirty();
    });

    closeTask.addEventListener("click", async () => {
      if (!task || busy || mode !== "read" || task.source === "trace" || task.readonly === true) return;
      const closing = task;
      busy = true;
      closeTask.disabled = true;
      try {
        await api.closeTask(closing.id, reasonInput.value.trim() || "Completed");
        toast("Task closed", { tone: "success" });
        page.offerUndo(`Closed “${closing.title}”`, () => api.updateTask(closing.id, { status: "open" }));
        busy = false;
        await page.load();
        dismiss();
      } catch (error) {
        toast(error.message, { tone: "error" });
      } finally {
        busy = false;
        closeTask.disabled = false;
        updateDirty();
      }
    });

    reopen.addEventListener("click", async () => {
      if (!task || busy || mode !== "read" || task.source === "trace" || task.readonly === true || task.status !== "closed") return;
      const reopened = task;
      busy = true;
      reopen.disabled = true;
      try {
        await api.updateTask(reopened.id, { status: "open" });
        toast("Task reopened", { tone: "success" });
        page.offerUndo(`Reopened “${reopened.title}”`, () => api.closeTask(reopened.id, reopened.close_reason || "Completed"));
        busy = false;
        await page.load();
        dismiss();
      } catch (error) {
        toast(error.message, { tone: "error" });
      } finally {
        busy = false;
        reopen.disabled = false;
        updateDirty();
      }
    });

    el.querySelector("form").addEventListener("submit", async (event) => {
      event.preventDefault?.();
      if (busy || mode === "read" || task?.source === "trace" || task?.readonly === true) return;
      busy = true; updateDirty();
      const fields = {
        title: titleInput.value.trim(),
        status: statusSelect.value,
        priority: prioritySelect.value === "" ? null : Number(prioritySelect.value),
        type: typeSelect.value || null,
        assignee: assigneeInput.value.trim() || null,
        description: description.value,
        acceptance: acceptance.value,
        // An edit adds a note to what the task held; a new task starts with it.
        notes: mode === "create" ? notes.value : notesWithAddition(task.notes, notes.value),
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
          for (const id of [...(task.depends_on ?? [])]) {
            if (!pendingDeps.includes(id)) { await api.taskDep(task.id, "remove", id); task.depends_on = task.depends_on.filter(dep => dep !== id); }
          }
          for (const id of pendingDeps) {
            if (!(task.depends_on ?? []).includes(id)) { await api.taskDep(task.id, "add", id); task.depends_on = [...(task.depends_on ?? []), id]; }
          }
          Object.assign(task, fields);
        }
        await page.load();
        busy = false;
        if (mode === "create") { baseline = snapshot(); dismiss(); }
        else { fill(page.tasks.find(fresh => fresh.id === task.id) ?? task, page.tasks); title.focus?.(); }
      } catch (error) {
        toast(error.message, { tone: "error" });
      } finally {
        busy = false; updateDirty();
      }
    });

    return {
      el,
      /** Show a task to read or change it. */
      openTask: (chosen, tasks) => {
        fill(chosen, tasks);
        el.dataset.open = "true";
        el.showModal?.();
        el.scrollTop = 0;
        title.focus?.();
        page._updateUrl({ push: true });
      },
      /** A new task, from the same fields. */
      openCreate: (tasks, { title: seed = "" } = {}) => {
        fill({}, tasks, true);
        if (seed) titleInput.value = seed;
        el.dataset.open = "true";
        el.showModal?.();
        titleInput.focus?.();
        page._updateUrl();
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
  createTrackerTheme($("theme"));
  const page = new TasksPage({
    status: $("status"),
    presets: $("presets"),
    activeFilters: $("active-filters"),
    undo: $("undo"),
    metrics: $("metrics"),
    board: $("board"),
    listWrap: $("list"),
    listRows: $("list-rows"),
    search: $("search"),
    type: $("type"),
    priority: $("priority"),
    statusFilter: $("status-filter"),
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
    if ($("live")?.checked && !document.hidden) void page.load({ background: true });
  }, 30000);
}
