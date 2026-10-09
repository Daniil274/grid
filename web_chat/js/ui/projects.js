/**
 * The active project: the one the rail is filtered to and the one a new chat
 * is created in (web_chat/js/chat.js reads it for `project_id`).
 *
 * The choice is remembered in localStorage and mirrored into `?project=<id>`,
 * so a reload - or a link from the tracker page - lands on the same project.
 * Clearing it (the rail's "No project") forgets it again.
 */

import { h, icon, replace } from "../lib/dom.js";
import { ICONS } from "./icons.js";

/** Where the last active project is remembered, across reloads. */
export const ACTIVE_PROJECT_KEY = "grid:activeProject";

/** The project id remembered from the last visit, or null. */
export function storedProject() {
  try {
    return localStorage.getItem(ACTIVE_PROJECT_KEY) || null;
  } catch {
    return null; // private mode: nothing would survive a reload anyway
  }
}

/** Remember the active project; null forgets it. */
export function rememberProject(id) {
  try {
    if (id) localStorage.setItem(ACTIVE_PROJECT_KEY, id);
    else localStorage.removeItem(ACTIVE_PROJECT_KEY);
  } catch {
    /* private mode: the choice just does not survive a reload */
  }
}

/** Point the address bar at the project, keeping the other query keys. */
export function syncProjectUrl(id) {
  if (typeof history === "undefined" || !history.replaceState || typeof location === "undefined") return;
  const params = new URLSearchParams(location.search);
  if (id) params.set("project", id);
  else params.delete("project");
  const query = params.toString();
  history.replaceState(null, "", query ? `${location.pathname}?${query}` : location.pathname);
}

/** Select a project (`null` = no project). The store's watchers persist it. */
export function setActiveProject(store, id) {
  store.set({ activeProject: id || null });
}

/** Mirror the active project into localStorage and the URL as it changes. */
export function trackActiveProject(store) {
  store.watch(["activeProject"], ({ activeProject }) => {
    rememberProject(activeProject);
    syncProjectUrl(activeProject);
  });
}

/**
 * The "New chat will be created in: <project>" line above the rail's list.
 *
 * @param {{node: HTMLElement, store: object}} options
 * @returns {{render: Function}}
 */
export function createProjectContext({ node, store }) {
  const render = ({ activeProject, projects = [] }) => {
    const project = projects.find((candidate) => candidate.id === activeProject);
    if (!project) {
      node.hidden = true;
      replace(node, "");
      return;
    }
    node.hidden = false;
    replace(
      node,
      h("span.railProject__icon", { "aria-hidden": "true" }, icon(ICONS.folder, { size: 13 })),
      h(
        "span.railProject__text",
        {},
        "New chat will be created in: ",
        h("strong", { text: project.title }),
      ),
      h(
        "button.railProject__clear",
        {
          type: "button",
          title: "Start new chats outside any project",
          "aria-label": "Clear the active project",
          on: { click: () => setActiveProject(store, null) },
        },
        icon(ICONS.close, { size: 12 }),
      ),
    );
  };
  store.watch(["activeProject", "projects"], render);
  return { render };
}
