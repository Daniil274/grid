/**
 * Conversation rail: search, the open-chat highlight, a spinner on chats whose
 * agent is still working, and rename / delete / move-to-project per row.
 *
 * Two groupings share one row: by date (the default), and by project - a tree
 * of groups up to three levels deep with a "No project" group at the bottom.
 * The list re-renders from the store on every change. That is affordable here
 * (tens of rows, not thousands) and removes a whole class of bugs where the
 * rail drifts out of sync with the conversation actually on screen. The one
 * exception is a row being renamed: re-rendering would steal the input's focus.
 */

import { h, icon, replace } from "../lib/dom.js";
import { dayBucket, plural, shortStamp } from "../lib/format.js";
import { ICONS } from "./icons.js";
import { openProjectDialog } from "./project-dialog.js";

const BUCKET_ORDER = ["Today", "Yesterday", "This week", "This month", "Earlier"];

function matches(conversation, query, projectTitle = "") {
  if (!query) return true;
  const haystack = `${conversation.title ?? ""} ${conversation.routed_agent ?? ""} ${projectTitle}`.toLowerCase();
  return haystack.includes(query);
}

function groupByDay(conversations) {
  const groups = new Map(BUCKET_ORDER.map((name) => [name, []]));
  for (const conversation of conversations) groups.get(dayBucket(conversation.updated_at)).push(conversation);
  return [...groups].filter(([, items]) => items.length);
}

/** Projects by parent id, with a bucket of roots under `null`. */
function projectChildren(projects) {
  const children = new Map([[null, []]]);
  for (const project of projects) {
    const key = project.parent_id ?? null;
    if (!children.has(key)) children.set(key, []);
    children.get(key).push(project);
  }
  return children;
}

/** The project and every project under it, by id. */
function subtreeIds(projects, rootId) {
  const ids = new Set([rootId]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const project of projects) {
      if (project.parent_id && ids.has(project.parent_id) && !ids.has(project.id)) {
        ids.add(project.id);
        grew = true;
      }
    }
  }
  return ids;
}

export function createConversationList({
  container,
  searchInput,
  modeToggle,
  store,
  onSelect,
  onRename,
  onDelete,
  onMoveToProject,
  onCreateProject,
  onCreateSubproject,
  onUpdateProject,
  onDeleteProject,
}) {
  let query = "";
  let renaming = null; // id of the row whose title is an input right now
  let moving = null; // id of the row whose "move to project" popover is open
  let projectMenu = null; // id of the project whose actions menu is open
  let mode = "date"; // "date" | "projects"
  const collapsed = new Set(); // project ids whose chats are hidden

  const actionButton = (title, glyph, handler) =>
    h("button.iconBtn.iconBtn--xs", {
      type: "button",
      title,
      "aria-label": title,
      on: { click: (event) => { event?.stopPropagation?.(); handler(); } },
    }, icon(glyph, { size: 13 }));

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
      render(store.get());
    };
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") void finish(true);
      else if (event.key === "Escape") void finish(false);
    });
    input.addEventListener("blur", () => void finish(true));
    input.addEventListener("click", (event) => event?.stopPropagation?.());
    titleNode.replaceWith(input);
    input.focus();
    input.select();
  };

  const moveTo = async (conversation, projectId) => {
    moving = null;
    if (onMoveToProject) await onMoveToProject(conversation.id, projectId);
    render(store.get());
  };

  const movePopover = (conversation, projects) => {
    const list = h("div.movePop__list", {});
    list.append(h("button.movePop__item", {
      type: "button",
      on: { click: () => void moveTo(conversation, null) },
    }, "No project"));
    for (const project of projects) {
      list.append(h("button.movePop__item", {
        type: "button",
        on: { click: () => void moveTo(conversation, project.id) },
      }, h("span.movePop__title", { text: project.title })));
    }
    let create = null;
    if (onCreateProject) {
      const input = h("input.movePop__new", { type: "text", placeholder: "New project\u2026", maxlength: 120, "aria-label": "New project name" });
      const button = h("button.movePop__create", {
        type: "button",
        on: {
          click: async () => {
            const title = input.value.trim();
            if (!title) return;
            moving = null;
            const project = await onCreateProject(title);
            if (project?.id && onMoveToProject) await onMoveToProject(conversation.id, project.id);
            render(store.get());
          },
        },
      }, "Create");
      create = h("div.movePop__createRow", {}, input, button);
    }
    return h("div.movePop", {
      role: "menu",
      on: { click: (event) => event?.stopPropagation?.() },
    },
      h("p.movePop__head", { text: "Move to project" }),
      list,
      create,
    );
  };

  // -- the project's own actions (menu, dialog) ---------------------------
  const openCreate = (parent) =>
    openProjectDialog({
      mode: "create",
      parent,
      projects: store.get().projects ?? [],
      onSubmit: async ({ title, description, parent_id }) => {
        projectMenu = null;
        if (parent_id) await onCreateSubproject?.(parent_id, { title, description });
        else await onCreateProject?.(title, description);
        render(store.get());
      },
    });

  const openEdit = (project) =>
    openProjectDialog({
      mode: "edit",
      project,
      projects: store.get().projects ?? [],
      onSubmit: async (patch) => {
        projectMenu = null;
        await onUpdateProject?.(project.id, patch);
        render(store.get());
      },
      onDelete: async () => {
        projectMenu = null;
        await onDeleteProject?.(project.id);
        render(store.get());
      },
    });

  const archiveProject = async (project, archived) => {
    projectMenu = null;
    try {
      await onUpdateProject?.(project.id, { archived });
    } catch {
      /* the callback already reported it */
    }
    render(store.get());
  };

  const projectMenuNode = (project, depth) => {
    // A project at the deepest level the store allows (3) takes no sub-project.
    const canNest = depth + 2 <= 3;
    // A handler closes the menu, then acts, so a dialog opens over a clean rail.
    const item = (label, glyph, handler) =>
      h(
        "button.projectMenu__item",
        {
          type: "button",
          role: "menuitem",
          on: { click: () => { projectMenu = null; handler(); } },
        },
        icon(glyph, { size: 14 }),
        h("span", { text: label }),
      );
    return h(
      "div.projectMenu",
      { role: "menu", "aria-label": `Actions for ${project.title}`, on: { click: (event) => event?.stopPropagation?.() } },
      canNest ? item("New sub-project", ICONS.plus, () => openCreate(project)) : null,
      item("Edit project", ICONS.edit, () => openEdit(project)),
      h(
        "a.projectMenu__item",
        { href: `/tasks?project=${encodeURIComponent(project.id)}`, title: `Tasks of ${project.title}` },
        icon(ICONS.check, { size: 14 }),
        h("span", { text: "Open tasks" }),
      ),
      item(project.archived ? "Unarchive" : "Archive", ICONS.folder, () => void archiveProject(project, !project.archived)),
    );
  };

  const row = (conversation, activeId, projects) => {
    const title = h("span.chatRow__title", { text: conversation.title || "New chat" });
    const main = h(
      "button.chatRow__main",
      { type: "button", on: { click: () => onSelect(conversation.open_id ?? conversation.id) } },
      h(
        "span.chatRow__head",
        {},
        conversation.active ? h("span.chatRow__spinner", { title: "Agent is working", "aria-label": "Agent is working" }) : null,
        title,
      ),
      h(
        "span.chatRow__meta",
        {},
        h("span", { text: conversation.routed_agent || conversation.agent_key || "auto" }),
        h("span", { text: "·" }),
        h("span", { text: plural(conversation.message_count || 0, "msg") }),
        // A chat working in a directory of its own says which.
        conversation.workspace
          ? h("span.chatRow__dir", { text: `· ${conversation.workspace.split(/[\\/]/).filter(Boolean).pop()}`, title: conversation.workspace })
          : null,
        h("span.chatRow__stamp", { text: shortStamp(conversation.updated_at) }),
      ),
    );
    const actions = h(
      "span.chatRow__actions",
      {},
      actionButton("Move to project", ICONS.folder, () => {
        moving = moving === conversation.id ? null : conversation.id;
        render(store.get());
      }),
      // This chat's plan, on the tracker page: the tasks its turns planned.
      h("a.iconBtn.iconBtn--xs", {
        href: `/tasks?conversation=${encodeURIComponent(conversation.id)}`,
        title: "Open tasks planned in this chat",
        "aria-label": "Open tasks planned in this chat",
        on: { click: (event) => event?.stopPropagation?.() },
      }, icon(ICONS.check, { size: 13 })),
      actionButton("Rename chat", ICONS.edit, () => startRename(conversation, title)),
      actionButton("Delete chat", ICONS.trash, () => {
        if (confirm(`Delete "${conversation.title || "New chat"}"? This cannot be undone.`)) onDelete(conversation.id);
      }),
    );
    const classes = [conversation.id === activeId ? "is-active" : "", conversation.active ? "is-running" : ""];
    const node = h("div.chatRow", { class: classes.join(" ") }, main, actions);
    if (moving === conversation.id) node.append(movePopover(conversation, projects));
    return node;
  };

  const renderDate = (visible, activeId, projects) => {
    if (!visible.length) {
      replace(container, h("p.rail__empty", { text: query ? "Nothing matches that search." : "No conversations yet." }));
      return;
    }
    replace(
      container,
      groupByDay(visible).map(([label, items]) =>
        h("div.chatGroup", {}, h("div.chatGroup__label", { text: label }), ...items.map((item) => row(item, activeId, projects))),
      ),
    );
  };

  const renderProjects = (visible, projects, activeId, activeProject) => {
    const children = projectChildren(projects);
    const known = new Set(projects.map((project) => project.id));
    const byProject = new Map();
    const unfiled = [];
    for (const conversation of visible) {
      const projectId = conversation.project;
      if (projectId && known.has(projectId)) {
        if (!byProject.has(projectId)) byProject.set(projectId, []);
        byProject.get(projectId).push(conversation);
      } else {
        unfiled.push(conversation);
      }
    }

    const node = (project, depth) => {
      const isCollapsed = collapsed.has(project.id);
      const items = byProject.get(project.id) ?? [];
      const childNodes = (children.get(project.id) ?? []).map((child) => node(child, depth + 1)).filter(Boolean);
      // While searching, a project stays only if it, or something under it, matches.
      if (query && !project.title.toLowerCase().includes(query) && !items.length && !childNodes.length) return null;
      const chevron = h("button.projectGroup__chevron", {
        type: "button",
        class: isCollapsed ? "" : "is-open",
        "aria-label": isCollapsed ? "Expand project" : "Collapse project",
        on: {
          click: (event) => {
            event?.stopPropagation?.();
            if (isCollapsed) collapsed.delete(project.id);
            else collapsed.add(project.id);
            render(store.get());
          },
        },
      }, icon(ICONS.chevron, { size: 13 }));
      const isActive = activeProject === project.id;
      const head = h("div.projectGroup__head", {
        class: [isActive ? "is-active" : "", project.archived ? "is-archived" : ""].filter(Boolean).join(" "),
        style: { "--depth": String(depth) },
      },
        chevron,
        h("button.projectGroup__title", {
          type: "button",
          title: `Filter chats to ${project.title} and start new ones in it`,
          on: { click: () => { projectMenu = null; store.set({ activeProject: project.id }); } },
        },
          h("span.projectGroup__name", { text: project.title }),
          project.archived ? h("span.projectGroup__tag", { text: "archived" }) : null,
          items.length ? h("span.projectGroup__count", { text: String(items.length) }) : null,
        ),
        h("button.projectGroup__menu", {
          type: "button",
          class: projectMenu === project.id ? "is-open" : "",
          title: "Project actions",
          "aria-label": `Actions for ${project.title}`,
          "aria-haspopup": "menu",
          on: {
            click: (event) => {
              event?.stopPropagation?.();
              projectMenu = projectMenu === project.id ? null : project.id;
              render(store.get());
            },
          },
        }, icon(ICONS.sliders, { size: 14 })),
      );
      if (projectMenu === project.id) head.append(projectMenuNode(project, depth));
      const group = h("div.projectGroup", {}, head);
      if (!isCollapsed) {
        for (const conversation of items) group.append(row(conversation, activeId, projects));
        for (const child of childNodes) group.append(child);
      }
      return group;
    };

    // Focused on one project: show that project and everything under it.
    const byId = new Map(projects.map((project) => [project.id, project]));
    const focused = activeProject && byId.has(activeProject) ? byId.get(activeProject) : null;
    const roots = focused ? [focused] : (children.get(null) ?? []);
    const groups = roots.map((project) => node(project, 0)).filter(Boolean);
    // The unfiled bucket stays visible whenever there is a tree to file into,
    // so "start outside any project" is always one click away.
    if (projects.length || unfiled.length) {
      groups.push(h("div.chatGroup.projectGroup--none", {},
        h("button.chatGroup__label.chatGroup__label--btn", {
          type: "button",
          class: activeProject == null ? "is-active" : "",
          title: "Start new chats outside any project",
          on: { click: () => store.set({ activeProject: null }) },
          text: "No project",
        }),
        ...unfiled.map((conversation) => row(conversation, activeId, projects)),
      ));
    }
    if (!groups.length) {
      groups.push(h("p.rail__empty", { text: query ? "Nothing matches that search." : "No projects yet." }));
    }
    if (onCreateProject && !query) {
      groups.push(h("button.projectGroup__new", {
        type: "button",
        on: { click: () => openCreate(null) },
      }, icon(ICONS.plus, { size: 14 }), h("span", { text: "New project" })));
    }
    replace(container, groups);
  };

  const render = ({ conversations, contextId, rootId, projects = [], activeProject = null }) => {
    if (renaming) return;
    const index = new Map(projects.map((project) => [project.id, project]));
    let visible = conversations.filter((conversation) => matches(conversation, query, index.get(conversation.project)?.title));
    // The active project is also the rail's filter: only it and its sub-projects.
    if (activeProject && index.has(activeProject)) {
      const scope = subtreeIds(projects, activeProject);
      visible = visible.filter((conversation) => conversation.project && scope.has(conversation.project));
    }
    const activeId = rootId ?? contextId;
    if (mode === "projects") renderProjects(visible, projects, activeId, activeProject);
    else renderDate(visible, activeId, projects);
  };

  const modeButton = (label, value) =>
    h("button.railModes__btn", {
      type: "button",
      class: mode === value ? "is-active" : "",
      on: { click: () => { mode = value; paintModes(); render(store.get()); } },
    }, label);

  const paintModes = () => {
    if (modeToggle) replace(modeToggle, modeButton("Projects", "projects"), modeButton("By date", "date"));
  };

  searchInput.addEventListener("input", () => {
    query = searchInput.value.trim().toLowerCase();
    render(store.get());
  });

  // A click anywhere but the open project menu (or its button) closes it.
  document.addEventListener?.("click", () => {
    if (projectMenu == null) return;
    projectMenu = null;
    render(store.get());
  });

  paintModes();
  store.watch(["conversations", "contextId", "rootId", "projects", "activeProject"], render);
}
