/** Transient notifications. Errors used to reach only the console; now they surface. */

import { h, icon } from "../lib/dom.js";
import { ICONS } from "./icons.js";

const TONE_ICON = { info: ICONS.check, success: ICONS.check, error: ICONS.error, attention: ICONS.clock };

let host = null;

function ensureHost() {
  if (!host) {
    host = h("div.toasts", { role: "status", "aria-live": "polite" });
    document.body.append(host);
  }
  return host;
}

/**
 * @param {string} message
 * @param {{tone?: "info"|"success"|"error"|"attention", timeout?: number,
 *          actions?: {label: string, run: () => void}[]}} [options]
 *   `timeout` 0 keeps the toast until it is clicked or dismissed; each action is
 *   a button that runs and then dismisses it.
 * @returns {() => void} dismiss
 */
export function toast(message, { tone = "info", timeout = tone === "error" ? 6000 : 2600, actions = [] } = {}) {
  let gone = false;
  const dismiss = () => {
    if (gone) return;
    gone = true;
    node.classList.add("is-leaving");
    node.addEventListener("animationend", () => node.remove(), { once: true });
  };
  const buttons = actions.map(({ label, run }) =>
    h("button.btn.btn--ghost.btn--xs.toast__action", {
      type: "button",
      text: label,
      on: { click: (event) => { event?.stopPropagation?.(); dismiss(); run(); } },
    }));
  const node = h(
    "div.toast",
    { dataset: { tone } },
    icon(TONE_ICON[tone] ?? ICONS.check, { size: 15 }),
    h("span.toast__text", { text: message }),
    buttons.length ? h("span.toast__actions", {}, ...buttons) : null,
  );
  ensureHost().append(node);
  node.addEventListener("click", dismiss);
  if (timeout > 0) setTimeout(dismiss, timeout);
  return dismiss;
}

/** Copy text and report the outcome, wherever the clipboard API is unavailable too. */
export async function copyText(text, label = "Copied") {
  try {
    await navigator.clipboard.writeText(text);
    toast(label, { tone: "success" });
    return true;
  } catch {
    toast("Clipboard access was blocked by the browser", { tone: "error" });
    return false;
  }
}
