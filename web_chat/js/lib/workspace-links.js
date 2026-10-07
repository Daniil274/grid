/**
 * Workspace files of the chat on screen.
 *
 * A chat may work in a directory of its own (web_chat/space.py,
 * choose_workspace), so a workspace file is named by its path and its chat:
 * links to /api/workspace/files/<path> carry `?context=<chat id>`, and the
 * server opens the file in that chat's directory.
 */

const WORKSPACE_FILES = "/api/workspace/files/";

let chat = null;

/** The chat whose files links on screen now point at. */
export function setWorkspaceChat(contextId) {
  chat = contextId || null;
}

/** *url* scoped to the chat on screen when it is a workspace file; else as it was. */
export function scopedWorkspaceUrl(url) {
  if (!chat || typeof url !== "string" || !url.startsWith(WORKSPACE_FILES)) return url;
  const scoped = new URL(url, location.origin);
  scoped.searchParams.set("context", chat);
  return scoped.pathname + scoped.search;
}

/** Scope every workspace-file link and picture under *root*. */
export function scopeWorkspaceLinks(root) {
  if (!chat) return;
  for (const link of root.querySelectorAll(`a[href^="${WORKSPACE_FILES}"]`)) {
    link.setAttribute("href", scopedWorkspaceUrl(link.getAttribute("href")));
  }
  for (const image of root.querySelectorAll(`img[src^="${WORKSPACE_FILES}"]`)) {
    image.setAttribute("src", scopedWorkspaceUrl(image.getAttribute("src")));
  }
}
