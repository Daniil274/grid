/**
 * Where a chat's work happens: this server, or one of the user's computers - and,
 * on a computer, in which folder (web_chat/devices_api.py, core/devices/execution.py).
 *
 * The two chips sit beside the route chip and answer two different questions:
 * *who* works on the message (the route) and *where the tools act* (the target).
 * `Cloud` is the server, its workspace and containers, and needs no second
 * choice; a device brings a folder with it, so the folder chip appears only then.
 *
 * The folder picker walks the device's own disk through
 * `GET /api/devices/{id}/dirs`: the node answers with the folder's path, its
 * subfolders, its parent, the bookmarks and the roots (drives on Windows), so a
 * path is never guessed from the browser's side.
 *
 * A choice is the user's, never a model's: everything here goes through the
 * store and a `change` callback the chat controller sends to the server.
 */

import { debounce, h, icon, replace } from "../lib/dom.js";
import { ICONS } from "./icons.js";

/** The chat works on the server: no device, no folder of its own. */
const CLOUD = "cloud";

/** The last segment of a path, whatever separators it uses. */
function folderName(path) {
  const parts = String(path || "").split(/[\\/]+/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : String(path || "");
}

/** A device's target value (`device:<id>`), as the server spells it. */
const targetOf = (device) => `device:${device.device}`;

/** One line that installs the node and pairs it, per platform, *code* included. */
function installers(origin, code) {
  return {
    linux: `curl -fsSL ${origin}/node/install.sh | sh -s -- ${code}`,
    windows: `iwr -UseBasicParsing ${origin}/node/install.ps1?code=${code} | iex`,
    // cmd.exe has no pipe-into-a-shell and its curl is a plain file fetcher:
    // the line downloads the exe itself and runs the same install subcommand.
    windowsCmd: `md "%LOCALAPPDATA%\\grid-node" 2>nul & curl -fsSL -o "%LOCALAPPDATA%\\grid-node\\grid-node.exe" ${origin}/node/bin/grid-node-windows-amd64.exe && "%LOCALAPPDATA%\\grid-node\\grid-node.exe" install --server ${origin} --pair ${code}`,
  };
}

/** A row of the folder list: the name opens the folder, `Use` picks it. */
function folderRow(entry, { onOpen, onUse }) {
  const path = entry.path || entry.name;
  return h(
    "div.workdirRow",
    {},
    h(
      "button.workdirRow__open",
      { type: "button", title: path, on: { click: () => onOpen(path) } },
      icon(ICONS.folder, { size: 14 }),
      h("span.workdirRow__name", { text: entry.name || folderName(path) }),
    ),
    h("button.workdirRow__use", {
      type: "button",
      text: "Use",
      title: `Work in ${path}`,
      on: { click: () => onUse(path) },
    }),
  );
}

/** One choice of the target list, in the route picker's own shape. */
function option({ iconPath, title, description, meta, active, onPick }) {
  return h(
    "button.routeOption",
    {
      type: "button",
      class: active ? "is-active" : "",
      title: description || null,
      on: { click: onPick },
    },
    h(
      "span.routeOption__top",
      {},
      h(
        "span.routeOption__head",
        {},
        iconPath ? icon(iconPath, { size: 14, className: "routeOption__icon" }) : null,
        h("span.routeOption__title", { text: title }),
      ),
      active ? icon(ICONS.check, { size: 14, className: "routeOption__check" }) : null,
    ),
    description ? h("span.routeOption__desc", { text: description }) : null,
    meta ? h("span.routeOption__meta", { text: meta }) : null,
  );
}

/**
 * @param {object} nodes the chips and popovers this picker drives
 * @param {object} store the app store (`executionTarget`, `executionWorkdir`, `devices`)
 * @param {object} api the REST client (devices, deviceDirs)
 * @param {(selection: {target: string, workdir: string}) => void} onChange the user moved the target
 * @param {(message: string) => void} [onCopied] say that a command was copied
 */
export function createTargetPicker({ nodes, store, api, onChange, onCopied }) {
  const { button, nameNode, metaNode, popover, list, folder } = nodes;
  // The chip's own glyph: a cloud on the server, a screen on a computer. The
  // first render paints it from the state itself - `paintIcons` in js/main.js
  // has already filled the markup in, and that glyph is simply replaced.
  const iconNode = button.querySelector(".routeChip__icon");
  let chipMode = "";

  // The chip drives the popover: it names it (aria-controls) and reports its
  // state (aria-expanded), the popover itself is focusable and holds the menu.
  button.setAttribute("aria-controls", popover.id);
  button.setAttribute("aria-expanded", "false");
  popover.setAttribute("tabindex", "-1");
  folder.button.setAttribute("aria-controls", folder.popover.id);
  folder.button.setAttribute("aria-expanded", "false");
  folder.popover.setAttribute("tabindex", "-1");

  const origin = `${location.protocol}//${location.host}`;

  /** Open one popover, closing the other: two open menus make no sense. */
  function menu(chip, panel, { onOpen, onClose } = {}) {
    const options = () => [...panel.querySelectorAll(".routeOption, .workdirRow__open, .workdirBookmark")];
    const focusFirst = () => (options()[0] ?? panel).focus();
    return {
      panel,
      chip,
      isOpen: () => panel.classList.contains("is-open"),
      open() {
        this.closeOther?.();
        panel.classList.add("is-open");
        chip.setAttribute("aria-expanded", "true");
        onOpen?.();
        focusFirst();
      },
      close({ restoreFocus = panel.contains(document.activeElement) } = {}) {
        if (!panel.classList.contains("is-open")) return;
        panel.classList.remove("is-open");
        chip.setAttribute("aria-expanded", "false");
        onClose?.();
        if (restoreFocus) chip.focus();
      },
      toggle() {
        if (this.isOpen()) this.close({ restoreFocus: true });
        else this.open();
      },
    };
  }

  // -- connecting a computer from right here ---------------------------------
  // A pairing code lives ten minutes and is spent once, so a fresh one is
  // asked for every time the popover opens and filled into the one line
  // itself; the code never has to be fetched anywhere else.
  async function offerPairingCode() {
    nodes.connectLinux.textContent = "Loading…";
    nodes.connectWindows.textContent = "Loading…";
    nodes.connectWindowsCmd.textContent = "Loading…";
    try {
      const { code } = await api.newPairing();
      const lines = installers(origin, code);
      nodes.connectLinux.textContent = lines.linux;
      nodes.connectWindows.textContent = lines.windows;
      nodes.connectWindowsCmd.textContent = lines.windowsCmd;
    } catch (error) {
      const text = error.message || "Pairing is unavailable on this server.";
      nodes.connectLinux.textContent = text;
      nodes.connectWindows.textContent = text;
      nodes.connectWindowsCmd.textContent = text;
    }
  }

  // The list is read while the popover is open, so a computer that connects
  // while the user watches appears in it at once (web_chat/devices_api.py).
  let devicesPoll = null;

  async function readDevices() {
    try {
      const answer = await api.devices();
      if (nodes.connectSection) nodes.connectSection.hidden = answer.pairing === false;
      const devices = Array.isArray(answer.devices) ? answer.devices : [];
      // A quiet poll must not rebuild the list under the user's fingers.
      if (JSON.stringify(devices) !== JSON.stringify(store.get().devices)) store.set({ devices });
    } catch {
      // The server was out of reach this tick; the next one tries again.
    }
  }

  function watchDevices(start) {
    if (start) {
      void readDevices();
      devicesPoll = setInterval(() => void readDevices(), 3000);
    } else if (devicesPoll !== null) {
      clearInterval(devicesPoll);
      devicesPoll = null;
    }
  }

  const targetMenu = menu(button, popover, {
    onOpen: () => {
      void offerPairingCode();
      watchDevices(true);
    },
    onClose: () => watchDevices(false),
  });
  const folderMenu = menu(folder.button, folder.popover);
  targetMenu.closeOther = () => folderMenu.close({ restoreFocus: false });
  folderMenu.closeOther = () => targetMenu.close({ restoreFocus: false });

  function closeAll() {
    targetMenu.close({ restoreFocus: false });
    folderMenu.close({ restoreFocus: false });
  }

  const deviceIn = (state) =>
    (state.devices || []).find((device) => targetOf(device) === state.executionTarget) ?? null;

  // -- the target chip and its list -----------------------------------------

  /**
   * A name is written whole or not at all: a chip that cuts it in the middle
   * names nothing, so the text goes and the glyph stays, with the name in the
   * title. The measurement happens while the name is still shown - hidden text
   * measures zero - so the class is dropped first and laid on again after.
   */
  function fitName() {
    button.classList.remove("is-compact");
    if (nameNode.scrollWidth > nameNode.clientWidth + 1) button.classList.add("is-compact");
  }

  // The folder's name follows the same rule as the device's: written whole
  // or not at all - a clipped name names nothing, the glyph stays a button.
  function fitFolderName() {
    folder.button.classList.remove("is-compact");
    if (folder.nameNode.scrollWidth > folder.nameNode.clientWidth + 1) folder.button.classList.add("is-compact");
  }

  function renderChip(state) {
    const device = deviceIn(state);
    const mode = device ? "device" : "cloud";
    if (mode !== chipMode) {
      chipMode = mode;
      if (iconNode) replace(iconNode, icon(device ? ICONS.monitor : ICONS.cloud, { size: 14 }));
    }
    const folderPath = device ? state.executionWorkdir || device.default_workdir || "" : "";
    const title = device
      ? `The tools act on ${device.name}${folderPath ? `, in ${folderPath}` : ""}`
      : "The tools act on this server, in the chat's workspace";
    if (device) {
      nameNode.textContent = device.name;
      metaNode.textContent = [device.platform || device.kind, folderPath].filter(Boolean).join(" · ");
      button.dataset.mode = "device";
    } else {
      nameNode.textContent = "Cloud";
      metaNode.textContent = "Tools act on this server";
      button.dataset.mode = "cloud";
    }
    // The title is also the chip's name while the text is hidden.
    button.title = title;
    button.setAttribute("aria-label", `Where this chat's tools act: ${title}`);
    // A folder is a device's folder: on the server the workspace says it.
    folder.button.hidden = !device;
    if (device) {
      folder.nameNode.textContent = folderPath ? folderName(folderPath) : "Choose a folder";
      folder.metaNode.textContent = folderPath || "The device's folder";
      folder.button.title = folderPath
        ? `This chat works in ${folderPath} on ${device.name}`
        : `Choose the folder this chat works in on ${device.name}`;
    }
    // Compacting one chip frees width for the other: fit both until they
    // settle, or a long route name would compact a chip that would fit.
    for (let round = 0; round < 3; round++) {
      const was = [button.classList.contains("is-compact"), folder.button.classList.contains("is-compact")];
      fitName();
      fitFolderName();
      if (was[0] === button.classList.contains("is-compact") && was[1] === folder.button.classList.contains("is-compact")) break;
    }
  }

  function renderList(state) {
    const devices = state.devices || [];
    const options = [
      option({
        iconPath: ICONS.cloud,
        title: "Cloud",
        description: "Files, search and commands run on this Grid server, in the chat's workspace.",
        meta: "the server · its workspace",
        active: !deviceIn(state),
        onPick: () => pick({ target: CLOUD, workdir: "" }),
      }),
      ...devices.map((device) =>
        option({
          iconPath: ICONS.monitor,
          title: device.name,
          description: `Files, search and commands run on this device (${device.kind || "device"}). Anything outside its folder is put to you first.`,
          meta: [device.platform, device.default_workdir, (device.capabilities || []).join(", ")]
            .filter(Boolean)
            .join(" · "),
          active: targetOf(device) === state.executionTarget,
          onPick: () =>
            pick({ target: targetOf(device), workdir: state.executionWorkdir || device.default_workdir || "" }),
        }),
      ),
    ];
    if (!devices.length) {
      options.push(
        h("p.routeHint", {
          text: "No computer is connected yet. Run one line from below on the computer; it appears here as soon as it connects.",
        }),
      );
    }
    replace(list, options);
  }

  function pick(selection) {
    closeAll();
    onChange(selection);
  }

  // -- the folder popover ----------------------------------------------------
  /** What the device last answered about one folder (web_chat/devices_api.py). */
  let listing = null;
  let loading = 0;

  const status = (text, tone = "") => {
    folder.status.textContent = text;
    folder.status.dataset.tone = tone;
    folder.status.hidden = !text;
  };

  function renderListing() {
    const answer = listing;
    if (!answer) {
      replace(folder.list, h("p.routeHint", { text: "Choose a device first." }));
      return;
    }
    folder.path.textContent = answer.path || answer.workdir || "…";
    folder.path.title = answer.path || "";
    replace(
      folder.list,
      answer.detail
        ? h("p.routeHint", { text: answer.detail })
        : answer.dirs?.length
          ? answer.dirs.map((entry) =>
              folderRow(entry, { onOpen: (path) => void load(path), onUse: (path) => use(path) }),
            )
          : h("p.routeHint", { text: "No subfolder here. Create one below, or type a path." }),
    );
    replace(folder.bookmarks, [
      ...(answer.workdir
        ? [
            h("button.workdirBookmark", {
              type: "button",
              text: "Device folder",
              title: answer.workdir,
              on: { click: () => void load(answer.workdir) },
            }),
          ]
        : []),
      ...(answer.bookmarks || []).map((bookmark) =>
        h("button.workdirBookmark", {
          type: "button",
          text: bookmark.name || folderName(bookmark.path),
          title: bookmark.path || "",
          on: { click: () => void load(bookmark.path) },
        }),
      ),
      ...(answer.roots || []).map((root) =>
        h("button.workdirBookmark.workdirBookmark--root", {
          type: "button",
          text: root.name || root.path || String(root),
          title: root.path || "",
          on: { click: () => void load(root.path || String(root)) },
        }),
      ),
    ]);
  }

  /**
   * Read one folder of the device. Answers are matched to the last request, so
   * a slow listing of a folder the user already left cannot draw over it.
   */
  async function load(path) {
    const device = deviceIn(store.get());
    if (!device) return;
    const asked = ++loading;
    status("Reading the folders…");
    try {
      const answer = await api.deviceDirs(device.device, path || "");
      if (asked !== loading) return;
      listing = answer;
      status("");
      renderListing();
    } catch (error) {
      if (asked !== loading) return;
      status(error.message, "error");
    }
  }

  /** Point the chat at *path* on the device it is already on. */
  function use(path) {
    const state = store.get();
    const device = deviceIn(state);
    const chosen = String(path || "").trim();
    if (!device || !chosen) return;
    closeAll();
    onChange({ target: targetOf(device), workdir: chosen });
  }

  // -- wiring ----------------------------------------------------------------
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    if (targetMenu.isOpen()) targetMenu.close({ restoreFocus: true });
    else {
      renderList(store.get());
      targetMenu.open();
    }
  });
  folder.button.addEventListener("click", (event) => {
    event.stopPropagation();
    if (folderMenu.isOpen()) {
      folderMenu.close({ restoreFocus: true });
      return;
    }
    folderMenu.open();
    const state = store.get();
    void load(state.executionWorkdir || deviceIn(state)?.default_workdir || "");
  });

  for (const panel of [popover, folder.popover]) {
    panel.addEventListener("click", (event) => event.stopPropagation());
    panel.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        closeAll();
        return;
      }
      if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
      const items = [...panel.querySelectorAll(".routeOption, .workdirRow__open, .workdirBookmark")];
      if (!items.length) return;
      event.preventDefault();
      if (event.key === "Home") return items[0].focus();
      if (event.key === "End") return items[items.length - 1].focus();
      const at = items.indexOf(document.activeElement);
      items[(at + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus();
    });
  }

  folder.up.addEventListener("click", () => {
    if (listing?.parent) void load(listing.parent);
    else if (listing?.path) {
      // A node that names no parent still has one: ask for its folder's own.
      const path = String(listing.path).replace(/[\\/]+$/, "");
      const parent = path.replace(/[\\/][^\\/]+$/, "");
      if (parent && parent !== path) void load(parent);
    }
  });
  folder.reload.addEventListener("click", () => void load(listing?.path || ""));
  folder.use.addEventListener("click", () => use(listing?.path || ""));
  folder.newForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const device = deviceIn(store.get());
    const name = folder.newInput.value.trim();
    if (!device || !name) return;
    try {
      // The node makes the folder and answers with the listing it is in.
      listing = await api.makeDeviceDir(device.device, listing?.path || "", name);
      folder.newInput.value = "";
      status("");
      renderListing();
    } catch (error) {
      status(error.message, "error");
    }
  });
  folder.pathForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const path = folder.pathInput.value.trim();
    if (path) void load(path);
  });

  // The copy buttons copy what the lines say now: the code in them is fresh
  // from the server, not the one a page load once brought.
  for (const [line, copy] of [
    [nodes.connectLinux, nodes.copyLinux],
    [nodes.connectWindows, nodes.copyWindows],
    [nodes.connectWindowsCmd, nodes.copyWindowsCmd],
  ]) {
    copy.addEventListener("click", async () => {
      const text = line.textContent;
      if (!text || text === "Loading…") return;
      try {
        await navigator.clipboard.writeText(line.textContent);
        onCopied?.("Installer command copied");
      } catch {
        onCopied?.("Select the command and copy it");
      }
    });
  }

  document.addEventListener("click", () => closeAll());
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && (targetMenu.isOpen() || folderMenu.isOpen())) closeAll();
  });

  store.watch(["executionTarget", "executionWorkdir", "devices"], (state) => {
    renderChip(state);
    renderList(state);
    // A listing is about one device: another target needs its own.
    if (folderMenu.isOpen() && deviceIn(state)) void load(state.executionWorkdir || "");
    else if (!deviceIn(state)) listing = null;
  });

  // The chip is only as wide as the viewport allows (`max-width: 46vw` in
  // styles/layout.css), so a resize can make a name that fitted stop fitting.
  window.addEventListener("resize", debounce(() => renderChip(store.get()), 120));
  // A name measured in the fallback font may stop fitting once the real one
  // lands, so the check runs again when the fonts are ready.
  document.fonts?.ready.then(() => renderChip(store.get())).catch(() => {});

  return { close: closeAll, refresh: () => renderList(store.get()) };
}
