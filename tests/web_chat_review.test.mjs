import { test } from "node:test";
import assert from "node:assert/strict";

import { installFakeDom } from "./fake_dom.mjs";
import { createMessage } from "../web_chat/js/ui/message.js";

installFakeDom();

const reportButton = (message) => message.el.find("msgAction--report");

test("an agent's answer offers a report of it, with its stored id", () => {
  const reported = [];
  const message = createMessage({ role: "assistant", content: "done", messageId: "m1", onReport: (answer) => reported.push(answer) });

  reportButton(message).click();

  assert.deepEqual(reported, [{ id: "m1", text: "done" }]);
});

test("an answer still streaming cannot be reported yet", () => {
  const reported = [];
  const message = createMessage({ role: "assistant", content: "working", onReport: (answer) => reported.push(answer) });

  reportButton(message).click();

  assert.deepEqual(reported, []);
  assert.ok(message.el.find("msg__notice"));
  message.adopt({ id: "m2" });
  reportButton(message).click();
  assert.deepEqual(reported, [{ id: "m2", text: "working" }]);
});

test("the user's own messages have no report button", () => {
  const message = createMessage({ role: "user", content: "hi", messageId: "u1", onReport: () => {} });

  assert.equal(reportButton(message), null);
});
