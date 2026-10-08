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
}) {
  let query = "";
  let renaming = null; // id of the row whose title is an input right now
  let moving = null; // id of the row whose "move to project" popover is open
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
      const head = h("div.projectGroup__head", {
        class: activeProject === project.id ? "is-active" : "",
        style: { "--depth": String(depth) },
      },
        chevron,
        h("button.projectGroup__title", {
          type: "button",
          title: `Chat in ${project.title}`,
          on: { click: () => store.set({ activeProject: project.id }) },
        },
          h("span.projectGroup__name", { text: project.title }),
          items.length ? h("span.projectGroup__count", { text: String(items.length) }) : null,
        ),
      );
      const group = h("div.projectGroup", {}, head);
      if (!isCollapsed) {
        for (const conversation of items) group.append(row(conversation, activeId, projects));
        for (const child of childNodes) group.append(child);
      }
      return group;
    };

    const groups = (children.get(null) ?? []).map((project) => node(project, 0)).filter(Boolean);
    if (unfiled.length) {
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
      replace(container, h("p.rail__empty", { text: query ? "Nothing matches that search." : "No projects yet." }));
      return;
    }
    replace(container, groups);
  };

  const render = ({ conversations, contextId, rootId, projects = [], activeProject = null }) => {
    if (renaming) return;
    const index = new Map(projects.map((project) => [project.id, project]));
    const visible = conversations.filter((conversation) => matches(conversation, query, index.get(conversation.project)?.title));
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

  paintModes();
  store.watch(["conversations", "contextId", "rootId", "projects", "activeProject"], render);
}
