/**
 * Phone-layout contracts for the chat shell. These pin the causes of three
 * regressions seen at ~390 CSS px: the header widening the main grid column,
 * the route picker's two independently capped lists hiding its Agents section,
 * and deep reasoning rows whose sticky folds and fixed-width meta crushed the
 * title. Real geometry was checked in Chromium at 390x844 and 360x640; these
 * assertions keep the declarations that produce it from silently disappearing.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const read = (name) => readFile(new URL(`../web_chat/styles/${name}`, import.meta.url), "utf8");

/** The body of the first rule whose selector matches, or "" when absent. */
function rule(css, selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = css.match(new RegExp(`(?:^|\\})\\s*${escaped}\\s*\\{([^}]*)\\}`, "m"));
  return match?.[1] ?? "";
}

/** Every top-level @media block with this query, found by matching braces. */
function mediaBlocks(css, query) {
  const blocks = [];
  for (let start = css.indexOf(`@media ${query}`); start >= 0; start = css.indexOf(`@media ${query}`, start + 1)) {
    const open = css.indexOf("{", start);
    let depth = 0;
    for (let i = open; i < css.length; i += 1) {
      if (css[i] === "{") depth += 1;
      if (css[i] === "}") {
        depth -= 1;
        if (depth === 0) {
          blocks.push(css.slice(open + 1, i));
          break;
        }
      }
    }
  }
  return blocks;
}

/** The rule's body in whichever matching media block declares it. */
function mediaRule(css, query, selector) {
  for (const block of mediaBlocks(css, query)) {
    const body = rule(block, selector);
    if (body) return body;
  }
  return "";
}

const has = (body, declaration) => body.replace(/\s+/g, " ").includes(declaration);

test("the main column cannot be widened by the topbar's contents", async () => {
  const css = await read("layout.css");
  const main = rule(css, ".main");
  assert.ok(has(main, "grid-template-columns: minmax(0, 1fr)"), main);
  // A hidden reference banner must not shift auto-placement onto the composer.
  for (const [selector, row] of [
    [".main > .topbar", "1"],
    [".main > .referenceBanner", "2"],
    [".main > .chatScroll", "3"],
    [".main > .composerWrap", "4"],
  ]) {
    assert.ok(has(rule(css, selector), `grid-row: ${row}`), selector);
  }
  assert.ok(has(rule(css, ".topbar"), "min-width: 0"));
  assert.ok(has(rule(css, ".topbar__route"), "min-width: 0"));
  assert.ok(has(rule(css, ".routeChip"), "min-width: 0"));
  assert.ok(has(rule(css, ".topbar__status"), "overflow: hidden"));
});

test("a phone scrolls the route picker as one list instead of clipping Agents", async () => {
  const css = await read("layout.css");
  const query = "(max-width: 720px)";
  assert.ok(has(mediaRule(css, query, ".routeColumns"), "grid-template-columns: minmax(0, 1fr)"));
  const list = mediaRule(css, query, ".routeList");
  assert.ok(has(list, "max-height: none"), list);
  assert.ok(has(list, "overflow: visible"), list);
});

test("deep reasoning folds are not sticky inside another timeline", async () => {
  const css = await read("reasoning.css");
  assert.ok(has(rule(css, ".step__fold"), "position: sticky"));
  assert.ok(has(rule(css, ".agent__steps .step__fold"), "position: static"));
});

test("a step label keeps its title instead of yielding to bars and badges", async () => {
  const css = await read("reasoning.css");
  assert.ok(has(rule(css, ".step__label"), "flex: 1 1 auto"));
  const title = rule(css, ".step__title");
  assert.ok(has(title, "max-width: 100%"), title);
  assert.ok(has(title, "min-width: 0"), title);
  assert.ok(has(title, "overflow: hidden"), title);
  assert.ok(has(title, "text-overflow: ellipsis"), title);
  assert.ok(has(title, "white-space: nowrap"), title);
});

test("phone gutters shrink and the nested token counter yields first", async () => {
  const css = await read("reasoning.css");
  const query = "(max-width: 600px)";
  assert.ok(has(mediaRule(css, query, ".step"), "padding-left: 16px"));
  assert.ok(has(mediaRule(css, query, ".step__icon"), "width: 16px"));
  assert.ok(has(mediaRule(css, query, ".agent__steps .step__usage"), "display: none"));
});

test("the phone composer stays compact and above the fold", async () => {
  const css = await read("chat.css");
  const query = "(max-width: 720px)";
  assert.ok(has(mediaRule(css, query, ".composer__input"), "max-height: 28dvh"));
  assert.ok(has(mediaRule(css, query, ".composerWrap"), "padding: 4px 12px 8px"));
  assert.ok(has(mediaRule(css, query, ".composer"), "padding: 8px 10px 6px"));
});
