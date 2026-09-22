/** Transient notifications. Errors used to reach only the console; now they surface. */

import { h, icon } from "../lib/dom.js";
import { ICONS } from "./icons.js";

const TONE_ICON = { info: ICONS.check, success: ICONS.check, error: ICONS.error };

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
 * @param {{tone?: "info"|"success"|"error", timeout?: number}} [options]
 */
export function toast(message, { tone = "info", timeout = tone === "error" ? 6000 : 2600 } = {}) {
  const node = h("div.toast", { dataset: { tone } }, icon(TONE_ICON[tone] ?? ICONS.check, { size: 15 }), h("span", { text: message }));
  ensureHost().append(node);
  const dismiss = () => {
    node.classList.add("is-leaving");
    node.addEventListener("animationend", () => node.remove(), { once: true });
  };
  node.addEventListener("click", dismiss);
  setTimeout(dismiss, timeout);
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
