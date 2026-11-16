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
import { decorateDiagrams } from "../lib/diagrams.js";
import { renderMarkdown, renderMarkdownBlocks } from "../lib/markdown.js";
import { decorateMath } from "../lib/math.js";
import { scopeWorkspaceLinks } from "../lib/workspace-links.js";
import { ICONS } from "./icons.js";
import { createActivityView } from "./activity.js";
import { renderUiBlock } from "./ui-block.js";
import { copyText, toast } from "./toast.js";

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

/**
 * Paint markdown into *container* one block at a time.
 *
 * A block that is already written renders the same HTML however the answer
 * continues, so its node is kept and only the block still being written is
 * rebuilt and decorated. A streaming answer therefore costs one small
 * insertion per frame instead of re-parsing and re-highlighting the whole
 * message, and a closed block keeps the node - and the work - it got once.
 *
 * *key* names the growing document (the message's element id), so two answers
 * streaming at once do not evict each other's cached blocks.
 */
function createBlockView(container, key) {
  /** One entry per painted block: the HTML it came from and the nodes it made. */
  let entries = [];

  const drop = (from) => {
    for (let index = from; index < entries.length; index += 1) {
      for (const node of entries[index].nodes) node.remove();
    }
    entries = entries.slice(0, from);
  };

  /** Remove children this view did not put there (a pulse, a notice, an editor). */
  const sweep = () => {
    const own = new Set();
    for (const entry of entries) for (const node of entry.nodes) own.add(node);
    for (const child of [...container.childNodes]) if (!own.has(child)) child.remove();
  };

  return {
    paint(source) {
      const blocks = renderMarkdownBlocks(source, key);
      const owned = entries.every((entry) => entry.nodes.every((node) => node.parentNode === container));
      if (!owned) {
        // The body was rebuilt elsewhere (the editor, an error box): start over.
        drop(0);
        container.replaceChildren();
      } else {
        sweep();
      }
      let keep = 0;
      while (keep < entries.length && keep < blocks.length && entries[keep].html === blocks[keep]) keep += 1;
      if (keep === entries.length && keep === blocks.length) return;
      drop(keep);
      for (let index = keep; index < blocks.length; index += 1) {
        // Built and decorated in a detached holder: a fenced block is itself a
        // <pre>, and decoration looks for one inside its scope.
        const holder = h("div");
        holder.innerHTML = blocks[index];
        decorateCode(holder);
        decorateDiagrams(holder);
        decorateMath(holder);
        scopeWorkspaceLinks(holder);
        const nodes = [...holder.childNodes];
        for (const node of nodes) container.append(node);
        entries.push({ html: blocks[index], nodes });
      }
    },
  };
}

function actionButton(label, path, onClick, extraClass = "") {
  return h("button.msgAction", { type: "button", class: extraClass, title: label, "aria-label": label, on: { click: onClick } }, icon(path, { size: 14 }));
}

/**
 * A prompt size for people: "812", "123k", "1.2m".
 *
 * The exact number is in the title; the row shows the magnitude that makes
 * a growing context visible at a glance.
 */
function contextSize(tokens) {
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1).replace(/\.0$/, "")}m`;
  if (tokens >= 1000) return `${Math.round(tokens / 1000)}k`;
  return String(tokens);
}

/**
 * @param {object} options
 * @param {"user"|"assistant"|"system"} options.role
 * @param {string} [options.content]
 * @param {string} [options.author] agent name, shown next to the timestamp
 * @param {string|number} [options.timestamp]
 * @param {(edit: {id: string, text: string, images: string[]}) => void} [options.onEdit]
 *   user messages only: saving an edit starts a new branch
 * @param {(target: {id: string, role: string}) => void} [options.onRetry]
 *   user and assistant messages: repeat the request behind this message - the
 *   message itself for the user's, the request the answer answers for an
 *   assistant's - as a new branch with the text untouched
 * @param {(contextId: string) => void} [options.onSwitchVersion] opens another
 *   version of an edited message
 * @param {?string} [options.messageId] the stored message's id
 * @param {?{index: number, total: number, targets: string[]}} [options.versions]
 * @param {(id: string) => void} [options.onSpeak] assistant messages only
 * @param {(answer: {id: string, text: string}) => void} [options.onReport] assistant
 *   messages only: report a problem with this answer for review
 * @param {() => void} [options.onContinue] resumes an interrupted turn
 * @param {(target: {id: string, role: string}) => void} [options.onDelete] deletes a
 *   stored message from the chat and the agent's context
 * @param {(id: string) => void} [options.onFork] assistant messages only: a new
 *   chat up to the end of this turn
 * @param {() => void} [options.onContinueInNewChat] assistant messages only:
 *   carry this chat's context into a new one
 * @param {"continuation"|"compaction"|null} [options.kind] a marker, not a bubble
 * @param {?object|string} [options.context] kind="context" only: where the seed
 *   carried into the chat came from (`{source_title}`/`{source_id}`/`{project}`)
 * @param {?{tokens_before: ?number, tokens_after: ?number}} [options.compaction]
 *   token counts for a compaction marker
 * @param {string[]} [options.images] data URLs of attached images
 * @param {?{turn_id: string, files: number}} [options.checkpoint] user messages only:
 *   the turn changed files, and `onRestore` brings them back to before it
 * @param {(checkpoint: {turn_id: string, files: number}) => void} [options.onRestore]
 * @param {object[]} [options.events] assistant messages only: the public typed
 *   events the server stored with the message (`message.events`)
 * @param {?number} [options.contextTokens] assistant messages only: the prompt
 *   size of the turn's last model call, shown beside the actions
 */
export function createMessage({
  role, content = "", author = "", timestamp, onEdit, onSpeak, onReport, onContinue, onSwitchVersion, onDelete, onFork,
  onRetry, onContinueInNewChat, onRestore, kind = null, compaction = null, context = null, images = [], messageId = null, versions = null,
  checkpoint = null, events = [], executionMode = "segmented", onExecutionModeToggle = null,
  ui = [], origin = null, onUiSave = null, onUiSubmit = null, contextTokens = null,
}) {
  if (kind === "continuation") return createContinuationMarker({ timestamp });
  if (kind === "compaction") return createCompactionMarker({ timestamp, compaction });
  if (kind === "context") return createContextCard({ timestamp, context, content });
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
  // One chronological stream owns both halves of the turn: every public
  // message closes its execution panel, and the next step starts another.
  const activity = isAssistant ? createActivityView({ mode: executionMode }).setAuthor(author) : null;
  const reasoning = activity;
  let currentExecutionMode = executionMode;

  let text = content;
  // A user's uploaded files close their text (lib/files.js): shown as files,
  // kept in the text the agent reads, and put back when the text is edited.
  const ownWords = () => (role === "user" ? splitFiles(text) : { text, files: [] });
  let interruptBox = null;
  // Revision notices, one line per machine code: a re-pin is not an error.
  const revisionNotes = new Map();
  // Under the author line, above the trace and the answer: a notice describes
  // the request, not the answer it produced.
  const revisionLines = h("div.msg__revisions", { hidden: true });
  let refusalBox = null;
  let resumable = false;
  let storedId = messageId;
  let editing = false;

  const speakButton =
    isAssistant && onSpeak
      ? actionButton(SPEECH_UI.idle.title, ICONS.volume, () => onSpeak(id), "msgAction--speak")
      : null;

  // "Restore files": shown once the turn this message started changed files.
  let currentCheckpoint = null;
  const restoreButton = role === "user" && onRestore
    ? actionButton("Restore files to before this message", ICONS.undo, () => currentCheckpoint && onRestore(currentCheckpoint))
    : null;
  const showCheckpoint = (next) => {
    currentCheckpoint = next?.files ? next : null;
    if (!restoreButton) return;
    restoreButton.hidden = !currentCheckpoint;
    if (currentCheckpoint) {
      const label = `Restore files to before this message (${currentCheckpoint.files} changed since)`;
      restoreButton.title = label;
      restoreButton.setAttribute("aria-label", label);
    }
  };
  showCheckpoint(checkpoint);

  // "Repeat request": the same request runs again from this point - the edit
  // flow with the text untouched, so the repeat is a new version of the
  // message rather than a copy of it further down the chat.
  const retryButton = (role === "user" || isAssistant) && onRetry
    ? actionButton(
      role === "user"
        ? "Repeat this request (starts a new branch)"
        : "Repeat the request behind this answer (starts a new branch)",
      ICONS.repeat,
      () => stored(() => onRetry({ id: storedId, role })),
      "msgAction--retry",
    )
    : null;

  const executionModeButton = isAssistant && onExecutionModeToggle
    ? h("button.msgAction.msgAction--executionMode", {
        type: "button",
        title: currentExecutionMode === "combined" ? "Show work by messages" : "Show work as one block",
        "aria-label": currentExecutionMode === "combined" ? "Show work by messages" : "Show work as one block",
        "aria-pressed": String(currentExecutionMode === "combined"),
        on: { click: onExecutionModeToggle },
      }, icon(ICONS.panel, { size: 14 }))
    : null;

  // The turn's ending context size, beside the actions: a number, not a
  // button, and absent until the turn reports one.
  const contextTokensNode = isAssistant
    ? h("span.msg__contextTokens", {
        hidden: true,
        title: "Context tokens after this message",
      })
    : null;

  const actions = h(
    "div.msg__actions",
    {},
    actionButton("Copy message", ICONS.copy, () => copyText(ownWords().text, "Message copied")),
    role === "user" && onEdit ? actionButton("Edit (starts a new branch)", ICONS.edit, () => startEdit()) : null,
    retryButton,
    restoreButton,
    speakButton,
    executionModeButton,
    isAssistant && onReport
      ? actionButton("Report a problem with this answer", ICONS.inspect, () => startReport(), "msgAction--report")
      : null,
    isAssistant && onFork ? actionButton("Fork: a new chat up to here", ICONS.fork, () => stored(() => onFork(storedId))) : null,
    onDelete
      ? actionButton(
        role === "user" ? "Delete this message and its answer (chat and context)" : "Delete from chat and context",
        ICONS.trash,
        () => stored(() => onDelete({ id: storedId, role })),
        "msgAction--delete",
      )
      : null,
    contextTokensNode,
  );

  /** Show the current context size; nothing at all when it is unknown. */
  function setContextTokens(tokens) {
    if (!contextTokensNode) return;
    const count = Number(tokens);
    const known = Number.isFinite(count) && count > 0;
    contextTokensNode.hidden = !known;
    contextTokensNode.textContent = known ? contextSize(count) : "";
    contextTokensNode.title = known
      ? `Context tokens after this message: ${Math.round(count).toLocaleString("en-US")}`
      : "Context tokens after this message";
  }
  setContextTokens(contextTokens);

  /** Run *action* once the message is stored; a message in flight has no id yet. */
  function stored(action) {
    if (storedId) {
      action();
      return;
    }
    toast("This message can be changed once its turn is saved.", { timeout: 4000 });
  }

  // The newest answer offers to carry this chat's context into a new one; the
  // transcript shows it on the last answer only (setContinueInNewChat).
  const continueInNewChat = isAssistant && onContinueInNewChat
    ? h(
        "div.msg__newChat",
        {},
        h(
          "button.btn.btn--ghost.msg__continueNew",
          {
            type: "button",
            title: "Carry this chat's context into a new one",
            on: { click: () => stored(() => onContinueInNewChat(storedId)) },
          },
          icon(ICONS.resume, { size: 13 }),
          "Continue in new chat",
        ),
      )
    : null;
  if (continueInNewChat) continueInNewChat.hidden = true;

  // Where a request came from, when a button of an interface sent it: the
  // server composed and checked the sentence, so this line only says which
  // control the person used - it never invents or repeats the prompt.
  const originLine = origin?.source === "ui"
    ? h("p.msg__origin", {
        dataset: { uiId: origin.ui_id ?? "", action: origin.action_id ?? "" },
        title: origin.ui_id ? `Interface ${origin.ui_id}, values as sent` : "",
        text: `Chosen in the interface${origin.action_label ? `: ${origin.action_label}` : ""}`,
      })
    : null;

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
  // Interfaces the answer presented (web_chat/ui_document.py), right under its
  // text: part of the answer, not a tool card. Each widget owns its local
  // choices and talks to the server only to save them or to send a button.
  const uiSlot = h("div.msg__ui");
  const uiWidgets = new Map();
  const addUiBlock = (block) => {
    if (!block || typeof block !== "object" || !block.id || uiWidgets.has(block.id)) return;
    const widget = renderUiBlock(block, {
      getMessageId: () => storedId,
      onSave: onUiSave,
      onSubmit: onUiSubmit,
    });
    uiWidgets.set(block.id, widget);
    uiSlot.append(widget.el);
    uiSlot.hidden = false;
  };
  for (const block of ui ?? []) addUiBlock(block);
  uiSlot.hidden = uiWidgets.size === 0;

  const gallery = h("div.msg__images", { hidden: !images?.length });
  for (const url of images ?? []) gallery.append(renderImage(url));
  const fileList = h("div.msg__files", { hidden: true });
  const root = h(
    "article.msg",
    { id, dataset: { role } },
    h("div.msg__meta", {}, authorNode, timestamp ? h("span.msg__time", { text: clock(timestamp) }) : null, switcher),
    revisionLines,
    originLine,
    activity?.el,
    isAssistant ? null : gallery,
    isAssistant ? null : fileList,
    body,
    isAssistant ? uiSlot : null,
    isAssistant ? gallery : null,
    actions,
    continueInNewChat,
  );

  const view = createBlockView(body, id);

  if (activity && events?.length) activity.apply(events);

  const paint = () => {
    const { text: words, files } = ownWords();
    if (!isAssistant) {
      fileList.replaceChildren(...files.map(renderFile));
      fileList.hidden = !files.length;
    }
    view.paint(words);
    // Workspace-file links the agent emits (/api/workspace/files/<path>)
    // download on click, from the directory this chat works in.
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
    kind: null,
    reasoning,
    activity,

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
    /** The turn this message started changed files: offer Restore. */
    setCheckpoint(next) {
      showCheckpoint(next);
    },

    adopt({ id: nextId, versions: nextVersions, checkpoint: nextCheckpoint, events: nextEvents, contextTokens: nextContextTokens }) {
      storedId = nextId ?? storedId;
      // A block shown during the live turn could not reach the server yet;
      // now that the answer has an id its buttons and saves can work.
      for (const widget of uiWidgets.values()) widget.refresh();
      renderVersions(nextVersions);
      showCheckpoint(nextCheckpoint ?? null);
      // A reconnect redraws nothing twice: the activity view dedupes by step.
      if (nextEvents?.length) activity?.apply(nextEvents);
      if (nextContextTokens) setContextTokens(nextContextTokens);
    },

    /** The prompt size of the turn's last model call, beside the actions. */
    setContextTokens,

    /** Show an interface the answer just presented, once. */
    addUiBlock(block) {
      addUiBlock(block);
      uiSlot.hidden = uiWidgets.size === 0;
    },

    /** Pass a server acknowledgement to the widget that sent the request. */
    acknowledgeUi(uiId, patch) {
      uiWidgets.get(uiId)?.acknowledge?.(patch);
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
      activity?.setAuthor(name);
    },

    /** Show this turn as one timeline or split it at public messages. */
    setExecutionMode(mode) {
      currentExecutionMode = mode;
      activity?.setMode(mode);
      if (executionModeButton) {
        const combined = mode === "combined";
        const label = combined ? "Show work by messages" : "Show work as one block";
        executionModeButton.title = label;
        executionModeButton.setAttribute("aria-label", label);
        executionModeButton.setAttribute("aria-pressed", String(combined));
      }
    },

    /** One live step frame: a top-level message joins the public stream. */
    observeStep(step) {
      activity?.observeStep(step);
    },

    /** One live typed event (`event` frames of a running turn, when sent). */
    applyEvent(event) {
      activity?.add(event);
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

    /**
     * The conversation was pinned to another Grid version or system
     * configuration and this turn continues on the current one: one quiet
     * line, not an error - nothing failed and the answer follows.
     * `code` is the machine reason (`revision.*`); the text is only for people.
     */
    addRevisionNotice({ code = null, content = "" } = {}) {
      if (!content) return;
      const key = code ?? content;
      const known = revisionNotes.get(key);
      if (known) {
        known.textContent = content;
        return;
      }
      const line = h("p.msg__notice.msg__revision", {
        text: content,
        title: code ?? "",
        dataset: { code: code ?? "" },
      });
      revisionNotes.set(key, line);
      revisionLines.append(line);
      revisionLines.hidden = false;
    },

    /**
     * A turn this runtime cannot continue: the reason, and - when the caller
     * knows a way out - the button that carries the conversation into a new
     * chat on the current configuration.
     * @param {string} reason the server's sentence
     * @param {{code?: ?string, onContinue?: ?Function}} [options]
     */
    setRevisionRefusal(reason, { code = null, onContinue = null } = {}) {
      root.classList.add("is-failed");
      root.classList.remove("is-waiting");
      body.querySelector(".typing")?.remove();
      const action = onContinue
        ? h(
            "button.btn.btn--primary.msg__continue",
            {
              type: "button",
              title: "Carry this conversation's history into a new chat on the current configuration",
              on: { click: () => onContinue() },
            },
            icon(ICONS.resume, { size: 13 }),
            "Continue in new configuration",
          )
        : null;
      refusalBox?.remove();
      refusalBox = h(
        "div.msg__error",
        { role: "alert", dataset: { code: code ?? "" } },
        h("strong", { text: "Cannot continue here" }),
        h("p", { text: reason }),
        action,
      );
      if (text) body.append(refusalBox);
      else body.replaceChildren(refusalBox);
    },

    /** Offer "Continue in new chat" - the transcript shows it on the last answer. */
    setContinueInNewChat(available) {
      if (!continueInNewChat) return;
      continueInNewChat.hidden = !available;
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

/** Where a carried-context seed says it came from, in the reader's words. */
function contextSource(context) {
  if (!context) return "an earlier chat";
  if (typeof context === "string") return context;
  const title = context.source_title || context.source || context.title;
  if (title) return title;
  if (context.project_title) return `the project ${context.project_title}`;
  if (context.project) return `the project ${context.project}`;
  if (context.source_id) return context.source_id;
  return "an earlier chat";
}

/**
 * The context a chat carried in from another chat or from its project: a
 * collapsed, never-editable card. It is a copy of what the model was given,
 * not something the reader wrote, so it has no edit, copy or version action -
 * a click only opens it to read.
 */
function createContextCard({ timestamp, context, content = "" }) {
  const id = `msg-${(sequence += 1)}`;
  const body = h("div.msg__contextBody");
  body.hidden = true;
  const chevron = h("span.msg__contextChevron", {}, icon(ICONS.chevron, { size: 12 }));
  const head = h(
    "button.msg__contextHead",
    { type: "button", "aria-expanded": "false", title: "Read the context this chat started with" },
    chevron,
    icon(ICONS.message, { size: 13 }),
    h("span.msg__contextLabel", { text: `Context carried from ${contextSource(context)}` }),
  );
  head.addEventListener("click", () => {
    const open = body.hidden;
    body.hidden = !open;
    head.setAttribute("aria-expanded", String(open));
    chevron.classList.toggle("is-open", open);
  });
  if (content) {
    body.innerHTML = renderMarkdown(content);
    decorateCode(body);
    decorateDiagrams(body);
    decorateMath(body);
    scopeWorkspaceLinks(body);
  }
  const root = h(
    "article.msg.msg--context",
    { id, dataset: { role: "marker" } },
    h("div.msg__meta", {}, h("span.msg__author", { text: "Context" }), timestamp ? h("span.msg__time", { text: clock(timestamp) }) : null),
    head,
    body,
  );
  return {
    el: root,
    id,
    role: "marker",
    kind: "context",
    reasoning: null,
    text: content,
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
    setContinueInNewChat() {},
    addImage() {},
    adopt() {},
    messageId: null,
    versions: null,
    images: [],
  };
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
