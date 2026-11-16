/**
 * Which view the map page shows: Office, Space, Layers, Timeline or Map.
 *
 * The choice is the reader's, and it is kept in `localStorage[grid.view]`. But a
 * stored choice is not an order to download a renderer: a narrow screen always
 * opens in Layers, and the 3D renderers (`office.js`/`space.js`, WebGL2) are
 * imported only when their view is really about to be drawn. Everything that decides
 * this takes its environment as an argument, so the rule can be pinned in Node
 * without a browser: `resolveInitialView({stored, width, reducedMotion, ...})`.
 *
 * The scene names an object the way the server does - `project:<id>`,
 * `collection:<project>:<name>`, `task:<id>`, `chat:<id>`, `file:<path from
 * the project's space>`, `message:<chat>:<id>`, `execution:<chat>:<run>`,
 * `operation:<chat>:<id>` - while the inspector names it by kind
 * (`{kind:"chat", id}`, `{kind:"message", chatId, id}`, ...). The two mappings
 * here are the only bridge between them, in both directions. A legacy
 * `process:`/`context:` name is not a canonical entity and maps to nothing.
 */

import { parseFilesUrl } from "./explorer.js";

/** Where the reader's choice is kept. */
export const VIEW_KEY = "grid.view";

/** The five views, the only values this module ever writes. */
export const VIEWS = ["office", "space", "layers", "timeline", "map"];

/** How each view is called on the page. */
export const VIEW_LABELS = { office: "Office", space: "Space", layers: "Layers", timeline: "Timeline", map: "Map" };

/** At or below this width the page opens in Layers, whatever was stored. */
export const MOBILE_MAX_WIDTH = 720;

const text = (value) => String(value ?? "");

/** Whether a viewport is the narrow, touch-first one. */
export function isMobileWidth(width) {
  const pixels = Number(width);
  return Number.isFinite(pixels) && pixels > 0 && pixels <= MOBILE_MAX_WIDTH;
}

/** The stored choice, or "" when there is none (or storage refuses to answer). */
export function readViewPreference(storage) {
  try {
    const value = text(storage?.getItem?.(VIEW_KEY));
    return VIEWS.includes(value) ? value : "";
  } catch {
    return "";
  }
}

/** Keep the choice; a browser that refuses storage is not an error here. */
export function writeViewPreference(storage, view) {
  try {
    if (VIEWS.includes(text(view))) storage?.setItem?.(VIEW_KEY, text(view));
  } catch {
    /* private mode, a full disk: the page still draws, it just forgets */
  }
  return view;
}

/** Put the view on the document, where the stylesheet reads it. */
export function applyView(doc, view) {
  const root = doc?.documentElement;
  if (root?.dataset) root.dataset.view = VIEWS.includes(text(view)) ? text(view) : "map";
  return view;
}

/**
 * Whether this browser can draw the 3D Space at all. A throwaway canvas asks
 * for a WebGL2 context - no renderer module is imported, no scene is built -
 * and the context is dropped at once.
 */
export function probeWebGL(doc = globalThis.document) {
  try {
    const canvas = doc?.createElement?.("canvas");
    if (typeof canvas?.getContext !== "function") return false;
    const gl = canvas.getContext("webgl2");
    if (!gl) return false;
    gl.getExtension?.("WEBGL_lose_context")?.loseContext?.();
    return true;
  } catch {
    return false;
  }
}

/**
 * The view the page opens in, and why.
 *
 * - A viewport of 720px or less opens in Layers, always: a phone never waits
 *   for a 3D module, and an explicit Office/Space pick can still enable it later.
 * - A device asking for reduced motion opens in Layers: the Space is available,
 *   but it would open as a still frame, so Layers is the clearer default.
 * - Otherwise the stored choice is honoured; a stored Office/Space that cannot be
 *   drawn (no WebGL2) becomes Layers with an honest note instead of a blank.
 * - With nothing stored, a WebGL2 desktop opens in Office: the room is the
 *   project surface, while Layers remains the always-readable alternative. A
 *   stored choice is kept.
 * - With no viewport at all (Node, tests) the Map is the safe fallback.
 *
 * @returns {{view: string, source: string, note: string}}
 */
export function resolveInitialView({
  stored = "",
  width = 0,
  reducedMotion = false,
  finePointer = null,
  webgl = null,
} = {}) {
  const pixels = Number(width);
  if (!Number.isFinite(pixels) || pixels <= 0) {
    return { view: "map", source: "unknown", note: "No viewport is known here, so the Map is drawn." };
  }
  if (isMobileWidth(pixels)) {
    return {
      view: "layers",
      source: "mobile",
      note: "A narrow screen opens in Layers; Space is loaded only if you choose it.",
    };
  }
  if (reducedMotion) {
    return {
      view: "layers",
      source: "reduced-motion",
      note: "This device asks for reduced motion, so Layers is the default; Space is still available.",
    };
  }
  const storedView = VIEWS.includes(text(stored)) ? text(stored) : "";
  if ((storedView === "space" || storedView === "office") && !webgl) {
    return {
      view: "layers",
      source: "stored",
      note: `The stored ${VIEW_LABELS[storedView]} view needs WebGL2, which this browser did not offer.`,
    };
  }
  if (storedView) return { view: storedView, source: "stored", note: "" };
  return {
    view: webgl ? "office" : "layers",
    source: "default",
    note: webgl ? "The project opens in Office. Layers is the accessible document alternative." : "Office needs WebGL2; this browser opens in Layers.",
  };
}

/** The root of the project's space, as `files_url` names it ("project"). */
export function spaceRoot(map) {
  return parseFilesUrl(map?.files_url)?.root ?? "";
}

/** Trim a path the way the files page does: no leading, trailing or doubled slashes. */
function cleanPath(value) {
  return text(value).trim().replace(/^\/+|\/+$/g, "").replace(/\/{2,}/g, "/");
}

/**
 * The scene id a selection names - the id the Space and Layers renderers use.
 * A path outside the project's space keeps its own path, so nothing is invented.
 */
export function sceneIdOfSelection(selection, { map = null, root = "" } = {}) {
  const kind = text(selection?.kind);
  // A selection that already carries the exact scene id keeps it: it was made
  // by clicking a scene node, so no parsing may shift it to a neighbour.
  if (text(selection?.sceneId)) return text(selection.sceneId);
  if (kind === "task") {
    const id = text(selection.id).trim();
    return id ? `${kind}:${id}` : "";
  }
  if (kind === "project" || kind === "collection") {
    const id = text(selection.id).trim();
    return id ? `${kind}:${id}` : "";
  }
  if (kind === "chat" || kind === "process") {
    const id = text(selection.id).trim();
    return id ? `chat:${id}` : "";
  }
  if (kind === "message") {
    const chatId = text(selection.chatId ?? selection.chat ?? selection.id).trim();
    const id = text(selection.messageId ?? selection.message ?? selection.nodeId ?? selection.id).trim();
    return chatId && id ? `message:${chatId}:${id}` : "";
  }
  if (kind === "execution") {
    const chatId = text(selection.chatId ?? selection.chat).trim();
    const id = text(selection.executionId ?? selection.run ?? selection.nodeId ?? selection.id).trim();
    if (!chatId || !id) return "";
    return selection.sub === true ? `execution:sub:${chatId}:${id}` : `execution:${chatId}:${id}`;
  }
  if (kind === "operation") {
    const chatId = text(selection.chatId ?? selection.chat).trim();
    const id = text(selection.operationId ?? selection.nodeId ?? selection.id).trim();
    return chatId && id ? `operation:${chatId}:${id}` : "";
  }
  if (kind === "file" || kind === "dir") {
    const base = cleanPath(root || spaceRoot(map));
    let path = cleanPath(selection.path);
    if (!path) return "";
    if (base && path === base) return "";
    if (base && path.startsWith(`${base}/`)) path = path.slice(base.length + 1);
    return path ? `file:${path}` : "";
  }
  return "";
}

/**
 * The chat whose detail a selection needs: the chat itself, the chat a message
 * or execution belongs to, or the chat that owns an operation. "" for a
 * selection that needs no chat detail at all (a task, a file, a project).
 */
export function chatIdOfSelection(selection) {
  const kind = text(selection?.kind);
  if (kind === "chat" || kind === "process") return text(selection.id).trim();
  if (kind === "message" || kind === "execution" || kind === "operation") {
    return text(selection.chatId ?? selection.chat).trim();
  }
  return "";
}

/**
 * The selection a scene id names. A file node becomes a folder when the answer
 * says it is one, and its path is the real one in the project's space
 * ("project/docs"), the path the inspector and the files page both understand.
 * An activity step selects the chat it belongs to: there is no step panel.
 */
export function selectionOfSceneId(sceneId, { scene = null, map = null, root = "" } = {}) {
  const id = text(sceneId);
  const at = id.indexOf(":");
  if (at <= 0) return null;
  const kind = id.slice(0, at);
  const rest = id.slice(at + 1);
  if (!rest) return null;
  if (kind === "project" || kind === "collection" || kind === "task") return { kind, id: rest };
  /** Split `<chat>:<local id>` once; every local id keeps its own colons. */
  const splitOnce = (value) => {
    const colon = value.indexOf(":");
    if (colon <= 0 || colon >= value.length - 1) return null;
    return [value.slice(0, colon), value.slice(colon + 1)];
  };
  if (kind === "chat") return { kind: "chat", id: rest, sceneId: id };
  if (kind === "message") {
    const pair = splitOnce(rest);
    return pair ? { kind: "message", chatId: pair[0], id: pair[1], messageId: pair[1], sceneId: id } : null;
  }
  if (kind === "execution") {
    if (rest.startsWith("sub:")) {
      const pair = splitOnce(rest.slice(4));
      return pair
        ? { kind: "execution", chatId: pair[0], id: pair[1], executionId: pair[1], sub: true, sceneId: id }
        : null;
    }
    const pair = splitOnce(rest);
    return pair
      ? { kind: "execution", chatId: pair[0], id: pair[1], executionId: pair[1], sub: false, sceneId: id }
      : null;
  }
  if (kind === "operation") {
    const pair = splitOnce(rest);
    return pair ? { kind: "operation", chatId: pair[0], id: pair[1], operationId: pair[1], sceneId: id } : null;
  }
  const node = Array.isArray(scene?.nodes) ? scene.nodes.find((entry) => entry.id === id) : null;
  if (kind === "file") {
    const base = cleanPath(root || spaceRoot(map));
    const type = node?.item?.kind === "dir" ? "dir" : "file";
    return { kind: type, path: base ? `${base}/${rest}` : rest, sceneId: id };
  }
  return null;
}

/**
 * The `<select id="grid-view">` of the header.
 *
 * It only reports a change; the page decides whether the view can really be
 * drawn and calls `set()` with what is on screen, so a failed Space pick snaps
 * the control back to the view that answered.
 */
export function createViewSwitch({ select = null, onChange = null, view = "map" } = {}) {
  let current = VIEWS.includes(text(view)) ? text(view) : "map";

  function paint() {
    if (select) {
      select.value = current;
      if (select.dataset) select.dataset.view = current;
      if ("title" in select) select.title = `${VIEW_LABELS[current]} view`;
    }
    return current;
  }

  const handle = (event) => {
    const next = text(event?.target?.value ?? select?.value);
    if (!VIEWS.includes(next)) {
      paint();
      return;
    }
    onChange?.(next, { explicit: true });
  };

  select?.addEventListener?.("change", handle);
  paint();

  return {
    select,
    get view() {
      return current;
    },
    set(next) {
      current = VIEWS.includes(text(next)) ? text(next) : "map";
      return paint();
    },
    destroy() {
      select?.removeEventListener?.("change", handle);
    },
  };
}
