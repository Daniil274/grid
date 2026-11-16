/**
 * The prompt size of a turn, shown beside an answer's actions.
 *
 * "Current context" is what the model saw in its last call of the turn
 * (web_chat/session.py's done frame and the stored message's metadata carry
 * ``context_tokens``). The UI shows it as a quiet number, never a button, and
 * only when there is a real count: a user's message, a system message and a
 * turn without usage stay bare.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { installFakeDom } from "./fake_dom.mjs";
import { createMessage } from "../web_chat/js/ui/message.js";

installFakeDom();

const indicator = (message) => message.el.find("msg__contextTokens");
const actions = (message) => message.el.find("msg__actions");

test("an answer carries the turn's context size beside its actions", () => {
  const message = createMessage({ role: "assistant", content: "done", contextTokens: 123456 });

  const node = indicator(message);
  assert.ok(node, "the indicator is in the message");
  assert.equal(actions(message).childNodes.includes(node), true, "it sits in the action row");
  assert.equal(node.hidden, false);
  assert.equal(node.textContent, "123k");
  // The exact number is not lost behind the rounded one.
  assert.equal(node.title, "Context tokens after this message: 123,456");
  // A number is shown, not a button of its own.
  assert.equal(node.tagName, "SPAN");
  assert.equal(node.classList.contains("msgAction"), false);
});

test("the magnitude is all the row shows", () => {
  const shown = (tokens) => indicator(createMessage({ role: "assistant", content: "", contextTokens: tokens }));
  assert.equal(shown(812).textContent, "812");
  assert.equal(shown(2048).textContent, "2k");
  assert.equal(shown(999_999).textContent, "1000k");
  assert.equal(shown(1_000_000).textContent, "1m");
  assert.equal(shown(1_200_000).textContent, "1.2m");
});

test("a message without a count shows nothing at all", () => {
  for (const value of [undefined, null, 0, -5, Number.NaN, ""]) {
    const message = createMessage({ role: "assistant", content: "done", contextTokens: value });
    const node = indicator(message);
    assert.ok(node, `the node exists for ${String(value)}`);
    assert.equal(node.hidden, true, `hidden for ${String(value)}`);
    assert.equal(node.textContent, "", `no text for ${String(value)}`);
  }
});

test("the user's own and system messages never carry the indicator", () => {
  for (const role of ["user", "system"]) {
    const message = createMessage({ role, content: "hi", contextTokens: 4096 });
    assert.equal(indicator(message), null, `${role} has no indicator`);
  }
});

test("a live done frame updates the number in place", () => {
  const message = createMessage({ role: "assistant", content: "working" });
  const row = actions(message);
  const node = indicator(message);
  assert.equal(node.hidden, true);
  const buttons = row.childrenWith("msgAction").length;

  message.setContextTokens(2048);

  // The same node in the same row: nothing was rebuilt.
  assert.equal(indicator(message), node);
  assert.equal(actions(message), row);
  assert.equal(row.childrenWith("msgAction").length, buttons);
  assert.equal(node.hidden, false);
  assert.equal(node.textContent, "2k");

  message.setContextTokens(1_500_000);
  assert.equal(indicator(message), node);
  assert.equal(node.textContent, "1.5m");
});

test("a loaded message adopts the stored count without a redraw", () => {
  const message = createMessage({ role: "assistant", content: "answer" });
  const node = indicator(message);
  assert.equal(node.hidden, true);

  message.adopt({ id: "m1", contextTokens: 4096 });

  assert.equal(indicator(message), node);
  assert.equal(node.hidden, false);
  assert.equal(node.textContent, "4k");
  assert.equal(node.title, "Context tokens after this message: 4,096");
});

test("adopting a message with no stored count leaves the row bare", () => {
  const message = createMessage({ role: "assistant", content: "answer" });

  message.adopt({ id: "m1" });

  assert.equal(indicator(message).hidden, true);
  assert.equal(indicator(message).textContent, "");
});
