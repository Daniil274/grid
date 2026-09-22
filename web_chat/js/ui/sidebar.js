/**
 * Conversation rail: search, date grouping, and the active-chat indicator.
 *
 * The list re-renders from the store on every change. That is affordable here
 * (tens of rows, not thousands) and removes a whole class of bugs where the
 * rail drifts out of sync with the conversation actually on screen.
 */

import { h, replace } from "../lib/dom.js";
import { dayBucket, plural, shortStamp } from "../lib/format.js";

const BUCKET_ORDER = ["Today", "Yesterday", "This week", "This month", "Earlier"];

function matches(conversation, query) {
  if (!query) return true;
  const haystack = `${conversation.title ?? ""} ${conversation.routed_agent ?? ""}`.toLowerCase();
  return haystack.includes(query);
}

function groupByDay(conversations) {
  const groups = new Map(BUCKET_ORDER.map((name) => [name, []]));
  for (const conversation of conversations) groups.get(dayBucket(conversation.updated_at)).push(conversation);
  return [...groups].filter(([, items]) => items.length);
}

export function createConversationList({ container, searchInput, store, onSelect }) {
  let query = "";

  const row = (conversation, activeId) =>
    h(
      "button.chatRow",
      {
        type: "button",
        class: conversation.id === activeId ? "is-active" : "",
        on: { click: () => onSelect(conversation.id) },
      },
      h("span.chatRow__title", { text: conversation.title || "New chat" }),
      h(
        "span.chatRow__meta",
        {},
        h("span", { text: conversation.routed_agent || conversation.agent_key || "auto" }),
        h("span", { text: "·" }),
        h("span", { text: plural(conversation.message_count || 0, "msg") }),
        h("span.chatRow__stamp", { text: shortStamp(conversation.updated_at) }),
      ),
    );

  const render = ({ conversations, contextId }) => {
    const visible = conversations.filter((conversation) => matches(conversation, query));
    if (!visible.length) {
      replace(container, h("p.rail__empty", { text: query ? "Nothing matches that search." : "No conversations yet." }));
      return;
    }
    replace(
      container,
      groupByDay(visible).map(([label, items]) =>
        h("div.chatGroup", {}, h("div.chatGroup__label", { text: label }), ...items.map((item) => row(item, contextId))),
      ),
    );
  };

  searchInput.addEventListener("input", () => {
    query = searchInput.value.trim().toLowerCase();
    render(store.get());
  });

  store.subscribe(render);
}
