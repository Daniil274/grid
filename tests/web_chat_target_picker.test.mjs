/**
 * The target chip (web_chat/js/ui/target-picker.js) in Node: the glyph says
 * where the tools act - a cloud on the server, a screen on a computer - and a
 * device's name is written only when the whole of it fits the chip.
 *
 * The DOM is the small fake from tests/fake_dom.mjs; width there is a number
 * a test sets, so "does it fit" is decided by the test, not by layout.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { installFakeDom } from "./fake_dom.mjs";

installFakeDom();

// The fake has no layout: a chip's widths are what the test says they are.
let widths = { client: 0, scroll: 0 };
const proto = document.createElement("div").constructor.prototype;
Object.defineProperty(proto, "clientWidth", { get: () => widths.client, configurable: true });
Object.defineProperty(proto, "scrollWidth", { get: () => widths.scroll, configurable: true });

document.addEventListener = () => {};
globalThis.window = { addEventListener: () => {} };
globalThis.location = { protocol: "https:", host: "grid.example" };

const { createStore } = await import("../web_chat/js/lib/store.js");
const { createTargetPicker } = await import("../web_chat/js/ui/target-picker.js");
const { ICONS } = await import("../web_chat/js/ui/icons.js");

const LONG_NAME = "my-workstation-with-a-very-long-name";
const DEVICE = {
  device: "dev-1",
  name: LONG_NAME,
  kind: "node",
  platform: "linux",
  default_workdir: "/home/me",
};

const el = (tag, cls) => {
  const node = document.createElement(tag);
  if (cls) node.setAttribute("class", cls);
  return node;
};

/** The chip and the picker over one store, as js/main.js builds them. */
function mount() {
  const button = el("button", "routeChip routeChip--target");
  const iconNode = el("span", "routeChip__icon");
  const text = el("span", "routeChip__text");
  const nameNode = el("span", "routeChip__name");
  const metaNode = el("span", "routeChip__meta");
  nameNode.textContent = "Cloud";
  text.append(nameNode, metaNode);
  button.append(iconNode, text, el("span", "routeChip__chevron"));

  const popover = el("div", "popover popover--target");
  popover.id = "target-popover";
  const list = el("div", "targetList");
  popover.append(list);

  const folder = {
    button: el("button", "routeChip routeChip--workdir"),
    nameNode: el("span", "routeChip__name"),
    metaNode: el("span", "routeChip__meta"),
    popover: el("div", "popover popover--workdir"),
    up: el("button"),
    path: el("code"),
    reload: el("button"),
    use: el("button"),
    bookmarks: el("div"),
    list: el("div"),
    newForm: el("form"),
    newInput: el("input"),
    pathForm: el("form"),
    pathInput: el("input"),
    status: el("p"),
  };

  const api = {
    newPairing: async () => ({ code: "ABCDE-FGHJK" }),
    devices: async () => ({ pairing: true, devices: [DEVICE] }),
    deviceDirs: async () => ({ path: "", entries: [] }),
  };

  const store = createStore({ executionTarget: "cloud", executionWorkdir: "", devices: [] });
  createTargetPicker({
    nodes: {
      button,
      nameNode,
      metaNode,
      popover,
      list,
      connectSection: el("section"),
      connectLinux: el("code"),
      connectWindows: el("code"),
      connectWindowsCmd: el("code"),
      copyLinux: el("button"),
      copyWindows: el("button"),
      copyWindowsCmd: el("button"),
      folder,
    },
    store,
    api,
    onChange: () => {},
    onCopied: () => {},
  });
  return { button, iconNode, nameNode, metaNode, list, store };
}

/** The `d` of the glyph the chip (or a row) shows. */
const glyph = (node) => node.querySelector("svg")?.childNodes?.[0]?.getAttribute("d") ?? null;

test("the server is a cloud, a computer is a screen", () => {
  const { button, iconNode, nameNode, list, store } = mount();
  assert.equal(glyph(iconNode), ICONS.cloud);
  assert.equal(nameNode.textContent, "Cloud");

  widths = { client: 400, scroll: 120 };
  store.set({ executionTarget: "device:dev-1", devices: [DEVICE] });
  assert.equal(button.dataset.mode, "device");
  assert.equal(glyph(iconNode), ICONS.monitor);
  assert.equal(nameNode.textContent, LONG_NAME);

  store.set({ executionTarget: "cloud" });
  assert.equal(glyph(iconNode), ICONS.cloud);
  assert.equal(nameNode.textContent, "Cloud");

  // Both rows of the list name themselves with the same two glyphs.
  const rows = list.querySelectorAll(".routeOption");
  assert.equal(rows.length, 2);
  assert.equal(glyph(rows[0]), ICONS.cloud);
  assert.equal(glyph(rows[1]), ICONS.monitor);
});

test("a name that does not fit the chip is not written", () => {
  widths = { client: 400, scroll: 120 };
  const { button, nameNode, store } = mount();
  store.set({ executionTarget: "device:dev-1", devices: [DEVICE] });
  assert.equal(button.classList.contains("is-compact"), false);

  // The same name, a narrower chip: the text goes, the glyph stays.
  widths = { client: 100, scroll: 260 };
  store.set({ devices: [DEVICE] });
  assert.equal(button.classList.contains("is-compact"), true);
  assert.match(button.title, new RegExp(LONG_NAME));
  assert.match(button.getAttribute("aria-label"), new RegExp(LONG_NAME));

  // Room again: the whole name comes back.
  widths = { client: 400, scroll: 120 };
  store.set({ devices: [{ ...DEVICE }] });
  assert.equal(button.classList.contains("is-compact"), false);
  assert.equal(nameNode.textContent, LONG_NAME);
});
