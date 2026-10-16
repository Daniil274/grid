/**
 * Tell the user when an agent waits for their Allow/Decline.
 *
 * A call the action policy holds (core/action_policy.py) shows its buttons in a
 * step of the agents' work - often deep inside a sub-agent's block, in a chat
 * that may not be the one on screen, in a tab that may be in the background.
 * Nothing waits forever: an unanswered request expires and the call is refused.
 *
 * The conversation list (GET /api/chat/conversations, polled while any agent
 * works) names the calls each chat holds (`awaiting_approval`). Each new one:
 * - an in-page toast that stays until it is answered, with "Show" (this chat)
 *   or "Open chat" (another one) - and, once, an offer to turn on system
 *   notifications, which the browser grants only on a click;
 * - a system notification when the page is not what the user is looking at:
 *   the tab is hidden or unfocused, or the request is in another chat;
 * - the tab title counts the waiting requests.
 * An answered or expired request takes its toast and notification away.
 */

import { toast as showToast } from "./toast.js";

/** The request's row in the open chat: the reasoning panel opens it on its own. */
function revealRequest(doc) {
  const row = doc.querySelector?.(".step__approval:not([hidden])");
  row?.scrollIntoView?.({ block: "center", behavior: "smooth" });
  row?.querySelector?.("button")?.focus?.({ preventScroll: true });
}

/**
 * @param {{
 *   store: {get: () => any, watch: (keys: string[], listener: (state: any) => void) => () => void},
 *   onOpen: (contextId: string) => void,
 *   doc?: Document,
 *   Notifier?: typeof Notification,
 *   say?: typeof showToast,
 *   focusWindow?: () => void,
 * }} options
 * @returns {() => void} stop
 */
export function createApprovalAlerts({
  store,
  onOpen,
  doc = globalThis.document,
  Notifier = globalThis.Notification,
  say = showToast,
  focusWindow = () => globalThis.focus?.(),
}) {
  const baseTitle = doc.title;
  /** approval_id -> what to take away once it is answered */
  const shown = new Map();
  let offered = false;

  const looking = () => doc.visibilityState !== "hidden" && (doc.hasFocus?.() ?? true);

  const waiting = (conversations) =>
    (conversations ?? []).flatMap((chat) =>
      (chat.awaiting_approval ?? []).map((request) => ({
        ...request,
        chat: chat.open_id ?? chat.id,
        ids: [chat.id, chat.open_id].filter(Boolean),
        title: chat.title || "New chat",
      })));

  const notifySystem = (request) => {
    if (Notifier?.permission !== "granted") return null;
    try {
      const notice = new Notifier("An agent waits for your approval", {
        body: `${request.title}: ${request.tool}`,
        tag: `approval-${request.approval_id}`,
        requireInteraction: true,
      });
      notice.onclick = () => {
        focusWindow();
        onOpen(request.chat);
        notice.close();
      };
      return notice;
    } catch {
      return null; // a browser that only notifies from a service worker
    }
  };

  const announce = (request) => {
    const here = request.ids.includes(store.get().contextId);
    const actions = [
      here
        ? { label: "Show", run: () => revealRequest(doc) }
        : { label: "Open chat", run: () => onOpen(request.chat) },
    ];
    if (Notifier?.permission === "default" && !offered) {
      offered = true;
      actions.push({ label: "Notify me", run: () => void Notifier.requestPermission?.() });
    }
    const dismiss = say(
      here
        ? `The agent waits for your approval: ${request.tool}`
        : `“${request.title}” waits for your approval: ${request.tool}`,
      { tone: "attention", timeout: 0, actions },
    );
    const notice = here && looking() ? null : notifySystem(request);
    shown.set(request.approval_id, () => {
      dismiss?.();
      notice?.close?.();
    });
  };

  const update = ({ conversations }) => {
    const requests = waiting(conversations);
    const live = new Set(requests.map((request) => request.approval_id));
    for (const request of requests) if (!shown.has(request.approval_id)) announce(request);
    for (const [id, takeAway] of shown) {
      if (!live.has(id)) {
        takeAway();
        shown.delete(id);
      }
    }
    doc.title = requests.length ? `(${requests.length}) Approval needed · ${baseTitle}` : baseTitle;
  };

  const stop = store.watch(["conversations"], update);
  return () => {
    stop();
    for (const takeAway of shown.values()) takeAway();
    shown.clear();
    doc.title = baseTitle;
  };
}
