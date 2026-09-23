/**
 * Conversation rail: search, date grouping, the open-chat highlight, a spinner
 * on chats whose agent is still working, and rename / delete per row.
 *
 * The list re-renders from the store on every change. That is affordable here
 * (tens of rows, not thousands) and removes a whole class of bugs where the
 * rail drifts out of sync with the conversation actually on screen. The one
 * exception is a row being renamed: re-rendering would steal the input's focus.
 */

import { h, icon, replace } from "../lib/dom.js";
import { dayBucket, plural, shortStamp } from "../lib/format.js";
import { ICONS } from "./icons.js";

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

export function createConversationList({ container, searchInput, store, onSelect, onRename, onDelete }) {
  let query = "";
  let renaming = null; // id of the row whose title is an input right now

  const actionButton = (title, glyph, handler) =>
    h("button.iconBtn.iconBtn--xs", {
      type: "button",
      title,
      "aria-label": title,
      on: { click: (event) => { event.stopPropagation(); handler(); } },
    }, icon(glyph, { size: 13 }));

  const startRename = (conversation, titleNode) => {
    renaming = conversation.id;
    const original = conversation.title || "New chat";
    const input = h("input.chatRow__rename", { type: "text", value: original, maxlength: 120, "aria-label": "Chat title" });
    let done = false;
    const finish = async (save) => {
      if (done) return;
      done = true;
      renaming = null;
      const title = input.value.trim();
      if (save && title && title !== original) await onRename(conversation.id, title);
      render(store.get());
    };
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") void finish(true);
      else if (event.key === "Escape") void finish(false);
    });
    input.addEventListener("blur", () => void finish(true));
    input.addEventListener("click", (event) => event.stopPropagation());
    titleNode.replaceWith(input);
    input.focus();
    input.select();
  };

  const row = (conversation, activeId) => {
    const title = h("span.chatRow__title", { text: conversation.title || "New chat" });
    const main = h(
      "button.chatRow__main",
      { type: "button", on: { click: () => onSelect(conversation.id) } },
      h(
        "span.chatRow__head",
        {},
        conversation.active ? h("span.chatRow__spinner", { title: "Agent is working", "aria-label": "Agent is working" }) : null,
        title,
      ),
      h(
        "span.chatRow__meta",
        {},
        h("span", { text: conversation.routed_agent || conversation.agent_key || "auto" }),
        h("span", { text: "·" }),
        h("span", { text: plural(conversation.message_count || 0, "msg") }),
        h("span.chatRow__stamp", { text: shortStamp(conversation.updated_at) }),
      ),
    );
    const actions = h(
      "span.chatRow__actions",
      {},
      actionButton("Rename chat", ICONS.edit, () => startRename(conversation, title)),
      actionButton("Delete chat", ICONS.trash, () => {
        if (confirm(`Delete "${conversation.title || "New chat"}"? This cannot be undone.`)) onDelete(conversation.id);
      }),
    );
    const classes = [conversation.id === activeId ? "is-active" : "", conversation.active ? "is-running" : ""];
    return h("div.chatRow", { class: classes.join(" ") }, main, actions);
  };

  const render = ({ conversations, contextId }) => {
    if (renaming) return;
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
