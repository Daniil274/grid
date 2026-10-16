/**
 * Focus containment for the drawers, and the runtime status live region.
 *
 * #admin, #agents, #settings, #plan and #password are plain <div>s that open by
 * adding a class, so the browser gives them none of a native <dialog>'s
 * guarantees. These two helpers add them without touching the markup: the
 * drawer becomes aria-modal, focus moves into its panel on open, Tab is kept
 * inside it, the rest of the page is made inert, and focus returns to whatever
 * opened the drawer once it closes. They live apart from toast.js so a view that
 * only needs to build nodes (the conversation rail, a chat) never runs DOM
 * lookups at import time.
 *
 * A WeakMap of per-drawer state means only the nodes this module made inert are
 * released again, so concurrent open/close cannot leak.
 */

import { $$ } from "../lib/dom.js";

const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[contenteditable]:not([contenteditable='false'])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

const overlayState = new WeakMap();

const panelOf = (drawer) => drawer.querySelector(".drawer__panel") ?? drawer;

/** Focusable, visible descendants of an element, in document order. */
function focusableWithin(scope) {
  return $$(FOCUSABLE, scope).filter(
    (node) => node.getClientRects().length > 0 && node.getAttribute("aria-hidden") !== "true",
  );
}

/**
 * Make a drawer the only thing the keyboard and a screen reader can reach.
 * Idempotent: a second call for an already-open drawer does nothing.
 *
 * @param {HTMLElement} drawer the open overlay (role="dialog")
 */
export function showOverlay(drawer) {
  if (overlayState.has(drawer)) return;
  const panel = panelOf(drawer);
  // The tests mount a drawer in a minimal DOM with no attribute or layout APIs;
  // there is no tab order to trap there, so leave it alone.
  if (typeof panel.hasAttribute !== "function") return;

  // Duck-typed rather than `instanceof HTMLElement`: the DOM here may be the
  // tests' minimal one, where that global class does not exist.
  const active = document.activeElement;
  const previous = active && typeof active.focus === "function" ? active : null;

  drawer.setAttribute("aria-modal", "true");

  // Everything behind the drawer leaves the tab order and the a11y tree.
  const inerted = [];
  for (const node of document.body.children ?? []) {
    if (node === drawer || node.tagName === "SCRIPT" || node.inert || node.classList.contains("toasts")) continue;
    node.inert = true;
    inerted.push(node);
  }

  // Focus the panel itself so a screen reader announces the dialog, unless the
  // drawer's own open() already moved focus to a field inside it.
  const hadTabindex = panel.hasAttribute("tabindex");
  if (!panel.contains(document.activeElement)) {
    if (!hadTabindex) panel.setAttribute("tabindex", "-1");
    panel.focus();
  }

  // `inert` alone keeps Tab inside; a fallback wrap covers browsers without it.
  const onKeydown = (event) => {
    if (event.key !== "Tab") return;
    const nodes = focusableWithin(panel);
    if (!nodes.length) {
      event.preventDefault();
      panel.focus();
      return;
    }
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || !panel.contains(active))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || !panel.contains(active))) {
      event.preventDefault();
      first.focus();
    }
  };
  drawer.addEventListener("keydown", onKeydown);

  overlayState.set(drawer, { previous, inerted, panel, hadTabindex, onKeydown });
}

/**
 * Undo showOverlay: release the page, drop aria-modal, restore focus.
 *
 * @param {HTMLElement} drawer the drawer that is closing
 */
export function hideOverlay(drawer) {
  const saved = overlayState.get(drawer);
  if (!saved) return;
  overlayState.delete(drawer);

  drawer.removeEventListener("keydown", saved.onKeydown);
  drawer.removeAttribute("aria-modal");
  if (!saved.hadTabindex) saved.panel.removeAttribute("tabindex");
  for (const node of saved.inerted) node.inert = false;

  const back = saved.previous;
  if (back && back.isConnected && !back.inert) back.focus();
}

// -- the runtime status line -------------------------------------------------
//
// #runtime-status is written by the entry point (js/main.js): turn it into a
// polite live region so a screen reader announces it, without a token-by-token
// narration (the answer itself streams elsewhere). Optional chaining keeps this
// a no-op wherever document exists without the DOM lookup (tests' fake DOM).
if (typeof document !== "undefined" && typeof document.getElementById === "function") {
  const runtimeStatus = document.getElementById("runtime-status");
  if (runtimeStatus) {
    runtimeStatus.setAttribute("role", "status");
    runtimeStatus.setAttribute("aria-live", "polite");
  }
}
