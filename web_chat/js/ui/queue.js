/**
 * Messages waiting for the agent: sent while it worked, delivered at its next
 * step or after its turn (web_chat/delivery.py). The list mirrors the server's
 * queue (`store.queue`); each row can be sent right away or dropped.
 */

import { h } from "../lib/dom.js";

const LABEL = {
  now: "Sending now",
  next_step: "At the agent's next step",
  after_turn: "After this turn",
};

export function createQueueTray({ container, store, onSendNow, onDrop }) {
  const render = ({ queue = [] }) => {
    container.hidden = queue.length === 0;
    container.replaceChildren(
      ...queue.map((item) =>
        h(
          "div.queueItem",
          { dataset: { state: item.state } },
          h("span.queueItem__badge", { text: item.state === "steering" ? "Waiting for the next step" : LABEL[item.delivery] ?? "Waiting" }),
          h("span.queueItem__text", { text: item.text || (item.images?.length ? `${item.images.length} image(s)` : "") }),
          h("button.btn.btn--ghost.btn--xs", { type: "button", title: "Send it now", on: { click: () => onSendNow(item) } }, "Send now"),
          h("button.btn.btn--ghost.btn--xs", { type: "button", title: "Drop this message", on: { click: () => onDrop(item) } }, "Cancel"),
        ),
      ),
    );
  };
  store.subscribe(render);
  render(store.get());
}
