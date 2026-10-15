/**
 * The files page (web_chat/explorer_api.py): the workspace the agents work in,
 * to browse, read, edit and arrange.
 *
 * Folders on the left, the open folder's entries in the middle, the chosen
 * file on the right - shown as code, rendered markdown or a picture, and
 * edited in place. Files are uploaded by button or by dropping them; entries
 * are renamed, moved (by name, by dragging onto a folder, or cut and paste),
 * copied, deleted and downloaded, one or many at a time. A folder downloads as
 * a zip.
 *
 * `?context=<chat>` shows the directory that chat works in (a chat may work in
 * a directory of its own). A chat with a space of its own lists its project's
 * and its user's spaces at its top, as folders named `project` and `user`:
 * moving a file there hands it on beyond the chat. `?path=` and `?file=` are the open folder and file,
 * so a view is a link and Back retraces it. Everything goes through the API,
 * which never follows a link the agents may have planted: links are listed as
 * such and only renamed, moved or deleted as themselves.
 */

import { $, $$, h, icon, replace, debounce } from "../lib/dom.js";
import { ICONS } from "../ui/icons.js";
import { toast, copyText } from "../ui/toast.js";
import { createThemeToggle } from "../ui/theme.js";
import { highlight } from "../lib/highlight.js";
import { languageFor } from "../lib/languages.js";
import { renderMarkdown } from "../lib/markdown.js";

const params = new URLSearchParams(location.search);
const context = params.get("context") || "";

const RAISE = {
  up: "M12 19V5M6 11l6-6 6 6",
  refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
  link: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1",
  download: "M12 4v11M7 10l5 5 5-5M5 20h14",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  tree: "M4 6h6M4 12h6M4 18h6M14 6h6M14 12h6M14 18h6",
};

// -- API -----------------------------------------------------------------------
function scoped(url, extra = {}) {
  const target = new URL(url, location.origin);
  if (context) target.searchParams.set("context", context);
  for (const [key, value] of Object.entries(extra)) {
    if (value !== undefined && value !== null) target.searchParams.set(key, value);
  }
  return target.pathname + target.search;
}

async function call(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  const payload = await response.json().catch(() => null);
  if (response.status === 401) {
    location.assign("/login");
    throw new Error("Signed out");
  }
  if (!response.ok) {
    const detail = payload?.detail;
    throw new Error(typeof detail === "string" ? detail : `${options.method || "GET"} failed (${response.status})`);
  }
  return payload;
}

const send = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

const api = {
  list: (path) => call(scoped("/api/workspace/tree", { path })),
  read: (path) => call(scoped("/api/workspace/text", { path })),
  write: (body) => call(scoped("/api/workspace/text"), send("PUT", body)),
  mkdir: (path) => call(scoped("/api/workspace/mkdir"), send("POST", { path })),
  move: (source, target) => call(scoped("/api/workspace/move"), send("POST", { source, target })),
  copy: (source, target) => call(scoped("/api/workspace/copy"), send("POST", { source, target })),
  remove: (path) => call(scoped("/api/workspace/entry", { path }), { method: "DELETE" }),
  search: (q, path) => call(scoped("/api/workspace/search", { q, path })),
  fileUrl: (path, inline = false) =>
    scoped(`/api/workspace/files/${path.split("/").map(encodeURIComponent).join("/")}`, inline ? { inline: "1" } : {}),
  zipUrl: (path) => scoped("/api/workspace/archive", { path }),
};

// -- paths ---------------------------------------------------------------------
const parentOf = (path) => path.split("/").slice(0, -1).join("/");
const nameOf = (path) => path.split("/").pop() || "";
const joinPath = (folder, name) => (folder ? `${folder}/${name}` : name);
const inside = (path, folder) => path === folder || path.startsWith(`${folder}/`);

/** A path typed by the user: from the workspace when it starts with "/", else from *base*. */
function typedPath(text, base) {
  const raw = String(text ?? "").trim();
  const parts = (raw.startsWith("/") ? raw : joinPath(base, raw)).split("/");
  const out = [];
  for (const part of parts) {
    if (!part || part === ".") continue;
    if (part === "..") out.pop();
    else out.push(part);
  }
  return out.join("/");
}

function fileSize(bytes) {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`;
}

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
const timeFormat = new Intl.DateTimeFormat(undefined, { timeStyle: "short" });
function when(seconds) {
  if (!seconds) return "";
  const date = new Date(seconds * 1000);
  const today = new Date();
  return date.toDateString() === today.toDateString() ? `Today ${timeFormat.format(date)}` : dateFormat.format(date);
}

const IMAGE = /\.(png|jpe?g|gif|webp)$/i;
const MARKDOWN = /\.(md|markdown|mdx)$/i;
const extension = (name) => (name.includes(".") && !name.startsWith(".") ? name.split(".").pop().toLowerCase() : "");

// -- state ---------------------------------------------------------------------
const state = {
  folder: params.get("path") || "",
  entries: [],
  selected: new Set(),
  anchor: null,
  sort: readStored("grid.files.sort", { key: "name", dir: 1 }),
  showHidden: readStored("grid.files.hidden", false),
  search: null, // {query, matches, complete} while a search is shown
  clipboard: null, // {mode: "cut"|"copy", paths: []}
  file: null, // the open file: {path, info, text, editing, dirty, view}
  expanded: new Set([""]),
  tree: new Map(), // folder path -> its subfolder names
  labels: new Map(), // path of another space (project, user) -> its title
  rootLabel: "Workspace",
};

/** Remember the titles of the spaces a listing shows (entries with `space`). */
function learnSpaces(entries) {
  for (const entry of entries) if (entry.space) state.labels.set(entry.path, entry.label || entry.name);
}

const labelOf = (path) => state.labels.get(path) ?? nameOf(path);

function readStored(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw == null ? fallback : JSON.parse(raw);
  } catch {
    return fallback;
  }
}

function store(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* a private window: the choice lasts this visit */
  }
}

const visible = (entry) => state.showHidden || !entry.name.startsWith(".");

// -- status line ---------------------------------------------------------------
const statusLine = $("#status");
function status(text, tone = "") {
  statusLine.textContent = text;
  statusLine.dataset.tone = tone;
}

// -- URL -----------------------------------------------------------------------
function syncUrl({ push = false } = {}) {
  const next = new URLSearchParams();
  if (context) next.set("context", context);
  if (state.folder) next.set("path", state.folder);
  if (state.file) next.set("file", state.file.path);
  const url = `${location.pathname}${next.toString() ? `?${next}` : ""}`;
  if (url === location.pathname + location.search) return;
  history[push ? "pushState" : "replaceState"](null, "", url);
}

// -- dialogs -------------------------------------------------------------------
/**
 * A small modal: a message, optionally one text field. Resolves to the field's
 * value (or true without a field) when confirmed, null when cancelled.
 */
function ask({ title, message = "", value = null, confirm = "OK", danger = false, select = null }) {
  return new Promise((resolve) => {
    const field =
      value === null
        ? null
        : h("input.filesDialog__input", { type: "text", value, spellcheck: false, autocomplete: "off", "aria-label": title });
    const ok = h(`button.btn.${danger ? "btn--danger" : "btn--primary"}`, { type: "submit", text: confirm });
    const cancel = h("button.btn.btn--ghost", { type: "button", text: "Cancel" });
    const form = h(
      "form.filesDialog__form",
      { method: "dialog" },
      h("h2.filesDialog__title", { text: title }),
      message ? h("p.filesDialog__message", { text: message }) : null,
      field,
      h("div.filesDialog__actions", {}, cancel, ok),
    );
    const dialog = h("dialog.filesDialog", { "aria-label": title }, form);
    let result = null;
    cancel.addEventListener("click", () => dialog.close());
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (field && !field.value.trim()) return field.focus();
      result = field ? field.value : true;
      dialog.close();
    });
    dialog.addEventListener("close", () => {
      dialog.remove();
      resolve(result);
    });
    document.body.append(dialog);
    dialog.showModal();
    if (field) {
      field.focus();
      // A rename selects the name without its extension, as file managers do.
      const [start, end] = select ?? [0, field.value.length];
      field.setSelectionRange(start, end);
    } else {
      ok.focus();
    }
  });
}

function stemRange(name) {
  const dot = name.lastIndexOf(".");
  return [0, dot > 0 ? dot : name.length];
}

// -- the folder tree -----------------------------------------------------------
const treeRoot = $("#tree");

async function loadTree(path) {
  try {
    const listing = await api.list(path);
    learnSpaces(listing.entries);
    state.tree.set(
      path,
      listing.entries.filter((entry) => entry.kind === "dir").map((entry) => entry.name),
    );
  } catch {
    state.tree.delete(path);
    state.expanded.delete(path);
  }
}

async function revealInTree(path) {
  const parts = path ? path.split("/") : [];
  let current = "";
  const needed = [""];
  for (const part of parts) {
    current = joinPath(current, part);
    needed.push(current);
  }
  for (const folder of needed.slice(0, -1).concat(needed.length === 1 ? [] : [])) state.expanded.add(folder);
  await Promise.all(needed.filter((folder) => !state.tree.has(folder)).map(loadTree));
}

function renderTree() {
  const node = (path, label, depth) => {
    const children = state.tree.get(path);
    const open = state.expanded.has(path);
    const hasChildren = children === undefined || children.some((name) => state.showHidden || !name.startsWith("."));
    const toggle = h(
      "button.filesTree__toggle",
      {
        type: "button",
        "aria-label": open ? `Collapse ${label}` : `Expand ${label}`,
        "aria-expanded": String(open),
        tabindex: "-1",
        class: hasChildren ? "" : "is-empty",
      },
      icon(ICONS.chevron, { size: 13 }),
    );
    const row = h(
      "div.filesTree__row",
      {
        role: "treeitem",
        tabindex: "0",
        "aria-selected": String(path === state.folder),
        "aria-expanded": hasChildren ? String(open) : null,
        dataset: { path },
        style: { paddingLeft: `${6 + depth * 14}px` },
      },
      toggle,
      icon(ICONS.folder, { size: 15, className: "icon filesTree__icon" }),
      h("span.filesTree__label", { text: label, title: path || "The workspace" }),
    );
    toggle.addEventListener("click", async (event) => {
      event.stopPropagation();
      if (open) state.expanded.delete(path);
      else {
        state.expanded.add(path);
        if (!state.tree.has(path)) await loadTree(path);
      }
      renderTree();
    });
    row.addEventListener("click", () => navigate(path, { push: true }));
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter") navigate(path, { push: true });
      if (event.key === "ArrowRight" && !open) toggle.click();
      if (event.key === "ArrowLeft" && open) toggle.click();
    });
    dropTarget(row, path);
    const group = [row];
    if (open && children) {
      for (const name of children) {
        if (!state.showHidden && name.startsWith(".")) continue;
        group.push(...node(joinPath(path, name), labelOf(joinPath(path, name)), depth + 1));
      }
    }
    return group;
  };
  replace(treeRoot, h("div.filesTree__list", { role: "tree", "aria-label": "Folders" }, node("", state.rootLabel, 0)));
}

// -- the listing ---------------------------------------------------------------
const rows = $("#rows");

function sorted(entries) {
  const { key, dir } = state.sort;
  return [...entries].sort((a, b) => {
    if ((a.kind === "dir") !== (b.kind === "dir")) return a.kind === "dir" ? -1 : 1;
    let order = 0;
    if (key === "size") order = (a.size ?? -1) - (b.size ?? -1);
    else if (key === "mtime") order = (a.mtime ?? 0) - (b.mtime ?? 0);
    if (!order) order = a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" });
    return order * dir;
  });
}

function shownEntries() {
  if (state.search) return state.search.matches.filter(visible);
  return sorted(state.entries.filter(visible));
}

function entryIcon(entry) {
  if (entry.space === "user") return icon(ICONS.users, { size: 17, className: "icon filesIcon filesIcon--space" });
  if (entry.space) return icon(ICONS.folder, { size: 17, className: "icon filesIcon filesIcon--space" });
  if (entry.kind === "dir") return icon(ICONS.folder, { size: 17, className: "icon filesIcon filesIcon--dir" });
  if (entry.kind === "link") return icon(RAISE.link, { size: 17, className: "icon filesIcon filesIcon--link" });
  return icon(ICONS.file, { size: 17, className: "icon filesIcon" });
}

function renderListing() {
  const entries = shownEntries();
  const cut = state.clipboard?.mode === "cut" ? new Set(state.clipboard.paths) : new Set();
  replace(
    rows,
    entries.map((entry) => {
      const checked = state.selected.has(entry.path);
      const check = h("input", { type: "checkbox", "aria-label": `Select ${entry.name}`, tabindex: "-1", hidden: Boolean(entry.space) });
      check.checked = checked;
      const ext = entry.kind === "file" ? extension(entry.name) : "";
      const name = h(
        "td.filesTable__name",
        {},
        entryIcon(entry),
        h("span.filesName", { text: entry.label || entry.name, title: entry.space ? `/${entry.path} - shared beyond this chat` : entry.path }),
        ext ? h("span.filesExt", { text: ext }) : null,
        entry.kind === "link" ? h("span.filesLinkTarget", { text: `→ ${entry.target ?? "?"}` }) : null,
        state.search ? h("span.filesWhere", { text: parentOf(entry.path) || "/" }) : null,
      );
      const row = h(
        "tr.filesRow",
        {
          tabindex: "0",
          draggable: entry.space ? "false" : "true",
          "aria-selected": String(checked),
          class: [checked ? "is-selected" : "", cut.has(entry.path) ? "is-cut" : "", state.file?.path === entry.path ? "is-open" : "", entry.space ? "is-space" : ""].join(" "),
          dataset: { path: entry.path, kind: entry.kind },
        },
        h("td.filesTable__check", {}, check),
        name,
        h("td.filesTable__size", { text: entry.kind === "dir" ? "—" : fileSize(entry.size) }),
        h("td.filesTable__date", { text: when(entry.mtime), title: entry.mtime ? new Date(entry.mtime * 1000).toLocaleString() : "" }),
      );
      check.addEventListener("click", (event) => {
        event.stopPropagation();
        toggleSelected(entry.path, check.checked);
      });
      row.addEventListener("click", (event) => clickRow(event, entry));
      row.addEventListener("dblclick", () => openEntry(entry));
      row.addEventListener("contextmenu", (event) => {
        event.preventDefault();
        if (entry.space) {
          spaceMenu(event, entry);
          return;
        }
        if (!state.selected.has(entry.path)) selectOnly(entry.path);
        entryMenu(event, entry);
      });
      row.addEventListener("dragstart", (event) => {
        if (!state.selected.has(entry.path)) selectOnly(entry.path);
        event.dataTransfer.setData("application/x-grid-paths", JSON.stringify([...state.selected]));
        event.dataTransfer.effectAllowed = "copyMove";
      });
      if (entry.kind === "dir") dropTarget(row, entry.path);
      return row;
    }),
  );
  if (!entries.length) {
    rows.append(
      h(
        "tr.filesEmpty",
        {},
        h("td", {
          colspan: "4",
          text: state.search
            ? `Nothing named like “${state.search.query}” here.`
            : "This folder is empty. Drop files here, or make a file or a folder.",
        }),
      ),
    );
  }
  const all = $("#select-all");
  all.checked = entries.length > 0 && entries.every((entry) => state.selected.has(entry.path));
  all.indeterminate = !all.checked && entries.some((entry) => state.selected.has(entry.path));
  for (const button of $$(".filesSort")) {
    const active = button.dataset.sort === state.sort.key;
    button.dataset.dir = active ? (state.sort.dir > 0 ? "asc" : "desc") : "";
    button.closest("th").setAttribute("aria-sort", active ? (state.sort.dir > 0 ? "ascending" : "descending") : "none");
  }
  renderSelectionBar();
}

function renderCrumbs() {
  const crumbs = $("#crumbs");
  const parts = state.folder ? state.folder.split("/") : [];
  const items = [{ label: state.rootLabel, path: "" }];
  parts.forEach((part, index) => {
    const path = parts.slice(0, index + 1).join("/");
    items.push({ label: labelOf(path), path });
  });
  replace(
    crumbs,
    items.map((item, index) => {
      const last = index === items.length - 1;
      const crumb = h(last ? "span.filesCrumb.is-current" : "button.filesCrumb", {
        type: last ? null : "button",
        text: item.label,
        "aria-current": last ? "location" : null,
      });
      if (!last) {
        crumb.addEventListener("click", () => navigate(item.path, { push: true }));
        dropTarget(crumb, item.path);
      }
      return [index ? h("span.filesCrumbSep", { text: "/" }) : null, crumb];
    }),
  );
  $("#go-up").disabled = !state.folder;
}

function renderSelectionBar() {
  const bar = $("#selection-bar");
  bar.hidden = state.selected.size === 0;
  $("#selection-count").textContent = `${state.selected.size} selected`;
  const paste = $("#paste");
  paste.hidden = !state.clipboard;
  if (state.clipboard) {
    const count = state.clipboard.paths.length;
    paste.textContent = `Paste ${count} ${count === 1 ? "item" : "items"}${state.clipboard.mode === "cut" ? " (move)" : ""}`;
  }
}

// -- selection -----------------------------------------------------------------
function selectOnly(path) {
  state.selected = new Set(path && !state.labels.has(path) ? [path] : []);
  state.anchor = path;
  renderListing();
}

function toggleSelected(path, on = !state.selected.has(path)) {
  if (state.labels.has(path)) return;
  if (on) state.selected.add(path);
  else state.selected.delete(path);
  state.anchor = path;
  renderListing();
}

function clickRow(event, entry) {
  if (event.shiftKey && state.anchor) {
    const paths = shownEntries().map((item) => item.path);
    const [from, to] = [paths.indexOf(state.anchor), paths.indexOf(entry.path)].sort((a, b) => a - b);
    if (from >= 0) {
      if (!(event.ctrlKey || event.metaKey)) state.selected.clear();
      for (const path of paths.slice(from, to + 1)) if (!state.labels.has(path)) state.selected.add(path);
      renderListing();
      return;
    }
  }
  if (event.ctrlKey || event.metaKey) return toggleSelected(entry.path);
  // A plain click opens: a folder is entered, a file shown beside the list.
  selectOnly(entry.path);
  openEntry(entry);
}

function selectedEntries() {
  const known = new Map([...state.entries, ...(state.search?.matches ?? [])].map((entry) => [entry.path, entry]));
  return [...state.selected].map((path) => known.get(path) ?? { path, name: nameOf(path), kind: "file" });
}

// -- navigation ----------------------------------------------------------------
let loading = 0;

async function navigate(folder, { push = false, keepFile = false } = {}) {
  const ticket = ++loading;
  status("Loading…");
  try {
    const listing = await api.list(folder);
    if (ticket !== loading) return;
    state.folder = listing.path;
    state.entries = listing.entries;
    state.rootLabel = listing.root_label || "Workspace";
    learnSpaces(listing.entries);
    state.search = null;
    $("#search").value = "";
    state.selected.clear();
    state.tree.set(
      state.folder,
      listing.entries.filter((entry) => entry.kind === "dir").map((entry) => entry.name),
    );
    $("#workspace-label").textContent = listing.workspace || "";
    await revealInTree(state.folder);
    if (!keepFile && state.file && parentOf(state.file.path) !== state.folder) await closeFile({ quiet: true });
    status(summary());
    renderCrumbs();
    renderTree();
    renderListing();
    syncUrl({ push });
  } catch (error) {
    if (ticket !== loading) return;
    status(error.message, "error");
    if (folder) {
      toast(`${error.message} Showing the workspace instead.`, { tone: "error" });
      return navigate("", { push: false });
    }
  }
}

function summary() {
  const shown = state.entries.filter(visible);
  const folders = shown.filter((entry) => entry.kind === "dir").length;
  const files = shown.length - folders;
  const bytes = shown.reduce((sum, entry) => sum + (entry.kind === "file" ? entry.size ?? 0 : 0), 0);
  const hidden = state.entries.length - shown.length;
  return [
    `${folders} ${folders === 1 ? "folder" : "folders"}`,
    `${files} ${files === 1 ? "file" : "files"}${files ? ` · ${fileSize(bytes)}` : ""}`,
    hidden ? `${hidden} hidden` : "",
  ]
    .filter(Boolean)
    .join(" · ");
}

async function refresh() {
  // Folders known to the tree are re-read too: the agents change them meanwhile.
  const known = [...state.tree.keys()].filter((path) => state.expanded.has(path) && path !== state.folder);
  await Promise.all(known.map(loadTree));
  await navigate(state.folder, { keepFile: true });
  if (state.file && !state.file.dirty) await openFile(state.file.path, { quiet: true });
}

function openEntry(entry) {
  if (entry.kind === "dir") return navigate(entry.path, { push: true });
  if (entry.kind === "link") {
    toast(`A link to ${entry.target ?? "somewhere"}: links are not followed here.`);
    return;
  }
  if (state.search && parentOf(entry.path) !== state.folder) {
    // A search result elsewhere: go to its folder, then open it.
    return navigate(parentOf(entry.path), { push: true, keepFile: true }).then(() => openFile(entry.path, { push: true }));
  }
  return openFile(entry.path, { push: true });
}

// -- the open file -------------------------------------------------------------
const preview = $("#preview");

async function confirmLeave() {
  if (!state.file?.dirty) return true;
  return Boolean(
    await ask({
      title: "Discard changes?",
      message: `${nameOf(state.file.path)} has unsaved changes.`,
      confirm: "Discard",
      danger: true,
    }),
  );
}

async function openFile(path, { push = false, quiet = false } = {}) {
  if (state.file?.path !== path && !(await confirmLeave())) return;
  const name = nameOf(path);
  state.file = { path, info: null, text: null, editing: false, dirty: false, view: MARKDOWN.test(name) ? "rendered" : "source" };
  preview.hidden = false;
  document.body.classList.add("has-preview");
  if (!quiet) replace(preview, previewHeader(), h("div.filesPreview__body", {}, h("p.filesPreview__note", { text: "Opening…" })));
  syncUrl({ push });
  renderListing();
  if (IMAGE.test(name)) {
    state.file.image = true;
    renderPreview();
    return;
  }
  try {
    const info = await api.read(path);
    if (state.file?.path !== path) return;
    state.file.info = info;
    state.file.text = info.text;
    renderPreview();
  } catch (error) {
    if (state.file?.path !== path) return;
    replace(preview, previewHeader(), h("div.filesPreview__body", {}, h("p.filesPreview__note.is-error", { text: error.message })));
  }
}

async function closeFile({ quiet = false } = {}) {
  if (!quiet && !(await confirmLeave())) return false;
  state.file = null;
  preview.hidden = true;
  document.body.classList.remove("has-preview");
  replace(preview);
  syncUrl();
  renderListing();
  return true;
}

function previewHeader() {
  const file = state.file;
  const name = nameOf(file.path);
  const button = (label, iconPath, handler, extra = {}) => {
    const node = h("button.iconBtn", { type: "button", title: label, "aria-label": label, ...extra }, icon(iconPath, { size: 16 }));
    node.addEventListener("click", handler);
    return node;
  };
  const actions = [];
  const editable = file.info && !file.info.binary && !file.info.truncated && file.text !== null;
  if (file.editing) {
    const save = h("button.btn.btn--primary", { type: "button", text: "Save", title: "Save (Ctrl+S)" });
    save.disabled = !file.dirty;
    save.addEventListener("click", saveFile);
    const cancel = h("button.btn.btn--ghost", { type: "button", text: "Cancel" });
    cancel.addEventListener("click", async () => {
      if (file.dirty && !(await confirmLeave())) return;
      file.editing = false;
      file.dirty = false;
      file.text = file.info.text;
      renderPreview();
    });
    actions.push(cancel, save);
  } else {
    if (file.info && MARKDOWN.test(name) && file.text !== null) {
      const toggle = h("button.btn.btn--ghost", { type: "button", text: file.view === "rendered" ? "Source" : "Preview" });
      toggle.addEventListener("click", () => {
        file.view = file.view === "rendered" ? "source" : "rendered";
        renderPreview();
      });
      actions.push(toggle);
    }
    if (editable) {
      const edit = h("button.btn.btn--ghost", { type: "button", text: "Edit", title: "Edit (E)" });
      edit.addEventListener("click", startEditing);
      actions.push(edit);
    }
    actions.push(
      button("Download", RAISE.download, () => download([file.path])),
      button("Copy path", ICONS.copy, () => copyText(file.path, "Path copied")),
      button("Rename or move", ICONS.edit, () => renameEntry({ path: file.path, name, kind: "file" })),
      button("Delete", ICONS.trash, () => removeEntries([{ path: file.path, name, kind: "file" }])),
    );
  }
  actions.push(button("Close", ICONS.close, () => closeFile()));
  const meta = file.info
    ? [fileSize(file.info.size), file.info.truncated ? "shown in part" : "", languageFor({ name }) || ""].filter(Boolean).join(" · ")
    : "";
  return h(
    "header.filesPreview__head",
    {},
    h(
      "div.filesPreview__title",
      {},
      h("strong", { text: name, title: file.path }),
      file.dirty ? h("span.filesDirty", { text: "● unsaved", "aria-label": "Unsaved changes" }) : null,
      meta ? h("span.filesPreview__meta", { text: meta }) : null,
    ),
    h("div.filesPreview__actions", {}, actions),
  );
}

function renderPreview() {
  const file = state.file;
  if (!file) return;
  let body;
  if (file.image) {
    const image = h("img.filesPreview__image", { src: api.fileUrl(file.path, true), alt: nameOf(file.path) });
    image.addEventListener("error", () => image.replaceWith(h("p.filesPreview__note", { text: "This picture cannot be shown." })));
    body = h("div.filesPreview__body.filesPreview__body--image", {}, image);
  } else if (!file.info) {
    body = h("div.filesPreview__body", {}, h("p.filesPreview__note", { text: "Opening…" }));
  } else if (file.info.binary) {
    const button = h("button.btn.btn--primary", { type: "button", text: "Download" });
    button.addEventListener("click", () => download([file.path]));
    body = h(
      "div.filesPreview__body.filesPreview__body--center",
      {},
      icon(ICONS.file, { size: 40, className: "icon filesPreview__bigIcon" }),
      h("p.filesPreview__note", { text: `A binary file of ${fileSize(file.info.size)}: it is not shown here.` }),
      button,
    );
  } else if (file.editing) {
    const area = h("textarea.filesEditor", { spellcheck: false, autocomplete: "off", "aria-label": `Contents of ${nameOf(file.path)}` });
    area.value = file.text;
    area.addEventListener("input", () => {
      file.text = area.value;
      if (!file.dirty) {
        file.dirty = true;
        refreshHeader();
      }
    });
    area.addEventListener("keydown", (event) => editorKeys(event, area));
    body = h("div.filesPreview__body.filesPreview__body--editor", {}, area);
    requestAnimationFrame(() => area.focus());
  } else if (file.view === "rendered" && MARKDOWN.test(file.path)) {
    body = h("div.filesPreview__body", {}, h("article.filesMarkdown", { html: renderMarkdown(file.text) }));
  } else {
    body = h("div.filesPreview__body", {}, codeView(file.text, languageFor({ name: nameOf(file.path) })));
  }
  const notes = file.info?.truncated
    ? h("p.filesPreview__banner", { text: `Only the first ${fileSize(file.text?.length ?? 0)} are shown; download the file for all of it.` })
    : null;
  replace(preview, previewHeader(), notes, body);
}

function refreshHeader() {
  const head = $(".filesPreview__head", preview);
  if (head) head.replaceWith(previewHeader());
}

function codeView(text, language) {
  const lines = text.split("\n");
  if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
  const gutter = h("pre.filesCode__gutter", { "aria-hidden": "true", text: lines.map((_, index) => index + 1).join("\n") });
  const code = h("code", { html: highlight(text, language) || "&nbsp;" });
  return h("div.filesCode", {}, gutter, h("pre.filesCode__text", {}, code));
}

function startEditing() {
  if (!state.file?.info || state.file.info.binary) return;
  state.file.editing = true;
  renderPreview();
}

function editorKeys(event, area) {
  if (event.key === "Tab" && !event.ctrlKey && !event.altKey && !event.metaKey) {
    event.preventDefault();
    const indent = /\t/.test(area.value) || /\.(go|makefile)$/i.test(state.file.path) || nameOf(state.file.path) === "Makefile" ? "\t" : "    ";
    const { selectionStart: start, selectionEnd: end } = area;
    if (event.shiftKey) return;
    area.setRangeText(indent, start, end, "end");
    area.dispatchEvent(new Event("input"));
  }
  if (event.key === "Escape" && !state.file.dirty) {
    state.file.editing = false;
    renderPreview();
  }
}

async function saveFile() {
  const file = state.file;
  if (!file?.editing || !file.dirty) return;
  try {
    const saved = await api.write({ path: file.path, text: file.text, mtime: file.info.mtime });
    file.info = { ...file.info, size: saved.size, mtime: saved.mtime, text: file.text };
    file.dirty = false;
    refreshHeader();
    toast("Saved", { tone: "success" });
    const listing = await api.list(state.folder).catch(() => null);
    if (listing) {
      state.entries = listing.entries;
      renderListing();
    }
  } catch (error) {
    if (/changed since/.test(error.message)) {
      const copyName = await ask({
        title: "The file changed on disk",
        message: "An agent or another tab changed it since you opened it. Save your version as a new file?",
        value: copyNameFor(nameOf(file.path)),
        confirm: "Save as",
      });
      if (copyName) {
        const target = typedPath(copyName, parentOf(file.path));
        try {
          await api.write({ path: target, text: file.text, create: true });
          file.dirty = false;
          await navigate(state.folder, { keepFile: true });
          await openFile(target, { push: true });
          toast(`Saved as ${nameOf(target)}`, { tone: "success" });
        } catch (inner) {
          toast(inner.message, { tone: "error" });
        }
      }
      return;
    }
    toast(error.message, { tone: "error" });
  }
}

function copyNameFor(name, taken = new Set()) {
  const dot = name.lastIndexOf(".");
  const [stem, ext] = dot > 0 ? [name.slice(0, dot), name.slice(dot)] : [name, ""];
  let candidate = `${stem} copy${ext}`;
  for (let number = 2; taken.has(candidate); number += 1) candidate = `${stem} copy ${number}${ext}`;
  return candidate;
}

// -- operations ----------------------------------------------------------------
async function makeFolder() {
  const name = await ask({ title: "New folder", message: `In ${state.folder || "the workspace"}.`, value: "New folder", confirm: "Create" });
  if (!name) return;
  try {
    const made = await api.mkdir(typedPath(name, state.folder));
    state.tree.delete(parentOf(made.path));
    await loadTree(parentOf(made.path));
    await navigate(state.folder);
    selectOnly(made.path);
  } catch (error) {
    toast(error.message, { tone: "error" });
  }
}

async function makeFile() {
  const name = await ask({ title: "New file", message: `In ${state.folder || "the workspace"}.`, value: "untitled.md", confirm: "Create", select: [0, 8] });
  if (!name) return;
  try {
    const made = await api.write({ path: typedPath(name, state.folder), text: "", create: true });
    await navigate(parentOf(made.path), { keepFile: true });
    await openFile(made.path, { push: true });
    startEditing();
  } catch (error) {
    toast(error.message, { tone: "error" });
  }
}

async function renameEntry(entry) {
  const value = await ask({
    title: entry.kind === "dir" ? "Rename folder" : "Rename",
    message: "A new name, or a path to move it to (from the workspace when it starts with /).",
    value: entry.name,
    confirm: "Rename",
    select: entry.kind === "dir" ? null : stemRange(entry.name),
  });
  if (!value || value === entry.name) return;
  const target = typedPath(value, parentOf(entry.path));
  await moveEntries([entry.path], null, { renameTo: target });
}

/** Move *paths* into *folder*, or one path to *renameTo*. */
async function moveEntries(paths, folder, { renameTo = null } = {}) {
  const moved = [];
  for (const path of paths) {
    const target = renameTo ?? joinPath(folder, nameOf(path));
    if (target === path) continue;
    if (folder !== null && inside(folder, path)) {
      toast(`${nameOf(path)} cannot be moved into itself.`, { tone: "error" });
      continue;
    }
    try {
      await api.move(path, target);
      moved.push([path, target]);
    } catch (error) {
      toast(`${nameOf(path)}: ${error.message}`, { tone: "error" });
    }
  }
  if (!moved.length) return;
  for (const [from, to] of moved) {
    if (state.file && inside(state.file.path, from)) state.file.path = to + state.file.path.slice(from.length);
    for (const key of [...state.tree.keys()]) if (inside(key, from)) state.tree.delete(key);
    state.tree.delete(parentOf(from));
    state.tree.delete(parentOf(to));
  }
  toast(moved.length === 1 ? `Moved to ${moved[0][1]}` : `${moved.length} items moved`, { tone: "success" });
  await refreshAfterChange();
  if (state.file) {
    renderPreview();
    syncUrl();
  }
}

async function copyEntries(paths, folder) {
  let copied = 0;
  for (const path of paths) {
    const names = new Set((await api.list(folder).catch(() => ({ entries: [] }))).entries.map((entry) => entry.name));
    let name = nameOf(path);
    if (names.has(name)) name = copyNameFor(name, names);
    try {
      status(`Copying ${nameOf(path)}…`);
      await api.copy(path, joinPath(folder, name));
      copied += 1;
    } catch (error) {
      toast(`${nameOf(path)}: ${error.message}`, { tone: "error" });
    }
  }
  if (copied) toast(copied === 1 ? "Copied" : `${copied} items copied`, { tone: "success" });
  state.tree.delete(folder);
  await refreshAfterChange();
}

async function removeEntries(entries) {
  if (!entries.length) return;
  const folders = entries.filter((entry) => entry.kind === "dir").length;
  const ok = await ask({
    title: entries.length === 1 ? `Delete ${entries[0].name}?` : `Delete ${entries.length} items?`,
    message: folders
      ? "Folders are deleted with everything in them. This cannot be undone."
      : "This cannot be undone.",
    confirm: "Delete",
    danger: true,
  });
  if (!ok) return;
  let removed = 0;
  for (const entry of entries) {
    try {
      await api.remove(entry.path);
      removed += 1;
      state.selected.delete(entry.path);
      if (state.file && inside(state.file.path, entry.path)) await closeFile({ quiet: true });
      for (const key of [...state.tree.keys()]) if (inside(key, entry.path)) state.tree.delete(key);
      state.tree.delete(parentOf(entry.path));
    } catch (error) {
      toast(`${entry.name}: ${error.message}`, { tone: "error" });
    }
  }
  if (removed) toast(removed === 1 ? "Deleted" : `${removed} items deleted`, { tone: "success" });
  if (state.folder && entries.some((entry) => inside(state.folder, entry.path))) state.folder = parentOf(entries[0].path);
  await refreshAfterChange();
}

async function refreshAfterChange() {
  const missing = [...state.expanded].filter((path) => !state.tree.has(path));
  await Promise.all(missing.map(loadTree));
  const query = state.search?.query;
  await navigate(state.folder, { keepFile: true });
  if (query) await runSearch(query);
}

function download(paths) {
  const known = new Map(state.entries.map((entry) => [entry.path, entry]));
  paths.forEach((path, index) => {
    const entry = known.get(path);
    const url = entry?.kind === "dir" ? api.zipUrl(path) : api.fileUrl(path);
    // Several downloads at once: spaced, as browsers drop rapid ones.
    setTimeout(() => {
      const link = h("a", { href: url, download: "" });
      document.body.append(link);
      link.click();
      link.remove();
    }, index * 350);
  });
}

function setClipboard(mode, paths) {
  if (!paths.length) return;
  state.clipboard = { mode, paths: [...paths] };
  toast(`${paths.length === 1 ? nameOf(paths[0]) : `${paths.length} items`} ${mode === "cut" ? "cut" : "copied"} - paste in any folder`);
  renderListing();
}

async function paste(folder = state.folder) {
  const clip = state.clipboard;
  if (!clip) return;
  if (clip.mode === "cut") {
    state.clipboard = null;
    await moveEntries(clip.paths, folder);
  } else {
    await copyEntries(clip.paths, folder);
  }
  renderSelectionBar();
}

async function moveTo(paths) {
  const value = await ask({
    title: paths.length === 1 ? `Move ${nameOf(paths[0])}` : `Move ${paths.length} items`,
    message: "To the folder (a path from the workspace; empty for the workspace itself).",
    value: `/${state.folder}`,
    confirm: "Move",
  });
  if (value === null) return;
  await moveEntries(paths, typedPath(value, ""));
}

// -- upload --------------------------------------------------------------------
function upload(files, folder = state.folder) {
  const list = [...files];
  if (!list.length) return;
  const body = new FormData();
  for (const file of list) body.append("files", file);
  const request = new XMLHttpRequest();
  request.open("POST", scoped("/api/workspace/uploads", { dir: folder }));
  request.upload.addEventListener("progress", (event) => {
    if (event.lengthComputable) status(`Uploading ${list.length} ${list.length === 1 ? "file" : "files"}… ${Math.round((event.loaded / event.total) * 100)}%`);
  });
  request.addEventListener("load", async () => {
    let payload = null;
    try {
      payload = JSON.parse(request.responseText);
    } catch {
      /* not JSON: shown by status below */
    }
    if (request.status === 401) return location.assign("/login");
    if (request.status >= 400) {
      toast(payload?.detail || `Upload failed (${request.status})`, { tone: "error" });
      status(summary());
      return;
    }
    const saved = payload?.files ?? [];
    toast(saved.length === 1 ? `Uploaded ${saved[0].name}` : `${saved.length} files uploaded`, { tone: "success" });
    await navigate(state.folder, { keepFile: true });
    state.selected = new Set(saved.map((file) => file.path).filter((path) => parentOf(path) === state.folder));
    renderListing();
  });
  request.addEventListener("error", () => {
    toast("The upload did not reach the server.", { tone: "error" });
    status(summary());
  });
  status(`Uploading ${list.length} ${list.length === 1 ? "file" : "files"}…`);
  request.send(body);
}

// -- drag and drop -------------------------------------------------------------
const draggedPaths = (event) => {
  try {
    return JSON.parse(event.dataTransfer.getData("application/x-grid-paths") || "[]");
  } catch {
    return [];
  }
};
const carries = (event, type) => [...(event.dataTransfer?.types ?? [])].includes(type);

/** *node* takes entries dragged from the list (moved; copied with Ctrl) and files from the desktop (uploaded). */
function dropTarget(node, folder) {
  node.addEventListener("dragover", (event) => {
    if (!carries(event, "application/x-grid-paths") && !carries(event, "Files")) return;
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = carries(event, "Files") || event.ctrlKey || event.altKey ? "copy" : "move";
    node.classList.add("is-dropTarget");
  });
  node.addEventListener("dragleave", () => node.classList.remove("is-dropTarget"));
  node.addEventListener("drop", (event) => {
    node.classList.remove("is-dropTarget");
    hideDropHint();
    if (carries(event, "Files")) {
      event.preventDefault();
      event.stopPropagation();
      upload(event.dataTransfer.files, folder);
      return;
    }
    const paths = draggedPaths(event).filter((path) => path !== folder);
    if (!paths.length) return;
    event.preventDefault();
    event.stopPropagation();
    if (event.ctrlKey || event.altKey) copyEntries(paths, folder);
    else moveEntries(paths, folder);
  });
}

const dropZone = $("#drop-zone");
const dropHint = $("#drop-hint");
let dragDepth = 0;
function hideDropHint() {
  dragDepth = 0;
  dropHint.hidden = true;
}
dropZone.addEventListener("dragenter", (event) => {
  if (!carries(event, "Files")) return;
  dragDepth += 1;
  dropHint.hidden = false;
});
dropZone.addEventListener("dragleave", () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) dropHint.hidden = true;
});
dropZone.addEventListener("dragover", (event) => {
  if (carries(event, "Files")) event.preventDefault();
});
dropZone.addEventListener("drop", (event) => {
  hideDropHint();
  if (!carries(event, "Files")) return;
  event.preventDefault();
  upload(event.dataTransfer.files, state.folder);
});

// -- context menu --------------------------------------------------------------
let menu = null;
function closeMenu() {
  menu?.remove();
  menu = null;
}

function showMenu(event, items) {
  closeMenu();
  menu = h(
    "div.filesMenu",
    { role: "menu" },
    items.filter(Boolean).map((item) => {
      if (item === "-") return h("div.filesMenu__sep", { role: "separator" });
      const button = h(
        "button.filesMenu__item",
        { type: "button", role: "menuitem", class: item.danger ? "is-danger" : "" },
        h("span", { text: item.label }),
        item.key ? h("kbd", { text: item.key }) : null,
      );
      button.addEventListener("click", () => {
        closeMenu();
        item.run();
      });
      return button;
    }),
  );
  document.body.append(menu);
  const { innerWidth, innerHeight } = window;
  const rect = menu.getBoundingClientRect();
  menu.style.left = `${Math.min(event.clientX, innerWidth - rect.width - 8)}px`;
  menu.style.top = `${Math.min(event.clientY, innerHeight - rect.height - 8)}px`;
  menu.querySelector("button")?.focus();
}

function entryMenu(event, entry) {
  const many = state.selected.size > 1;
  const chosen = selectedEntries();
  const paths = chosen.map((item) => item.path);
  showMenu(event, [
    !many && { label: entry.kind === "dir" ? "Open folder" : "Open", key: "Enter", run: () => openEntry(entry) },
    !many && entry.kind === "file" && { label: "Edit", key: "E", run: () => openFile(entry.path, { push: true }).then(startEditing) },
    { label: many ? `Download ${paths.length} items` : entry.kind === "dir" ? "Download as .zip" : "Download", run: () => download(paths) },
    "-",
    { label: "Cut", key: "Ctrl+X", run: () => setClipboard("cut", paths) },
    { label: "Copy", key: "Ctrl+C", run: () => setClipboard("copy", paths) },
    entry.kind === "dir" && state.clipboard && !many && { label: "Paste into this folder", run: () => paste(entry.path) },
    !many && { label: "Duplicate", run: () => copyEntries([entry.path], parentOf(entry.path)) },
    !many && { label: "Rename…", key: "F2", run: () => renameEntry(entry) },
    { label: "Move to…", run: () => moveTo(paths) },
    !many && { label: "Copy path", run: () => copyText(entry.path, "Path copied") },
    "-",
    { label: many ? `Delete ${paths.length} items` : "Delete", key: "Del", danger: true, run: () => removeEntries(chosen) },
  ]);
}

function folderMenu(event) {
  showMenu(event, [
    { label: "New folder…", run: makeFolder },
    { label: "New file…", run: makeFile },
    { label: "Upload files…", run: () => $("#upload-input").click() },
    state.clipboard && { label: "Paste", key: "Ctrl+V", run: () => paste() },
    "-",
    { label: "Download folder as .zip", run: () => download([state.folder]) },
    { label: "Copy folder path", run: () => copyText(state.folder || "/", "Path copied") },
    { label: "Refresh", run: refresh },
  ]);
}

function spaceMenu(event, entry) {
  showMenu(event, [
    { label: "Open", key: "Enter", run: () => openEntry(entry) },
    state.clipboard && { label: "Paste into it", run: () => paste(entry.path) },
    { label: "Download as .zip", run: () => download([entry.path]) },
    { label: "Copy path", run: () => copyText(`/${entry.path}`, "Path copied") },
  ]);
}

document.addEventListener("click", (event) => {
  if (menu && !menu.contains(event.target)) closeMenu();
});
window.addEventListener("blur", closeMenu);
window.addEventListener("resize", closeMenu);
dropZone.addEventListener("contextmenu", (event) => {
  if (event.target.closest(".filesRow")) return;
  event.preventDefault();
  selectOnly(null);
  folderMenu(event);
});

// -- search --------------------------------------------------------------------
async function runSearch(query) {
  const text = query.trim();
  if (!text) {
    state.search = null;
    status(summary());
    renderListing();
    return;
  }
  status(`Searching “${text}”…`);
  try {
    const found = await api.search(text, state.folder);
    if ($("#search").value.trim() !== text) return;
    state.search = { query: text, matches: found.matches, complete: found.complete };
    state.selected.clear();
    const count = found.matches.length;
    status(`${count} ${count === 1 ? "match" : "matches"} below ${state.folder || "the workspace"}${found.complete ? "" : " (search stopped early: refine it)"}`);
    renderListing();
  } catch (error) {
    status(error.message, "error");
  }
}

// -- keyboard ------------------------------------------------------------------
function moveFocus(step, extend) {
  const entries = shownEntries();
  if (!entries.length) return;
  const focused = document.activeElement?.closest?.(".filesRow")?.dataset.path ?? state.anchor;
  let index = entries.findIndex((entry) => entry.path === focused);
  index = Math.max(0, Math.min(entries.length - 1, index < 0 ? 0 : index + step));
  const target = entries[index];
  if (extend) {
    state.selected.add(target.path);
    renderListing();
  } else {
    selectOnly(target.path);
  }
  state.anchor = target.path;
  rows.querySelector(`[data-path="${CSS.escape(target.path)}"]`)?.focus();
}

document.addEventListener("keydown", (event) => {
  const typing = event.target.closest?.("input, textarea, select, [contenteditable]");
  const modifier = event.ctrlKey || event.metaKey;
  if (modifier && event.key.toLowerCase() === "s" && state.file?.editing) {
    event.preventDefault();
    saveFile();
    return;
  }
  if (event.key === "Escape" && menu) return closeMenu();
  if (typing || $("dialog[open]")) return;
  const chosen = selectedEntries();
  const focusedRow = event.target.closest?.(".filesRow");
  switch (true) {
    case event.key === "/":
      event.preventDefault();
      $("#search").focus();
      break;
    case event.key === "Backspace" || (event.altKey && event.key === "ArrowUp"):
      if (state.folder) {
        event.preventDefault();
        navigate(parentOf(state.folder), { push: true });
      }
      break;
    case event.key === "ArrowDown" || event.key === "ArrowUp":
      if (focusedRow || event.target === document.body) {
        event.preventDefault();
        moveFocus(event.key === "ArrowDown" ? 1 : -1, event.shiftKey);
      }
      break;
    case event.key === "Enter" && Boolean(focusedRow):
      event.preventDefault();
      openEntry(chosen.find((entry) => entry.path === focusedRow.dataset.path) ?? chosen[0]);
      break;
    case event.key === "Delete" && chosen.length > 0:
      removeEntries(chosen);
      break;
    case event.key === "F2" && chosen.length === 1:
      event.preventDefault();
      renameEntry(chosen[0]);
      break;
    case event.key.toLowerCase() === "e" && !modifier && Boolean(state.file) && !state.file.editing:
      startEditing();
      break;
    case modifier && event.key.toLowerCase() === "a":
      event.preventDefault();
      state.selected = new Set(shownEntries().filter((entry) => !entry.space).map((entry) => entry.path));
      renderListing();
      break;
    case modifier && event.key.toLowerCase() === "c" && chosen.length > 0:
      setClipboard("copy", chosen.map((entry) => entry.path));
      break;
    case modifier && event.key.toLowerCase() === "x" && chosen.length > 0:
      setClipboard("cut", chosen.map((entry) => entry.path));
      break;
    case modifier && event.key.toLowerCase() === "v" && Boolean(state.clipboard):
      paste();
      break;
    case event.key === "Escape":
      if (state.search) {
        $("#search").value = "";
        runSearch("");
      } else if (state.selected.size) selectOnly(null);
      else if (state.file && !state.file.editing) closeFile();
      break;
    default:
  }
});

// -- wiring --------------------------------------------------------------------
function decorate(button, path, size = 16) {
  button.prepend(icon(path, { size }));
}

function boot() {
  createThemeToggle($("#theme-toggle"));
  decorate($("#theme-toggle"), ICONS.theme);
  decorate($("#go-up"), RAISE.up);
  decorate($("#refresh"), RAISE.refresh);
  decorate($("#tree-toggle"), RAISE.tree);
  decorate($("#new-folder"), ICONS.folder, 15);
  decorate($("#new-file"), ICONS.plus, 15);
  decorate($("#upload"), ICONS.attach, 15);
  decorate($("#zip"), RAISE.download, 15);
  if (context) $("#back-to-chat").href = `/?conversation=${encodeURIComponent(context)}`;

  $("#go-up").addEventListener("click", () => state.folder && navigate(parentOf(state.folder), { push: true }));
  $("#refresh").addEventListener("click", refresh);
  $("#new-folder").addEventListener("click", makeFolder);
  $("#new-file").addEventListener("click", makeFile);
  $("#upload").addEventListener("click", () => $("#upload-input").click());
  $("#upload-input").addEventListener("change", (event) => {
    upload(event.target.files);
    event.target.value = "";
  });
  $("#paste").addEventListener("click", () => paste());
  $("#zip").addEventListener("click", () => download([state.folder]));
  $("#tree-toggle").addEventListener("click", () => {
    const open = document.body.classList.toggle("is-treeOpen");
    $("#tree-toggle").setAttribute("aria-expanded", String(open));
  });
  const hidden = $("#show-hidden");
  hidden.checked = state.showHidden;
  hidden.addEventListener("change", () => {
    state.showHidden = hidden.checked;
    store("grid.files.hidden", state.showHidden);
    status(summary());
    renderTree();
    renderListing();
  });
  $("#select-all").addEventListener("change", (event) => {
    state.selected = event.target.checked ? new Set(shownEntries().filter((entry) => !entry.space).map((entry) => entry.path)) : new Set();
    renderListing();
  });
  for (const button of $$(".filesSort")) {
    button.addEventListener("click", () => {
      const key = button.dataset.sort;
      state.sort = { key, dir: state.sort.key === key ? -state.sort.dir : 1 };
      store("grid.files.sort", state.sort);
      renderListing();
    });
  }
  const search = $("#search");
  const searchSoon = debounce((value) => runSearch(value), 250);
  search.addEventListener("input", () => searchSoon(search.value));
  search.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      search.value = "";
      runSearch("");
      search.blur();
    }
  });
  for (const button of $$("[data-bulk]")) {
    button.addEventListener("click", () => {
      const chosen = selectedEntries();
      const paths = chosen.map((entry) => entry.path);
      ({
        cut: () => setClipboard("cut", paths),
        copy: () => setClipboard("copy", paths),
        move: () => moveTo(paths),
        download: () => download(paths),
        delete: () => removeEntries(chosen),
        clear: () => selectOnly(null),
      })[button.dataset.bulk]();
    });
  }
  window.addEventListener("beforeunload", (event) => {
    if (state.file?.dirty) event.preventDefault();
  });
  window.addEventListener("popstate", async () => {
    const next = new URLSearchParams(location.search);
    await navigate(next.get("path") || "", { keepFile: true });
    const file = next.get("file");
    if (file && file !== state.file?.path) await openFile(file);
    else if (!file && state.file) await closeFile({ quiet: true });
  });

  const file = params.get("file");
  navigate(state.folder, { keepFile: true }).then(() => file && openFile(file));
}

boot();
