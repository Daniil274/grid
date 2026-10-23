/**
 * The persistent notice on a reference-only chat.
 *
 * The new chat is empty on purpose: its old history was not imported, replayed
 * or restored. The notice names the source and links to it in a new tab, so the
 * owner can read the old thread and copy the parts they want into a NEW message
 * here. The link is reference data, never executable state - the banner says so.
 */

import { h, replace } from "../lib/dom.js";
import { REFERENCE_SOURCE_PATH, provenanceLabelOf, sourceLabelOf } from "./migration-dialog.js";

/** The conversation row for *id*, following a branch to its family row. */
export function conversationFor(conversations = [], id) {
  return conversations.find((item) => item.id === id || item.open_id === id) ?? null;
}

/** The source's title from the rail's rows, or "" when it is not listed. */
export function sourceTitleOf(migration, conversations = []) {
  const sourceId = migration?.source_context_id;
  if (!sourceId) return "";
  return conversationFor(conversations, sourceId)?.title ?? "";
}

/**
 * Draw the notice from the target's migration record; hide it for every other chat.
 *
 * @param {object} options
 * @param {HTMLElement} options.container the banner slot in index.html
 * @param {object} options.migration the target's `metadata.migration` record
 * @param {object[]} [options.conversations] the rail's rows, for the source title
 */
export function renderReferenceBanner({ container, migration, conversations = [] }) {
  const active = migration?.action === "context.reference_bootstrap" && migration?.executable_state === "none";
  container.hidden = !active;
  if (!active) {
    container.replaceChildren();
    return null;
  }
  const sourceId = migration.source_context_id;
  const title = sourceTitleOf(migration, conversations) || sourceLabelOf({ context_id: sourceId }, "");
  const provenance = provenanceLabelOf({ provenance: migration.source_provenance });
  const historyCount = Number(migration.reference?.history?.messages ?? 0);
  const link = h("a.referenceBanner__link", {
    href: REFERENCE_SOURCE_PATH(sourceId),
    target: "_blank",
    rel: "noopener",
    title: "Open the old chat in a new tab and copy what you need",
    text: `${title} ↗`,
  });
  replace(
    container,
    h("div.referenceBanner__body", {},
      h("div.referenceBanner__head", {},
        h("strong", { text: "Reference-only chat" }),
        h("span.referenceBanner__badge", { text: `source provenance: ${provenance}` }),
      ),
      h("p.referenceBanner__text", {},
        "The old history stays in ",
        link,
        ` (${historyCount} message(s), referenced only). It is not imported, not replayed and no workspace files are restored; old instructions and tools are not carried over.`,
      ),
      h("p.referenceBanner__hint", {
        text:
          "Open the old chat, select and copy the text you want, then paste it into a new message here. "
          + "It becomes reference material in your request, not executable state.",
      }),
    ),
  );
  return container;
}
