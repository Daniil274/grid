/**
 * The Server card's build line: version and short commit of the running server.
 *
 * The API reports `build` on /api/admin/server; the Accounts drawer shows it
 * beside "Restart server", so a restart that looks ineffective can be checked
 * against what the process really loaded.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { buildLabel, buildTitle } from "../web_chat/js/accounts/build-info.js";

test("a build becomes one quiet line under the version it belongs to", () => {
  assert.equal(
    buildLabel({ version: "0.2.0", commit: "f244d18", commit_full: "f244d18abc", source: "git" }),
    "Running Grid 0.2.0 · f244d18",
  );
});

test("a server that cannot tell its commit says unknown, not a guess", () => {
  assert.equal(buildLabel({ version: "0.2.0", commit: "unknown" }), "Running Grid 0.2.0 · unknown");
  assert.equal(buildLabel({}), "Running Grid unknown · unknown");
});

test("no build in the payload leaves the card as it was", () => {
  assert.equal(buildLabel(undefined), "");
  assert.equal(buildLabel(null), "");
});

test("the tooltip names the full commit and where it came from", () => {
  assert.equal(buildTitle({ commit_full: "f244d18abc", source: "git" }), "Commit f244d18abc (from git)");
  assert.equal(buildTitle({ commit_full: "f244d18abc" }), "Commit f244d18abc");
  assert.equal(buildTitle({ commit_full: "unknown", source: "unknown" }), "");
  assert.equal(buildTitle(undefined), "");
});
