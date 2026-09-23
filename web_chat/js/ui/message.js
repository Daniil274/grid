/**
 * A single chat message.
 *
 * The assistant's message is a stack: the reasoning timeline on top, the answer
 * below, hover actions at the bottom. Streaming re-renders the answer at most
 * once per frame, which keeps a fast token stream from pinning the main thread.
 */

import { $$, h, icon, onFrame } from "../lib/dom.js";
import { clock } from "../lib/format.js";
import { renderMarkdown } from "../lib/markdown.js";
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

function actionButton(label, path, onClick) {
  return h("button.msgAction", { type: "button", title: label, "aria-label": label, on: { click: onClick } }, icon(path, { size: 14 }));
}

/**
 * @param {object} options
 * @param {"user"|"assistant"|"system"} options.role
 * @param {string} [options.content]
 * @param {string} [options.author] agent name, shown next to the timestamp
 * @param {string|number} [options.timestamp]
 * @param {(text: string) => void} [options.onEdit] user messages only
 * @param {(id: string) => void} [options.onSpeak] assistant messages only
 */
export function createMessage({ role, content = "", author = "", timestamp, onEdit, onSpeak }) {
  const isAssistant = role === "assistant";
  const id = `msg-${(sequence += 1)}`;
  const body = h("div.msg__body");
  const reasoning = isAssistant ? createReasoningPanel() : null;

  let text = content;

  const speakButton =
    isAssistant && onSpeak
      ? actionButton(SPEECH_UI.idle.title, ICONS.volume, () => onSpeak(id))
      : null;

  const actions = h(
    "div.msg__actions",
    {},
    actionButton("Copy message", ICONS.copy, () => copyText(text, "Message copied")),
    role === "user" && onEdit ? actionButton("Edit and resend", ICONS.edit, () => onEdit(text)) : null,
    speakButton,
  );

  const authorNode = h("span.msg__author", { text: author || ROLE_LABEL[role] || role });
  const root = h(
    "article.msg",
    { id, dataset: { role } },
    h("div.msg__meta", {}, authorNode, timestamp ? h("span.msg__time", { text: clock(timestamp) }) : null),
    reasoning?.el,
    body,
    actions,
  );

  const paint = () => {
    body.innerHTML = renderMarkdown(text);
    decorateCode(body);
  };
  const paintSoon = onFrame(paint);

  if (text) paint();

  return {
    el: root,
    id,
    role,
    reasoning,

    get text() {
      return text;
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
  };
}
