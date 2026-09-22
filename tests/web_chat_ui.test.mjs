import { test } from "node:test";
import assert from "node:assert/strict";

import { renderMarkdown, escapeHtml } from "../web_chat/js/lib/markdown.js";
import { duration, dayBucket, plural } from "../web_chat/js/lib/format.js";
import { createStore } from "../web_chat/js/lib/store.js";
import { SpeechPlayer, takeSentences } from "../web_chat/js/ui/speech.js";
import { humanizePath, speakableText } from "../web_chat/js/lib/speakable.js";

test("model output can never inject markup", () => {
  const html = renderMarkdown('<img src=x onerror="alert(1)"> and <script>alert(2)</script>');
  assert.ok(!html.includes("<img"));
  assert.ok(!html.includes("<script"));
  assert.ok(html.includes("&lt;script&gt;"));
});

test("link targets are restricted to safe schemes", () => {
  assert.ok(renderMarkdown("[ok](https://example.com)").includes('href="https://example.com"'));
  assert.ok(renderMarkdown("[relative](/docs)").includes('href="/docs"'));
  const dangerous = renderMarkdown("[no](javascript:alert(1))");
  assert.ok(!dangerous.includes("href"));
  assert.ok(dangerous.includes("[no]"));
});

test("code spans cannot be spoofed by the sentinel appearing in the source", () => {
  const html = renderMarkdown("literal &#0;0&#0; next to `real code`");
  assert.ok(html.includes("&amp;#0;0&amp;#0;"));
  assert.ok(html.includes("<code>real code</code>"));
});

test("emphasis never runs inside code spans", () => {
  assert.equal(renderMarkdown("`a * b * c`"), "<p><code>a * b * c</code></p>");
});

test("an unclosed fence still renders as code while streaming", () => {
  const html = renderMarkdown("Intro:\n\n```python\nprint(1)");
  assert.ok(html.includes('<pre data-language="python">'));
  assert.ok(html.includes("print(1)"));
});

test("half-typed emphasis renders as itself", () => {
  assert.equal(renderMarkdown("a **b"), "<p>a **b</p>");
});

test("nested lists keep their structure", () => {
  const html = renderMarkdown("- one\n- two\n  - deep\n- three");
  assert.equal(html, "<ul><li>one</li><li>two<ul><li>deep</li></ul></li><li>three</li></ul>");
});

test("tables need a divider row to become a table", () => {
  assert.ok(renderMarkdown("| a | b |\n| --- | --- |\n| 1 | 2 |").includes("<table>"));
  assert.ok(!renderMarkdown("| a | b |\n| 1 | 2 |").includes("<table>"));
});

test("blockquotes render their own blocks", () => {
  assert.equal(renderMarkdown("> **hi**"), "<blockquote><p><strong>hi</strong></p></blockquote>");
});

test("escapeHtml covers every character that could start a tag or attribute", () => {
  assert.equal(escapeHtml(`<&>"'`), "&lt;&amp;&gt;&quot;&#39;");
});

test("durations read in the unit that suits their scale", () => {
  assert.equal(duration(340), "340ms");
  assert.equal(duration(4200), "4.2s");
  assert.equal(duration(42000), "42s");
  assert.equal(duration(72000), "1m 12s");
  assert.equal(duration(null), "");
});

test("day buckets group by calendar day, not elapsed hours", () => {
  const now = new Date();
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  assert.equal(dayBucket(now.toISOString()), "Today");
  assert.equal(dayBucket(yesterday.toISOString()), "Yesterday");
  assert.equal(dayBucket(null), "Earlier");
});

test("plural only pluralizes when it should", () => {
  assert.equal(plural(1, "step"), "1 step");
  assert.equal(plural(3, "step"), "3 steps");
});

test("the store notifies only on a real change", () => {
  const store = createStore({ count: 0 });
  const seen = [];
  store.subscribe((state) => seen.push(state.count));

  store.set({ count: 0 });
  store.set({ count: 1 });

  assert.deepEqual(seen, [0, 1]);
});

test("unsubscribing stops delivery", () => {
  const store = createStore({ value: "a" });
  let calls = 0;
  const off = store.subscribe(() => calls++);
  off();
  store.set({ value: "b" });

  assert.equal(calls, 1); // the immediate call on subscribe only
});

/** Minimal browser audio: every clip "plays" and ends on the next tick. */
function stubAudio() {
  globalThis.URL = { createObjectURL: () => "blob:test", revokeObjectURL: () => {} };
  globalThis.Audio = class {
    constructor() {
      this.paused = false;
      setTimeout(() => this.onended?.(), 0);
    }
    play() { this.paused = false; return Promise.resolve(); }
    pause() { this.paused = true; }
    removeAttribute() {}
    load() {}
  };
}

test("sentences are taken off the stream as soon as they are complete", () => {
  const first = takeSentences("Yes, that works. Now the ");
  assert.deepEqual(first.chunks, ["Yes, that works."]);
  assert.equal(first.rest, "Now the ");

  const second = takeSentences(`${first.rest}second part is done! And`);
  assert.deepEqual(second.chunks, ["Now the second part is done!"]);
  assert.equal(second.rest, "And");
});

test("markup written for the eye is not read out", () => {
  assert.equal(speakableText("**Bold** and `code`"), "Bold and code");
  assert.equal(speakableText("[the docs](https://example.com)"), "the docs");
  assert.equal(speakableText("## Heading"), "Heading");
  assert.equal(speakableText("> quoted"), "quoted");
});

test("screen-only blocks are dropped rather than read", () => {
  assert.equal(speakableText("```js\nvar x = 1;\n```"), "");
  assert.equal(speakableText("| a | b |\n| --- | --- |\n| 1 | 2 |"), "");
  assert.equal(speakableText("Before\n\n---\n\nAfter"), "Before\n\nAfter");
  assert.equal(speakableText("![a diagram](x.png)"), "");
});

test("paths are read by name, not by separator", () => {
  assert.equal(humanizePath("core/agent_factory.py"), "agent factory");
  assert.equal(humanizePath("web_chat/*"), "web chat");
  assert.equal(speakableText("see `core/action_policy.py` now"), "see action policy now");
  assert.equal(speakableText("changed web_chat/server.py today"), "changed server today");
});

test("prose in any script survives the path rules", () => {
  // `и/или` is a word pair, not a path: the rule only fires on ASCII tokens.
  assert.equal(speakableText("и/или это"), "и/или это");
  assert.equal(speakableText("Репозиторий на ветке `feature/Reasoning-view`"), "Репозиторий на ветке Reasoning view");
});

test("list items become plain sentences", () => {
  assert.equal(speakableText("- one\n- two"), "one\ntwo");
  assert.equal(speakableText("1. first\n2. second"), "first\nsecond");
  assert.equal(speakableText("- [x] done"), "done");
});

test("decoration and repeated punctuation are cleaned up", () => {
  assert.equal(speakableText("Done ✅ really!!!"), "Done really!");
  assert.equal(speakableText("Wait... ok"), "Wait… ok");
  assert.equal(speakableText("Read https://www.example.com/a/b now"), "Read example.com now");
});

test("a streamed answer is synthesized sentence by sentence, in order", async () => {
  stubAudio();
  const synthesized = [];
  const player = new SpeechPlayer({ synthesize: async (text) => { synthesized.push(text); return {}; } });

  const utterance = player.start("msg-1");
  utterance.push("First sentence here. Second sentence ");
  utterance.push("is also here. Tail");
  utterance.close();
  await utterance.finished;

  assert.deepEqual(synthesized, ["First sentence here.", "Second sentence is also here.", "Tail"]);
  assert.equal(player.activeId, null);
});

test("starting another message silences the first", async () => {
  stubAudio();
  const player = new SpeechPlayer({ synthesize: async () => new Promise(() => {}) });
  const first = player.start("msg-1");
  first.push("Something long. ");

  player.start("msg-2");

  assert.equal(player.activeId, "msg-2");
  assert.equal(player.isActive("msg-1"), false);
  await first.finished;
});

test("speech state is reported per message so the right button reacts", async () => {
  stubAudio();
  const seen = [];
  const player = new SpeechPlayer({
    synthesize: async () => ({}),
    onState: (id, state) => seen.push(`${id}:${state}`),
  });

  await player.speak("msg-7", "One sentence.");

  assert.ok(seen.includes("msg-7:loading"));
  assert.ok(seen.includes("msg-7:speaking"));
  assert.equal(seen.at(-1), "msg-7:idle");
});

test("synthesis is serialized: the engine has one slot, not three", async () => {
  stubAudio();
  let inFlight = 0;
  let peak = 0;
  const player = new SpeechPlayer({
    synthesize: async () => {
      peak = Math.max(peak, (inFlight += 1));
      await new Promise((resolve) => setTimeout(resolve, 5));
      inFlight -= 1;
      return {};
    },
  });

  await player.speak("msg-1", "One. Two. Three. Four. Five.");

  assert.equal(peak, 1);
});

test("a long answer is spoken past its first sentence", async () => {
  stubAudio();
  const spoken = [];
  const player = new SpeechPlayer({
    synthesize: async (text) => {
      spoken.push(text);
      return {};
    },
  });

  await player.speak("msg-1", "**Summary:**\nThe repo is ahead by 4 commits. There are 27 changed files. Also 3 deleted.");

  assert.deepEqual(spoken, [
    "Summary:",
    "The repo is ahead by 4 commits.",
    "There are 27 changed files.",
    "Also 3 deleted.",
  ]);
});
