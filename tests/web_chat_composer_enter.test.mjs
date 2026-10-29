/**
 * The composer's Enter contract, on a real `createComposer` over the fake DOM.
 *
 * A phone's on-screen keyboard sends the same keydown as a hardware one:
 * key "Enter", keyCode 13, shiftKey false, isComposing false. There is no
 * marker in the event that says "soft keyboard", so the composer decides by
 * the device's primary pointer: coarse means a finger, and there Enter must
 * fall through to the browser so the textarea gets a native new line. Desktop
 * keeps Enter to send and Shift+Enter for a new line, and an IME may end its
 * composition with an Enter that must never send.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { installFakeDom } from "./fake_dom.mjs";
import { createStore } from "../web_chat/js/lib/store.js";
import { createComposer } from "../web_chat/js/ui/composer.js";

installFakeDom();
Node.TEXT_NODE = 3;

const PHONE = "(pointer: coarse)";

/** A composer like the one main.js builds, over whatever device we ask for. */
function composer({ coarse = false, state = {} } = {}) {
  const root = document.createElement("div");
  const nodes = Object.fromEntries(["input", "sendButton", "stopButton", "deliverySelect", "continueButton",
    "attachButton", "fileInput", "tray"].map((key) => [key, document.createElement("div")]));
  nodes.input.closest = () => root;
  nodes.input.value = "";
  nodes.input.scrollHeight = 30;
  const label = document.createTextNode("Stop");
  label.nodeType = Node.TEXT_NODE;
  nodes.stopButton.append(label);

  // The only media feature the composer asks about: a finger-first device.
  globalThis.matchMedia = (query) => ({ matches: query === PHONE && coarse });

  const sent = [];
  const store = createStore({ streaming: false, uploads: { enabled: false, max_file_mb: 25, max_files: 10 }, ...state });
  createComposer({ ...nodes, store,
    onSend: (...args) => sent.push(args), onStop: () => {}, onContinue: () => {} });

  /** Press a key on the composer's textarea; reports whether the page cancelled it. */
  const press = (event) => {
    let prevented = false;
    const keydown = { key: "Enter", shiftKey: false, isComposing: false, keyCode: 13, ...event };
    for (const fn of nodes.input.listeners.keydown ?? []) {
      fn({ ...keydown, preventDefault: () => { prevented = true; } });
    }
    return prevented;
  };

  return { ...nodes, sent, press, store };
}

// -- a phone: Enter is a new line -------------------------------------------

test("a phone's Enter is left to the browser: no submit, no cancel, draft intact", () => {
  const ui = composer({ coarse: true });
  ui.input.value = "first line";
  const prevented = ui.press({});
  assert.equal(prevented, false, "preventDefault would swallow the native new line");
  assert.deepEqual(ui.sent, []);
  assert.equal(ui.input.value, "first line");
});

test("a phone's Enter then Send sends every line of the draft", () => {
  const ui = composer({ coarse: true });
  ui.input.value = "first line";
  ui.press({});
  // The browser would insert the newline the composer did not cancel.
  ui.input.value += "\nsecond line";
  ui.sendButton.click();
  assert.equal(ui.sent.length, 1);
  assert.equal(ui.sent[0][0], "first line\nsecond line");
  assert.equal(ui.input.value, "");
});

test("Ctrl+Enter and ⌘+Enter still send from a phone", () => {
  for (const chord of [{ ctrlKey: true }, { metaKey: true }]) {
    const ui = composer({ coarse: true });
    ui.input.value = "sent by chord";
    assert.equal(ui.press(chord), true);
    assert.deepEqual(ui.sent, [["sent by chord", [], null]]);
  }
});

test("a phone's Shift+Enter stays a new line too", () => {
  const ui = composer({ coarse: true });
  ui.input.value = "a";
  assert.equal(ui.press({ shiftKey: true }), false);
  assert.deepEqual(ui.sent, []);
  assert.equal(ui.input.value, "a");
});

test("a phone cannot send a blank or whitespace-only draft with Enter", () => {
  for (const draft of ["", "   ", "\n\n"]) {
    const ui = composer({ coarse: true });
    ui.input.value = draft;
    ui.press({});
    ui.sendButton.click();
    assert.deepEqual(ui.sent, []);
    assert.equal(ui.input.value, draft);
  }
});

// -- a desktop: the contract that was already there -------------------------

test("a desktop's Enter sends and clears the composer", () => {
  const ui = composer();
  ui.input.value = "hello";
  assert.equal(ui.press({}), true);
  assert.deepEqual(ui.sent, [["hello", [], null]]);
  assert.equal(ui.input.value, "");
});

test("a desktop's Shift+Enter is a new line: not sent, not cancelled", () => {
  const ui = composer();
  ui.input.value = "line";
  assert.equal(ui.press({ shiftKey: true }), false);
  assert.deepEqual(ui.sent, []);
  assert.equal(ui.input.value, "line");
});

test("Shift+Enter remains a new line even with Ctrl or Meta held", () => {
  for (const coarse of [false, true]) {
    for (const chord of [{ ctrlKey: true }, { metaKey: true }]) {
      const ui = composer({ coarse });
      ui.input.value = "line";
      assert.equal(ui.press({ ...chord, shiftKey: true }), false);
      assert.deepEqual(ui.sent, []);
      assert.equal(ui.input.value, "line");
    }
  }
});

test("a desktop's Enter cannot send an empty or whitespace-only draft", () => {
  for (const draft of ["", "   "]) {
    const ui = composer();
    ui.input.value = draft;
    ui.press({});
    assert.deepEqual(ui.sent, []);
    assert.equal(ui.input.value, draft);
  }
});

test("Enter respects the states that block sending and keeps the draft", () => {
  const ui = composer({ state: { restartPending: true, resumable: true } });
  ui.input.value = "keep this draft";
  ui.press({});
  assert.deepEqual(ui.sent, []);
  assert.equal(ui.input.value, "keep this draft");
});

// -- an IME: Enter belongs to the composition, not to the chat --------------

test("Enter during an IME composition never sends and is not cancelled", () => {
  for (const coarse of [false, true]) {
    const ui = composer({ coarse });
    ui.input.value = "こんにちは";
    assert.equal(ui.press({ isComposing: true }), false);
    assert.deepEqual(ui.sent, []);
    assert.equal(ui.input.value, "こんにちは");
  }
});

test("Android's keyCode 229 Enter never sends, whatever isComposing says", () => {
  for (const coarse of [false, true]) {
    const ui = composer({ coarse });
    ui.input.value = "привет";
    ui.press({ keyCode: 229, isComposing: false });
    ui.press({ keyCode: 229, isComposing: false, shiftKey: false });
    assert.deepEqual(ui.sent, []);
    assert.equal(ui.input.value, "привет");
  }
});

test("desktop Enter still sends when matchMedia is unavailable", () => {
  const ui = composer();
  delete globalThis.matchMedia;
  ui.input.value = "text";
  assert.equal(ui.press({}), true);
  assert.deepEqual(ui.sent, [["text", [], null]]);
});

test("the textarea requests a return key rather than a send key", () => {
  const html = readFileSync(new URL("../web_chat/index.html", import.meta.url), "utf8");
  assert.match(html, /<textarea\b[^>]*id="composer-input"[^>]*enterkeyhint="enter"/);
});
