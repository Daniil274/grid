/**
 * The rail's shell around its list (web_chat/js/ui/sidebar.js): folding the
 * rail away on a wide screen, and the two menus of its bottom - "More" (the
 * pages and settings used less often) and the account's (accounts, password,
 * sign out).
 *
 * The menus hold the very buttons main.js wires by id (#open-settings,
 * #sign-out, …) and shows or hides by role, so moving a button between the dock
 * and a menu changes nothing in what it does. "More" hides itself when the
 * signed-in user has none of its entries.
 */

const FOLDED_KEY = "grid:rail:collapsed";
/** Wider than this, the rail sits beside the chat and can be folded away. */
const WIDE = "(min-width: 1081px)";

const read = () => {
  try {
    return globalThis.localStorage?.getItem(FOLDED_KEY) === "1";
  } catch {
    return false;
  }
};

const write = (folded) => {
  try {
    globalThis.localStorage?.setItem(FOLDED_KEY, folded ? "1" : "0");
  } catch {
    /* private mode: the choice lasts the visit */
  }
};

/** Whether the rail sits beside the chat (a wide screen) rather than over it. */
export const railBeside = () => globalThis.matchMedia?.(WIDE)?.matches ?? true;

/** Fold the rail away (wide screens), or bring it back. */
export function setRailFolded(folded) {
  document.body.classList.toggle("rail-collapsed", folded);
  write(folded);
}

/** Show the rail: back beside the chat on a wide screen, over it on a narrow one. */
export function revealRail() {
  if (railBeside()) setRailFolded(false);
  else document.body.classList.add("rail-open");
}

function popover(button, menu) {
  const close = () => {
    menu.hidden = true;
    button.setAttribute("aria-expanded", "false");
  };
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    const open = menu.hidden;
    for (const other of document.querySelectorAll(".railPopover")) other.hidden = true;
    menu.hidden = !open;
    button.setAttribute("aria-expanded", String(open));
    if (open) menu.querySelector("button:not([hidden]), a:not([hidden])")?.focus();
  });
  // An entry acts, then the menu goes; a click elsewhere or Escape closes it too.
  menu.addEventListener("click", (event) => {
    if (event.target.closest("button, a")) close();
  });
  document.addEventListener("click", (event) => {
    if (!menu.hidden && !menu.contains(event.target)) close();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !menu.hidden) {
      close();
      button.focus();
    }
  });
  return close;
}

/**
 * @param {{collapse: HTMLElement, more: HTMLElement, moreMenu: HTMLElement,
 *   account: HTMLElement, accountMenu: HTMLElement, accountName: HTMLElement,
 *   avatar: HTMLElement}} nodes
 */
export function createRailShell({ collapse, more, moreMenu, account, accountMenu, accountName, avatar }) {
  if (read()) document.body.classList.add("rail-collapsed");
  collapse?.addEventListener("click", () => setRailFolded(true));

  if (more && moreMenu) popover(more, moreMenu);
  if (account && accountMenu) popover(account, accountMenu);

  // "More" only when it holds something for this user; the avatar is the name's initial.
  const sync = () => {
    if (more && moreMenu) {
      more.hidden = ![...moreMenu.children].some((entry) => !entry.hidden);
    }
    if (avatar && accountName) {
      avatar.textContent = (accountName.textContent.trim()[0] ?? "?").toUpperCase();
    }
  };
  sync();
  if (typeof MutationObserver === "function") {
    const watcher = new MutationObserver(sync);
    if (moreMenu) watcher.observe(moreMenu, { attributes: true, subtree: true, attributeFilter: ["hidden"] });
    if (accountName) watcher.observe(accountName, { childList: true, characterData: true, subtree: true });
  }
  return { sync };
}
