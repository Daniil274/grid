/**
 * A single chat message.
 *
 * The assistant's message is a stack: the reasoning timeline on top, the answer
 * below, hover actions at the bottom. Streaming re-renders the answer at most
 * once per frame, which keeps a fast token stream from pinning the main thread.
 */

import { $$, h, icon, onFrame } from "../lib/dom.js";
import { splitFiles, withFiles } from "../lib/files.js";
import { clock } from "../lib/format.js";
import { renderMarkdown } from "../lib/markdown.js";
import { scopeWorkspaceLinks } from "../lib/workspace-links.js";
import { ICONS } from "./icons.js";
import { createReasoningPanel } from "./reasoning.js";
import { copyText } from "./toast.js";

const ROLE_LABEL = { assistant: "Grid", user: "You", system: "System" };

/** Button title and glyph per speech state. */
const SPEECH_UI = {
  idle: { icon: "volume", title: "Read this answer aloud" },
  loading: { icon: "volume", title: "Preparing audio…" },
  speaking: { icon: "volumeOff", title: "Stop reading" },
  error: { icon: "volume", title: "Audio failed - click to retry" },
};

/** Heading of a turn that stopped early, by `interruption.reason`. */
const STOP_LABEL = {
  user_stop: "Stopped",
  timeout: "Timed out",
  max_turns: "Turn limit reached",
  error: "Stopped on an error",
  crash: "Interrupted by a restart",
};

let sequence = 0;

/** Give each code block a language tag and a copy button. */
function decorateCode(container) {
  for (const pre of $$("pre", container)) {
    if (pre.dataset.decorated) continue;
    pre.dataset.decorated = "true";
    const code = pre.querySelector("code")?.textContent ?? "";
    pre.prepend(
      h(
        "div.codeBar",
        {},
        h("span.codeBar__lang", { text: pre.dataset.language || "text" }),
        h("button.iconBtn.iconBtn--xs", {
          type: "button",
          title: "Copy code",
          on: { click: () => copyText(code, "Code copied") },
        }, icon(ICONS.copy, { size: 13 })),
      ),
    );
  }
}

function actionButton(label, path, onClick, extraClass = "") {
  return h("button.msgAction", { type: "button", class: extraClass, title: label, "aria-label": label, on: { click: onClick } }, icon(path, { size: 14 }));
}

/**
 * @param {object} options
 * @param {"user"|"assistant"|"system"} options.role
 * @param {string} [options.content]
 * @param {string} [options.author] agent name, shown next to the timestamp
 * @param {string|number} [options.timestamp]
 * @param {(edit: {id: string, text: string, images: string[]}) => void} [options.onEdit]
 *   user messages only: saving an edit starts a new branch
 * @param {(contextId: string) => void} [options.onSwitchVersion] opens another
 *   version of an edited message
 * @param {?string} [options.messageId] the stored message's id
 * @param {?{index: number, total: number, targets: string[]}} [options.versions]
 * @param {(id: string) => void} [options.onSpeak] assistant messages only
 * @param {(answer: {id: string, text: string}) => void} [options.onReport] assistant
 *   messages only: report a problem with this answer for review
 * @param {() => void} [options.onContinue] resumes an interrupted turn
 * @param {"continuation"|"compaction"|null} [options.kind] a marker, not a bubble
 * @param {?{tokens_before: ?number, tokens_after: ?number}} [options.compaction]
 *   token counts for a compaction marker
 * @param {string[]} [options.images] data URLs of attached images
 */
export function createMessage({
  role, content = "", author = "", timestamp, onEdit, onSpeak, onReport, onContinue, onSwitchVersion,
  kind = null, compaction = null, images = [], messageId = null, versions = null,
}) {
  if (kind === "continuation") return createContinuationMarker({ timestamp });
  if (kind === "compaction") return createCompactionMarker({ timestamp, compaction });
  const isAssistant = role === "assistant";
  const id = `msg-${(sequence += 1)}`;
  const body = h("div.msg__body");
  // Markdown images are rebuilt on every paint, so the zoom toggle is caught on
  // the stable container: a workspace image expands in place when clicked.
  body.addEventListener("click", (event) => {
    if (event.target?.classList?.contains("msg__inlineImage")) {
      event.target.classList.toggle("is-expanded");
    }
  });
  const reasoning = isAssistant ? createReasoningPanel() : null;

  let text = content;
  // A user's uploaded files close their text (lib/files.js): shown as files,
  // kept in the text the agent reads, and put back when the text is edited.
  const ownWords = () => (role === "user" ? splitFiles(text) : { text, files: [] });
  let interruptBox = null;
  let resumable = false;
  let storedId = messageId;
  let editing = false;

  const speakButton =
    isAssistant && onSpeak
      ? actionButton(SPEECH_UI.idle.title, ICONS.volume, () => onSpeak(id), "msgAction--speak")
      : null;

  const actions = h(
    "div.msg__actions",
    {},
    actionButton("Copy message", ICONS.copy, () => copyText(ownWords().text, "Message copied")),
    role === "user" && onEdit ? actionButton("Edit (starts a new branch)", ICONS.edit, () => startEdit()) : null,
    speakButton,
    isAssistant && onReport
      ? actionButton("Report a problem with this answer", ICONS.inspect, () => startReport(), "msgAction--report")
      : null,
  );

  const authorNode = h("span.msg__author", { text: author || ROLE_LABEL[role] || role });
  // "‹ 2/3 ›": the versions of an edited message, each in its own branch.
  const switcher = h("span.msg__versions");
  let currentVersions = null;
  const renderVersions = (info) => {
    currentVersions = info ?? null;
    switcher.replaceChildren();
    if (!info || info.total < 2 || !onSwitchVersion) return;
    const go = (step) => onSwitchVersion(info.targets[info.index + step]);
    switcher.append(
      h("button.msgAction.msgAction--xs", {
        type: "button", title: "Previous version", "aria-label": "Previous version",
        disabled: info.index === 0, on: { click: () => go(-1) },
      }, icon(ICONS.chevron, { size: 12, className: "icon icon--flip" })),
      h("span.msg__versionCount", { text: `${info.index + 1}/${info.total}` }),
      h("button.msgAction.msgAction--xs", {
        type: "button", title: "Next version", "aria-label": "Next version",
        disabled: info.index === info.total - 1, on: { click: () => go(1) },
      }, icon(ICONS.chevron, { size: 12 })),
    );
  };
  renderVersions(versions);
  // A user's attachments sit above their text; an assistant's generated
  // images follow its answer.
  const gallery = h("div.msg__images", { hidden: !images?.length });
  for (const url of images ?? []) gallery.append(renderImage(url));
  const fileList = h("div.msg__files", { hidden: true });
  const root = h(
    "article.msg",
    { id, dataset: { role } },
    h("div.msg__meta", {}, authorNode, timestamp ? h("span.msg__time", { text: clock(timestamp) }) : null, switcher),
    reasoning?.el,
    reasoning?.planEl,
    isAssistant ? null : gallery,
    isAssistant ? null : fileList,
    body,
    isAssistant ? gallery : null,
    actions,
  );

  const paint = () => {
    const { text: words, files } = ownWords();
    if (!isAssistant) {
      fileList.replaceChildren(...files.map(renderFile));
      fileList.hidden = !files.length;
    }
    body.innerHTML = renderMarkdown(words);
    decorateCode(body);
    // Workspace-file links the agent emits (/api/workspace/files/<path>)
    // download on click, from the directory this chat works in.
    scopeWorkspaceLinks(body);
    if (!isAssistant) scopeWorkspaceLinks(fileList);
  };
  const paintSoon = onFrame(paint);

  if (text) paint();

  const startReport = () => {
    if (!storedId) {
      body.append(h("p.msg__notice", { text: "This answer can be reported once its turn is saved." }));
      return;
    }
    onReport({ id: storedId, text });
  };

  /** The text becomes an editor in place; saving sends it into a new branch. */
  const startEdit = () => {
    if (editing) return;
    if (!storedId) {
      // Just sent: the id arrives when the turn is stored.
      body.append(h("p.msg__notice", { text: "This message can be edited once its turn is saved." }));
      return;
    }
    editing = true;
    root.classList.add("is-editing");
    const { text: words, files } = ownWords();
    const input = h("textarea.msg__editor", { rows: Math.min(12, words.split("\n").length + 1), "aria-label": "Edit message" });
    input.value = words;
    const finish = (save) => {
      const next = input.value.trim();
      editing = false;
      root.classList.remove("is-editing");
      paint();
      if (save && (next || files.length) && next !== words.trim()) {
        onEdit({ id: storedId, text: withFiles(next, files), images: [...gallery.querySelectorAll("img")].map((node) => node.src) });
      }
    };
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        finish(true);
      } else if (event.key === "Escape") finish(false);
    });
    body.replaceChildren(
      input,
      h(
        "div.msg__editBar",
        {},
        h("span.msg__editHint", { text: "Saving starts a new branch; this version stays." }),
        h("button.btn.btn--ghost", { type: "button", on: { click: () => finish(false) } }, "Cancel"),
        h("button.btn.btn--primary", { type: "button", on: { click: () => finish(true) } }, "Save & send"),
      ),
    );
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  };

  return {
    el: root,
    id,
    role,
    reasoning,

    get text() {
      return text;
    },

    /** An interruption that Continue can still resume. */
    get resumable() {
      return resumable;
    },

    /** The stored message's id, once its turn was saved. */
    get messageId() {
      return storedId;
    },

    /** Versions of this edited message, or null. */
    get versions() {
      return currentVersions;
    },

    /** Data URLs of the images the message shows. */
    get images() {
      return [...gallery.querySelectorAll("img")].map((node) => node.src);
    },

    /** Take the stored id and versions without re-rendering the message. */
    adopt({ id: nextId, versions: nextVersions }) {
      storedId = nextId ?? storedId;
      renderVersions(nextVersions);
    },

    /** Show an image the model generated, once. */
    addImage(url) {
      if ([...gallery.children].some((node) => node.getAttribute("src") === url)) return;
      gallery.append(renderImage(url));
      gallery.hidden = false;
      root.classList.remove("is-waiting");
      body.querySelector(".typing")?.remove();
    },

    /** Reflect what the speech player is doing with this message. */
    setSpeechState(state) {
      if (!speakButton) return;
      const ui = SPEECH_UI[state] ?? SPEECH_UI.idle;
      speakButton.dataset.speech = state;
      speakButton.title = ui.title;
      speakButton.setAttribute("aria-label", ui.title);
      speakButton.replaceChildren(icon(ICONS[ui.icon], { size: 14 }));
      // A speaking message keeps its controls visible: the stop must be there
      // the moment the audio turns out to be wrong.
      root.classList.toggle("is-speaking", state === "speaking" || state === "loading");
    },

    /** Name the agent that took the turn, once routing has decided. */
    setAuthor(name) {
      authorNode.textContent = name;
    },

    /** Replace the answer (final output, or a restored message). */
    setText(next) {
      text = next ?? "";
      paint();
    },

    /** Append a streamed token; repainting is coalesced into one frame. */
    appendText(delta) {
      text += delta;
      root.classList.remove("is-waiting");
      paintSoon();
    },

    /** Show the pulse while the runtime is working and nothing is on screen yet.
     * Owns only the indicator: an error or a first token must survive this. */
    setWaiting(waiting) {
      const show = waiting && !text;
      root.classList.toggle("is-waiting", show);
      if (show && !body.querySelector(".typing")) {
        body.append(h("span.typing", {}, h("i"), h("i"), h("i")));
      } else if (!show) {
        body.querySelector(".typing")?.remove();
      }
    },

    /** State a turn that ran but produced no answer - never a blank bubble. */
    setNotice(notice) {
      if (text) return;
      body.replaceChildren(h("p.msg__notice", { text: notice }));
    },

    /** Mark a turn that ended badly, so the message reads as incomplete. */
    setFailed(message) {
      root.classList.add("is-failed");
      root.classList.remove("is-waiting");
      const notice = h("div.msg__error", { role: "alert" }, h("strong", { text: "Error" }), h("p", { text: message }));
      if (text) {
        body.querySelector(".msg__error")?.remove();
        body.append(notice);
      } else body.replaceChildren(notice);
    },

    /**
     * Mark a turn that stopped before its answer: why, and - while it can still
     * be resumed - a Continue that picks it up with everything it had done.
     * @param {{reason: string}} interruption the stored record
     * @param {{summary?: string, resumable?: boolean}} [options]
     */
    setInterrupted(interruption, { summary = "", resumable: canResume = false } = {}) {
      resumable = Boolean(canResume && onContinue);
      root.classList.add("is-interrupted");
      root.classList.remove("is-waiting");
      body.querySelector(".typing")?.remove();
      if (!text && summary) this.setText(summary);
      interruptBox?.remove();
      interruptBox = h(
        "div.msg__interrupt",
        { role: "status" },
        h("strong", { text: STOP_LABEL[interruption?.reason] ?? "Interrupted" }),
        resumable
          ? h(
              "button.btn.btn--primary.msg__continue",
              { type: "button", title: "Continue from where the agent stopped", on: { click: () => onContinue() } },
              icon(ICONS.resume, { size: 13 }),
              "Continue",
            )
          : null,
      );
      root.insertBefore(interruptBox, actions);
    },

    /** Take away Continue once another turn has started. */
    retireContinue() {
      resumable = false;
      interruptBox?.querySelector(".msg__continue")?.remove();
    },
  };
}

/** One image of a message; a click shows it at full width and back. */
/** An uploaded file: its icon, name and type; a click downloads it. */
function renderFile({ name, url }) {
  const extension = name.includes(".") ? name.split(".").pop().toUpperCase() : "File";
  return h(
    "a.msgFile",
    { href: url, download: name, title: `Download ${name}` },
    icon(ICONS.file, { size: 22 }),
    h("span.msgFile__info", {},
      h("span.msgFile__name", { text: name }),
      h("span.msgFile__type", { text: extension })),
  );
}

function renderImage(url) {
  return h("img.msg__image", {
    src: url,
    alt: "Image",
    loading: "lazy",
    on: { click: (event) => event.currentTarget.classList.toggle("is-expanded") },
  });
}

/** The stored user entry of a Continue: a thin marker in the thread. */
function createContinuationMarker({ timestamp }) {
  const id = `msg-${(sequence += 1)}`;
  const root = h(
    "article.msg.msg--marker",
    { id, dataset: { role: "marker" } },
    h("span.msg__marker", {}, icon(ICONS.resume, { size: 12 }), "Continued", timestamp ? ` · ${clock(timestamp)}` : ""),
  );
  return {
    el: root,
    id,
    role: "marker",
    reasoning: null,
    text: "",
    resumable: false,
    setSpeechState() {},
    setAuthor() {},
    setText() {},
    appendText() {},
    setWaiting() {},
    setNotice() {},
    setFailed() {},
    setInterrupted() {},
    retireContinue() {},
    addImage() {},
    adopt() {},
    messageId: null,
    versions: null,
    images: [],
  };
}

/**
 * A display-only marker where the thread's context was compacted.
 *
 * Compaction rewrites the agent session, not this visible log, so without a
 * marker the thread would silently skip a stretch of history. The counts are
 * approximate and omitted when missing.
 */
function createCompactionMarker({ timestamp, compaction }) {
  const id = `msg-${(sequence += 1)}`;
  const before = Number(compaction?.tokens_before);
  const after = Number(compaction?.tokens_after);
  const counts = Number.isFinite(before) && Number.isFinite(after)
    ? ` · ~${compactCount(before)} → ~${compactCount(after)} tokens`
    : "";
  const root = h(
    "article.msg.msg--marker.msg--compaction",
    { id, dataset: { role: "marker" } },
    h(
      "span.msg__marker",
      {},
      icon(ICONS.compact, { size: 12 }),
      "Context compacted",
      counts,
      timestamp ? ` · ${clock(timestamp)}` : "",
    ),
  );
  return {
    el: root,
    id,
    role: "marker",
    kind: "compaction",
    reasoning: null,
    text: "",
    resumable: false,
    setSpeechState() {},
    setAuthor() {},
    setText() {},
    appendText() {},
    setWaiting() {},
    setNotice() {},
    setFailed() {},
    setInterrupted() {},
    retireContinue() {},
    addImage() {},
    adopt() {},
    messageId: null,
    versions: null,
    images: [],
  };
}

/** Round a token count to a short, human-scale label (1.2k, 34k, 1.1M). */
function compactCount(value) {
  if (value < 1000) return String(value);
  if (value < 1000000) return `${Math.round(value / 100) / 10}k`;
  return `${Math.round(value / 100000) / 10}M`;
}
