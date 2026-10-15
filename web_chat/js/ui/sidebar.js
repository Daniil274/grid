/**
 * The rail's list: the user's projects as a tree, with their chats inside, and
 * below them the chats of no project, by date.
 *
 * - A project row opens and closes its branch (the chevron), and a click on
 *   its name also makes it the active project: the one a new chat starts in
 *   (web_chat/js/ui/projects.js). Its hover actions start a chat in it ("+")
 *   or open its menu ("⋯": new chat, sub-project, edit, tasks, archive, delete).
 *   Which branches are open is remembered across reloads; the branch holding
 *   the chat on screen opens by itself. A long project shows its latest chats
 *   and a "Show N more" line; archived projects wait folded at the end.
 * - A chat row is one line - title and age - with a "⋯" menu (rename, move to
 *   a project, its tasks, delete); a double click renames it in place. Opening
 *   a chat makes its project the active one, so "New chat" continues there.
 * - A chat is dragged onto a project to move it there, or onto "Chats" to take
 *   it out of any project.
 * - The search reads projects and chats alike and lists what matches flat,
 *   each chat with the project it lives in.
 *
 * The list re-renders from the store on every change - tens of rows, not
 * thousands - so it never drifts from the conversation on screen. The one
 * exception is a row being renamed: re-rendering would steal its input's focus.
 */

import { h, icon, replace } from "../lib/dom.js";
import { dayBucket, plural, shortStamp } from "../lib/format.js";
import { ICONS } from "./icons.js";
import { openProjectDialog } from "./project-dialog.js";

const BUCKET_ORDER = ["Today", "Yesterday", "This week", "This month", "Earlier"];
/** Chats a project shows before "Show N more". */
export const PROJECT_PREVIEW = 5;
/** How deeply projects nest; the store's own limit (web_chat/projects.py). */
const MAX_DEPTH = 3;
/** Swatch colours a project is told apart by (styles/rail.css, --swatch-0..7). */
const SWATCHES = 8;
const EXPANDED_KEY = "grid:rail:expanded";
const SECTIONS_KEY = "grid:rail:folded";
const DRAG_TYPE = "application/x-grid-chat";

const MORE = "M5 12h.01M12 12h.01M19 12h.01";

/** A stable swatch for a project, from its id. */
export function swatchOf(id) {
  let hash = 0;
  for (const char of String(id ?? "")) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return hash % SWATCHES;
}

const storage = {
  read(key, fallback) {
    try {
      const raw = globalThis.localStorage?.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch {
      return fallback;
    }
  },
  write(key, value) {
    try {
      globalThis.localStorage?.setItem(key, JSON.stringify(value));
    } catch {
      /* private mode: the rail just forgets on reload */
    }
  },
};

function groupByDay(conversations) {
  const groups = new Map(BUCKET_ORDER.map((name) => [name, []]));
  for (const conversation of conversations) groups.get(dayBucket(conversation.updated_at)).push(conversation);
  return [...groups].filter(([, items]) => items.length);
}

/** *node* placed *depth* levels into the tree (styles/rail.css reads --depth). */
function atDepth(node, depth) {
  if (node.style?.setProperty) node.style.setProperty("--depth", String(depth));
  node.dataset.depth = String(depth);
  return node;
}

const newest = (a, b) => String(b.updated_at ?? "").localeCompare(String(a.updated_at ?? ""));

/** A chat's age in a few characters: "now", "5m", "3h", then its date. */
export function ageOf(value, now = Date.now()) {
  const time = value ? new Date(value).getTime() : NaN;
  if (!Number.isFinite(time)) return "";
  const minutes = Math.floor((now - time) / 60_000);
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 12 * 60) return `${Math.floor(minutes / 60)}h`;
  return shortStamp(value);
}

/** The project tree's facts the rows need: children, chats, ancestors, depth. */
function treeOf(projects, conversations) {
  const byId = new Map(projects.map((project) => [project.id, project]));
  const children = new Map();
  for (const project of projects) {
    const parent = project.parent_id && byId.has(project.parent_id) ? project.parent_id : null;
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(project);
  }
  for (const list of children.values()) list.sort((a, b) => a.title.localeCompare(b.title));
  const chats = new Map();
  const unfiled = [];
  for (const conversation of conversations) {
    if (conversation.project && byId.has(conversation.project)) {
      if (!chats.has(conversation.project)) chats.set(conversation.project, []);
      chats.get(conversation.project).push(conversation);
    } else {
      unfiled.push(conversation);
    }
  }
  for (const list of chats.values()) list.sort(newest);
  const ancestors = (id) => {
    const out = [];
    for (let current = byId.get(id); current?.parent_id && byId.has(current.parent_id); current = byId.get(current.parent_id)) {
      out.push(current.parent_id);
    }
    return out;
  };
  const subtree = (id) => {
    const ids = [id];
    for (const child of children.get(id) ?? []) ids.push(...subtree(child.id));
    return ids;
  };
  const count = (id) => subtree(id).reduce((sum, member) => sum + (chats.get(member)?.length ?? 0), 0);
  const running = (id) => subtree(id).some((member) => (chats.get(member) ?? []).some((chat) => chat.active));
  const path = (id) => [...ancestors(id).reverse(), id].map((member) => byId.get(member)?.title).filter(Boolean);
  return { byId, children, chats, unfiled, ancestors, count, running, path, depth: (id) => ancestors(id).length };
}

/**
 * A floating menu by *anchor*, on the page's top layer so the rail's scroll
 * never clips it. One at a time; Escape, a click elsewhere or a scroll closes it.
 */
let openMenu = null;
export function closeRailMenu() {
  openMenu?.remove();
  openMenu = null;
}

function showMenu(anchor, items, label) {
  closeRailMenu();
  const menu = h("div.railMenu", { role: "menu", "aria-label": label, on: { click: (event) => event?.stopPropagation?.() } });
  const fill = (entries) =>
    replace(
      menu,
      entries.filter(Boolean).map((item) => {
        if (item === "-") return h("div.railMenu__sep", { role: "separator" });
        if (item.node) return item.node;
        const props = {
          role: "menuitem",
          class: item.danger ? "is-danger" : "",
          title: item.title ?? null,
          on: {
            click: (event) => {
              if (item.submenu) {
                event?.preventDefault?.();
                fill(item.submenu());
                return;
              }
              closeRailMenu();
              item.run?.();
            },
          },
        };
        const body = [icon(item.icon ?? ICONS.chevron, { size: 14 }), h("span.railMenu__label", { text: item.label })];
        if (item.hint) body.push(h("span.railMenu__hint", { text: item.hint }));
        return item.href
          ? h("a.railMenu__item", { ...props, href: item.href }, ...body)
          : h("button.railMenu__item", { ...props, type: "button" }, ...body);
      }),
    );
  fill(items);
  document.body.append(menu);
  openMenu = menu;
  const rect = anchor?.getBoundingClientRect?.();
  if (rect) {
    const width = 232;
    const left = Math.max(8, Math.min(rect.right - width, (globalThis.innerWidth ?? 1024) - width - 8));
    const below = rect.bottom + 4;
    const fitsBelow = below + 320 < (globalThis.innerHeight ?? 768);
    menu.style.left = `${left}px`;
    if (fitsBelow) menu.style.top = `${below}px`;
    else menu.style.bottom = `${(globalThis.innerHeight ?? 768) - rect.top + 4}px`;
  }
  menu.querySelector?.("button, a")?.focus?.();
  return menu;
}

document.addEventListener?.("click", () => closeRailMenu());
document.addEventListener?.("keydown", (event) => {
  if (event.key === "Escape") closeRailMenu();
});

export function createConversationList({
  container,
  searchInput,
  store,
  onSelect,
  onRename,
  onDelete,
  onMoveToProject,
  onCreateProject,
  onCreateSubproject,
  onUpdateProject,
  onDeleteProject,
  onNewChat,
}) {
  let query = "";
  let renaming = null; // id of the chat whose title is an input right now
  let archivedOpen = false;
  const expanded = new Set(storage.read(EXPANDED_KEY, []));
  const folded = new Set(storage.read(SECTIONS_KEY, [])); // "projects" | "chats"
  const showAll = new Set(); // projects listing every chat, not the latest few
  // The rail's own changes (a branch opened, a section folded) always redraw it.
  const rerender = () => render(store.get(), { force: true });
  container.addEventListener?.("scroll", closeRailMenu);

  const toggleExpanded = (id, open = !expanded.has(id)) => {
    if (open) expanded.add(id);
    else expanded.delete(id);
    storage.write(EXPANDED_KEY, [...expanded]);
  };

  const toggleSection = (name) => {
    if (folded.has(name)) folded.delete(name);
    else folded.add(name);
    storage.write(SECTIONS_KEY, [...folded]);
    rerender();
  };

  // -- drag a chat onto a project -----------------------------------------------
  const dropTarget = (node, projectId) => {
    node.addEventListener?.("dragover", (event) => {
      if (![...(event.dataTransfer?.types ?? [])].includes(DRAG_TYPE)) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      node.classList.add("is-drop");
    });
    node.addEventListener?.("dragleave", () => node.classList.remove("is-drop"));
    node.addEventListener?.("drop", (event) => {
      node.classList.remove("is-drop");
      const id = event.dataTransfer?.getData?.(DRAG_TYPE);
      if (!id) return;
      event.preventDefault();
      const conversation = store.get().conversations.find((item) => item.id === id);
      if (!conversation || (conversation.project ?? null) === projectId) return;
      if (projectId) toggleExpanded(projectId, true);
      void Promise.resolve(onMoveToProject?.(id, projectId)).then(rerender);
    });
  };

  // -- a chat ----------------------------------------------------------------------
  const startRename = (conversation, titleNode) => {
    renaming = conversation.id;
    const original = conversation.title || "New chat";
    const input = h("input.chatRow__rename", { type: "text", value: original, maxlength: 120, "aria-label": "Chat title" });
    let done = false;
    const finish = async (save) => {
      if (done) return;
      done = true;
      renaming = null;
      const title = input.value.trim();
      if (save && title && title !== original) await onRename(conversation.id, title);
      rerender();
    };
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") void finish(true);
      else if (event.key === "Escape") void finish(false);
    });
    input.addEventListener("blur", () => void finish(true));
    input.addEventListener("click", (event) => event?.stopPropagation?.());
    titleNode.replaceWith(input);
    input.focus();
    input.select?.();
  };

  const moveItems = (conversation, tree) => {
    const items = [];
    if (conversation.project) {
      items.push({ label: "No project", icon: ICONS.close, run: () => void move(conversation, null) });
    }
    const walk = (parent, depth) => {
      for (const project of tree.children.get(parent) ?? []) {
        if (project.archived) continue;
        if (project.id !== conversation.project) {
          items.push({
            label: `${" ".repeat(depth)}${project.title}`,
            icon: ICONS.folder,
            run: () => void move(conversation, project.id),
          });
        }
        walk(project.id, depth + 1);
      }
    };
    walk(null, 0);
    if (onCreateProject) {
      const input = h("input.railMenu__input", { type: "text", placeholder: "New project…", maxlength: 120, "aria-label": "New project name" });
      const create = async () => {
        const title = input.value.trim();
        if (!title) return;
        closeRailMenu();
        const project = await onCreateProject(title);
        if (project?.id) {
          toggleExpanded(project.id, true);
          await move(conversation, project.id);
        }
      };
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") void create();
      });
      items.push("-", { node: h("div.railMenu__create", {}, input, h("button.railMenu__go", { type: "button", on: { click: () => void create() } }, "Create")) });
    }
    return items;
  };

  const move = async (conversation, projectId) => {
    if (projectId) toggleExpanded(projectId, true);
    if (onMoveToProject) await onMoveToProject(conversation.id, projectId);
    rerender();
  };

  const chatMenu = (anchor, conversation, title, tree) =>
    showMenu(anchor, [
      { label: "Rename", icon: ICONS.edit, hint: "double-click", run: () => startRename(conversation, title) },
      onMoveToProject && { label: "Move to project", icon: ICONS.folder, submenu: () => moveItems(conversation, tree) },
      { label: "Tasks of this chat", icon: ICONS.check, href: `/tasks?conversation=${encodeURIComponent(conversation.id)}` },
      { label: "Files of this chat", icon: ICONS.file, href: `/files?context=${encodeURIComponent(conversation.id)}` },
      "-",
      {
        label: "Delete",
        icon: ICONS.trash,
        danger: true,
        run: () => {
          if (confirm(`Delete "${conversation.title || "New chat"}" with its files? This cannot be undone.`)) onDelete(conversation.id);
        },
      },
    ], `Actions for ${conversation.title || "New chat"}`);

  const chatRow = (conversation, activeId, tree, { depth = 0, where = null } = {}) => {
    const title = h("span.chatRow__title", { text: conversation.title || "New chat" });
    const meta = [
      conversation.routed_agent || conversation.agent_key || "auto",
      plural(conversation.message_count || 0, "message"),
      conversation.workspace ? conversation.workspace : null,
    ].filter(Boolean).join(" · ");
    // A call of its running turn waits for the user's Allow/Decline (approval-alerts.js).
    const awaiting = (conversation.awaiting_approval ?? []).length > 0;
    const select = () => {
      // Opening a chat of a project makes that project the one new chats start in.
      const project = conversation.project && tree.byId.has(conversation.project) ? conversation.project : null;
      if (store.get().activeProject !== project) store.set({ activeProject: project });
      onSelect(conversation.open_id ?? conversation.id);
    };
    const main = h(
      "button.chatRow__main",
      { type: "button", title: `${conversation.title || "New chat"}\n${meta}`, on: { click: select, dblclick: () => startRename(conversation, title) } },
      awaiting
        ? h("span.chatRow__ask", { title: "Waiting for your approval", "aria-label": "Waiting for your approval" })
        : conversation.active ? h("span.chatRow__spinner", { title: "Agent is working", "aria-label": "Agent is working" }) : null,
      title,
      where
        ? h("span.chatRow__where", { title: where.path },
            h("span.railProject__swatch", { class: `railSwatch--${swatchOf(where.id)}`, "aria-hidden": "true" }),
            h("span", { text: where.title }))
        : null,
      h("span.chatRow__stamp", { text: ageOf(conversation.updated_at) }),
    );
    const more = h(
      "button.chatRow__more",
      { type: "button", title: "Chat actions", "aria-label": `Actions for ${conversation.title || "New chat"}`, "aria-haspopup": "menu" },
      icon(MORE, { size: 15 }),
    );
    more.addEventListener("click", (event) => {
      event?.stopPropagation?.();
      chatMenu(more, conversation, title, tree);
    });
    const classes = [
      conversation.id === activeId ? "is-active" : "",
      conversation.active ? "is-running" : "",
      awaiting ? "is-awaiting" : "",
    ];
    const node = atDepth(h(
      "div.chatRow",
      { class: classes.join(" "), draggable: onMoveToProject ? "true" : null, dataset: { id: conversation.id } },
      main,
      more,
    ), depth);
    node.addEventListener?.("dragstart", (event) => {
      event.dataTransfer?.setData?.(DRAG_TYPE, conversation.id);
      if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
      node.classList.add("is-dragging");
    });
    node.addEventListener?.("dragend", () => node.classList.remove("is-dragging"));
    return node;
  };

  // -- a project -------------------------------------------------------------------
  const openCreate = (parent) =>
    openProjectDialog({
      mode: "create",
      parent,
      projects: store.get().projects ?? [],
      onSubmit: async ({ title, description, parent_id }) => {
        const project = parent_id
          ? await onCreateSubproject?.(parent_id, { title, description })
          : await onCreateProject?.(title, description);
        if (parent_id) toggleExpanded(parent_id, true);
        if (project?.id) toggleExpanded(project.id, true);
        rerender();
      },
    });

  const openEdit = (project) =>
    openProjectDialog({
      mode: "edit",
      project,
      projects: store.get().projects ?? [],
      onSubmit: async (patch) => {
        await onUpdateProject?.(project.id, patch);
        rerender();
      },
      onDelete: async () => {
        await onDeleteProject?.(project.id);
        rerender();
      },
    });

  const newChatIn = (project) => {
    toggleExpanded(project.id, true);
    store.set({ activeProject: project.id });
    onNewChat?.(project.id);
  };

  // What deleting a project does, in the store's terms: its sub-projects and
  // chats move up to its parent; its space goes to the trash, its memory goes.
  const confirmDelete = (project, tree) => {
    const children = (tree.children.get(project.id) ?? []).length;
    const chats = (tree.chats.get(project.id) ?? []).length;
    const parent = project.parent_id ? tree.byId.get(project.parent_id)?.title : null;
    const message =
      `Delete “${project.title}”?\n\n` +
      `Its ${plural(children, "sub-project")} and ${plural(chats, "chat")} move to ${parent ? `“${parent}”` : "the top level"}. ` +
      "Its files go to the trash for 7 days and its memory is deleted.";
    if (!confirm(message)) return;
    void Promise.resolve(onDeleteProject(project.id)).catch(() => {}).then(rerender);
  };

  const projectMenu = (anchor, project, tree) => {
    const canNest = tree.depth(project.id) + 1 < MAX_DEPTH;
    showMenu(anchor, [
      onNewChat && !project.archived && { label: "New chat here", icon: ICONS.plus, run: () => newChatIn(project) },
      canNest && onCreateSubproject && { label: "New sub-project", icon: ICONS.folder, run: () => openCreate(project) },
      { label: "Rename and describe", icon: ICONS.edit, run: () => openEdit(project) },
      { label: "Tasks of the project", icon: ICONS.check, href: `/tasks?project=${encodeURIComponent(project.id)}` },
      "-",
      onUpdateProject && {
        label: project.archived ? "Restore from archive" : "Archive",
        icon: ICONS.undo,
        run: () => void Promise.resolve(onUpdateProject(project.id, { archived: !project.archived })).catch(() => {}).then(rerender),
      },
      onDeleteProject && { label: "Delete project…", icon: ICONS.trash, danger: true, run: () => confirmDelete(project, tree) },
    ], `Actions for ${project.title}`);
  };

  const projectNode = (project, tree, state) => {
    const { activeId, activeProject, open } = state;
    const depth = tree.depth(project.id);
    const isOpen = open.has(project.id);
    const total = tree.count(project.id);
    const toggle = h(
      "button.railProject__toggle",
      {
        type: "button",
        class: isOpen ? "is-open" : "",
        "aria-label": isOpen ? `Collapse ${project.title}` : `Expand ${project.title}`,
        "aria-expanded": String(isOpen),
        on: {
          click: (event) => {
            event?.stopPropagation?.();
            toggleExpanded(project.id);
            rerender();
          },
        },
      },
      icon(ICONS.chevron, { size: 12 }),
    );
    const main = h(
      "button.railProject__main",
      {
        type: "button",
        title: project.description ? `${project.title}\n${project.description}` : `${project.title} - new chats start here`,
        on: {
          click: () => {
            // Picking a project opens it; picking the open, active one folds it.
            if (activeProject === project.id && isOpen) toggleExpanded(project.id, false);
            else toggleExpanded(project.id, true);
            store.set({ activeProject: project.id });
            rerender();
          },
        },
      },
      h("span.railProject__swatch", { class: `railSwatch--${swatchOf(project.id)}`, "aria-hidden": "true" }),
      h("span.railProject__name", { text: project.title }),
      !isOpen && tree.running(project.id) ? h("span.railProject__live", { title: "An agent is working here" }) : null,
      total ? h("span.railProject__count", { text: String(total), title: plural(total, "chat") }) : null,
    );
    const actions = h("span.railProject__actions", {});
    if (onNewChat && !project.archived) {
      actions.append(h(
        "button.railProject__act",
        { type: "button", title: `New chat in ${project.title}`, "aria-label": `New chat in ${project.title}`, on: { click: (event) => { event?.stopPropagation?.(); newChatIn(project); } } },
        icon(ICONS.plus, { size: 14 }),
      ));
    }
    const more = h(
      "button.railProject__act",
      { type: "button", title: "Project actions", "aria-label": `Actions for ${project.title}`, "aria-haspopup": "menu" },
      icon(MORE, { size: 15 }),
    );
    more.addEventListener("click", (event) => {
      event?.stopPropagation?.();
      projectMenu(more, project, tree);
    });
    actions.append(more);
    const row = atDepth(h(
      "div.railProject__row",
      { class: [activeProject === project.id ? "is-active" : "", project.archived ? "is-archived" : ""].filter(Boolean).join(" ") },
      toggle,
      main,
      actions,
    ), depth);
    dropTarget(row, project.id);
    const node = h("div.railProject", { role: "treeitem", "aria-expanded": String(isOpen), "aria-level": String(depth + 1), dataset: { id: project.id } }, row);
    if (!isOpen) return node;
    const branch = atDepth(h("div.railProject__branch", { role: "group" }), depth);
    for (const child of tree.children.get(project.id) ?? []) {
      if (child.archived && !project.archived) continue;
      branch.append(projectNode(child, tree, state));
    }
    const chats = tree.chats.get(project.id) ?? [];
    const limited = showAll.has(project.id) || chats.length <= PROJECT_PREVIEW + 1 ? chats : chats.slice(0, PROJECT_PREVIEW);
    for (const conversation of limited) branch.append(chatRow(conversation, activeId, tree, { depth: depth + 1 }));
    if (limited.length < chats.length) {
      branch.append(atDepth(h(
        "button.railProject__more",
        { type: "button", on: { click: () => { showAll.add(project.id); rerender(); } } },
        `Show ${chats.length - limited.length} more`,
      ), depth + 1));
    }
    if (!branch.childNodes.length) {
      branch.append(atDepth(h("p.railProject__empty", {},
        onNewChat && !project.archived
          ? h("button.railLink", { type: "button", on: { click: () => newChatIn(project) } }, "Start the first chat here")
          : "No chats yet",
      ), depth + 1));
    }
    node.append(branch);
    return node;
  };

  // -- sections --------------------------------------------------------------------
  const sectionHead = (name, label, count, extra = null) =>
    h(
      "div.railSection__head",
      {},
      h(
        "button.railSection__toggle",
        { type: "button", "aria-expanded": String(!folded.has(name)), on: { click: () => toggleSection(name) } },
        icon(ICONS.chevron, { size: 11, className: `icon railSection__chevron${folded.has(name) ? "" : " is-open"}` }),
        h("span", { text: label }),
        count ? h("span.railSection__count", { text: String(count) }) : null,
      ),
      extra,
    );

  const renderTree = (conversations, projects, activeId, activeProject) => {
    const tree = treeOf(projects, conversations);
    // Open: what the user opened, and the branch holding the chat on screen.
    const open = new Set(expanded);
    const current = conversations.find((conversation) => conversation.id === activeId);
    for (const id of current?.project && tree.byId.has(current.project) ? [current.project, ...tree.ancestors(current.project)] : []) open.add(id);
    const state = { activeId, activeProject, open };

    const live = (tree.children.get(null) ?? []).filter((project) => !project.archived);
    const archived = projects.filter((project) => project.archived && !(project.parent_id && tree.byId.get(project.parent_id)?.archived));
    const projectsSection = h("section.railSection", { "aria-label": "Projects" });
    const newProject = onCreateProject
      ? h("button.railSection__add", { type: "button", title: "New project", "aria-label": "New project", on: { click: () => openCreate(null) } }, icon(ICONS.plus, { size: 14 }))
      : null;
    projectsSection.append(sectionHead("projects", "Projects", live.length, newProject));
    if (!folded.has("projects")) {
      const list = h("div.railTree", { role: "tree", "aria-label": "Projects" });
      for (const project of live) list.append(projectNode(project, tree, state));
      if (!live.length && !archived.length) {
        list.append(h("p.railHint", {},
          "Projects keep related chats, their files, tasks and memory together. ",
          onCreateProject ? h("button.railLink", { type: "button", on: { click: () => openCreate(null) } }, "Create one") : null,
        ));
      }
      if (archived.length) {
        list.append(h(
          "button.railArchived",
          { type: "button", "aria-expanded": String(archivedOpen), on: { click: () => { archivedOpen = !archivedOpen; rerender(); } } },
          icon(ICONS.chevron, { size: 11, className: `icon railSection__chevron${archivedOpen ? " is-open" : ""}` }),
          h("span", { text: `Archived (${archived.length})` }),
        ));
        if (archivedOpen) for (const project of archived) list.append(projectNode(project, tree, state));
      }
      projectsSection.append(list);
    }

    const chatsSection = h("section.railSection.railSection--chats", { "aria-label": "Chats outside projects" });
    const head = sectionHead("chats", projects.length ? "Chats" : "Recent", tree.unfiled.length);
    dropTarget(head, null);
    chatsSection.append(head);
    if (!folded.has("chats")) {
      if (!tree.unfiled.length) {
        chatsSection.append(h("p.railHint", { text: conversations.length ? "Every chat is in a project." : "No chats yet." }));
      }
      for (const [label, items] of groupByDay([...tree.unfiled].sort(newest))) {
        chatsSection.append(h("div.chatGroup", {}, h("div.chatGroup__label", { text: label }), ...items.map((item) => chatRow(item, activeId, tree))));
      }
    }
    replace(container, projectsSection, chatsSection);
  };

  const renderSearch = (conversations, projects, activeId) => {
    const tree = treeOf(projects, conversations);
    const words = query.split(/\s+/).filter(Boolean);
    const hit = (text) => words.every((word) => text.toLowerCase().includes(word));
    const foundProjects = projects.filter((project) => hit(`${project.title} ${project.description ?? ""}`));
    const foundChats = conversations
      .filter((conversation) => hit(`${conversation.title ?? ""} ${conversation.routed_agent ?? ""} ${tree.path(conversation.project).join(" ")}`))
      .sort(newest);
    const out = [];
    if (foundProjects.length) {
      out.push(h("div.chatGroup__label", { text: "Projects" }));
      for (const project of foundProjects) {
        out.push(h(
          "button.railFound",
          {
            type: "button",
            on: {
              click: () => {
                for (const id of [project.id, ...tree.ancestors(project.id)]) toggleExpanded(id, true);
                store.set({ activeProject: project.id });
                searchInput.value = "";
                query = "";
                rerender();
              },
            },
          },
          h("span.railProject__swatch", { class: `railSwatch--${swatchOf(project.id)}`, "aria-hidden": "true" }),
          h("span.railFound__name", { text: project.title }),
          h("span.railFound__where", { text: tree.path(project.parent_id).join(" › ") || plural(tree.count(project.id), "chat") }),
        ));
      }
    }
    if (foundChats.length) {
      out.push(h("div.chatGroup__label", { text: "Chats" }));
      for (const conversation of foundChats) {
        const project = conversation.project && tree.byId.get(conversation.project);
        const where = project ? { id: project.id, title: project.title, path: tree.path(project.id).join(" › ") } : null;
        out.push(chatRow(conversation, activeId, tree, { where }));
      }
    }
    if (!out.length) out.push(h("p.railHint", { text: "Nothing matches that search." }));
    replace(container, ...out);
  };

  // What the rail shows, as text: a poll that brings the same chats back must
  // not rebuild it - rebuilt rows restart their spinners and drop the hover.
  let shown = null;
  const signature = (conversations, projects, activeId, activeProject) =>
    JSON.stringify([
      activeId, activeProject, query,
      conversations.map((c) => [c.id, c.open_id, c.title, c.project, c.active, c.updated_at, c.message_count, c.routed_agent, c.agent_key, c.workspace]),
      projects.map((p) => [p.id, p.title, p.parent_id, p.archived, p.description]),
      // The age shown ("5m") moves with the clock.
      Math.floor(Date.now() / 60_000),
    ]);

  const render = ({ conversations = [], contextId, rootId, projects = [], activeProject = null }, { force = false } = {}) => {
    if (renaming) return;
    const activeId = rootId ?? contextId;
    // A chat nobody wrote in is not listed, unless it is the one on screen:
    // abandoned "New chat"s would line the rail (the server drops them later).
    conversations = conversations.filter(
      (conversation) => conversation.message_count || conversation.active || conversation.id === activeId,
    );
    const now = signature(conversations, projects, activeId, activeProject);
    if (!force && now === shown) return;
    shown = now;
    if (query) renderSearch(conversations, projects, activeId);
    else renderTree(conversations, projects, activeId, activeProject);
  };

  searchInput.addEventListener("input", () => {
    query = searchInput.value.trim().toLowerCase();
    rerender();
  });
  searchInput.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && searchInput.value) {
      searchInput.value = "";
      query = "";
      rerender();
    }
  });

  store.watch(["conversations", "contextId", "rootId", "projects", "activeProject"], render);
  return { render: rerender };
}
