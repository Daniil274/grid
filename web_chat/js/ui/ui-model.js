/**
 * The pure half of a presented interface (web_chat/ui_document.py).
 *
 * Sorting, selection and the small bookkeeping of a block's values happen on
 * the page, without a model call. Keeping them here - away from the DOM - means
 * they can be read and tested on their own: the widget in `ui-block.js` only
 * paints what these functions decide.
 *
 * Nothing in a block is executable. The server checked every node against a
 * closed catalog; this module still treats a shape it does not recognise as
 * something to skip, never as something to run.
 */

/** Components this build knows how to draw, and nothing else. */
export const COMPONENTS = new Set(["stack", "text", "metric", "table", "select", "button"]);

/** `"price:asc"` -> `{column: "price", direction: "asc"}`, or null. */
export function parseSort(value) {
  if (typeof value !== "string") return null;
  const found = /^([A-Za-z][A-Za-z0-9_-]{0,39}):(asc|desc)$/.exec(value);
  return found ? { column: found[1], direction: found[2] } : null;
}

/** A cell as the sort reads it: numbers as numbers, everything else as text. */
function comparable(value) {
  if (typeof value === "number" && Number.isFinite(value)) return { numeric: true, value };
  if (typeof value === "boolean") return { numeric: true, value: value ? 1 : 0 };
  return { numeric: false, value: value == null ? "" : String(value) };
}

/**
 * Rows of a table in the order *sortValue* asks for.
 *
 * A copy is returned, never the stored list: local sorting is a view of the
 * data, not a change to it. Rows that compare equal keep their order, so a
 * reader who switches between columns does not see the table shuffle.
 */
export function sortRows(rows, sortValue) {
  const list = Array.isArray(rows) ? rows.slice() : [];
  const sort = parseSort(sortValue);
  if (!sort) return list;
  const direction = sort.direction === "desc" ? -1 : 1;
  const keyed = list.map((row, index) => ({ row, index, cell: comparable(row?.[sort.column]) }));
  keyed.sort((left, right) => {
    const a = left.cell;
    const b = right.cell;
    let order;
    if (a.numeric && b.numeric) order = a.value - b.value;
    else if (a.numeric !== b.numeric) order = a.numeric ? -1 : 1; // numbers before text, always
    else order = a.value < b.value ? -1 : a.value > b.value ? 1 : 0;
    return order !== 0 ? order * direction : left.index - right.index;
  });
  return keyed.map((entry) => entry.row);
}

/** The sort a click on *column* asks for: the other direction of this one. */
export function nextSortValue(current, column) {
  const sort = parseSort(current);
  if (!sort || sort.column !== column) return `${column}:asc`;
  return `${column}:${sort.direction === "asc" ? "desc" : "asc"}`;
}

/** A copy of *values* with one key replaced - blocks never mutate in place. */
export function setValue(values, key, value) {
  return { ...(values ?? {}), [key]: value };
}

/** The table node called *id*, or null. */
export function findNode(node, id) {
  if (!node || typeof node !== "object") return null;
  if (node.id === id) return node;
  for (const child of node.children ?? []) {
    const found = findNode(child, id);
    if (found) return found;
  }
  return null;
}

/** Every node of a tree, in document order. */
export function nodes(root) {
  const found = [];
  const visit = (node) => {
    if (!node || typeof node !== "object" || !COMPONENTS.has(node.type)) return;
    found.push(node);
    for (const child of node.children ?? []) visit(child);
  };
  visit(root);
  return found;
}

/**
 * What a block holds right now: its values and their revision.
 *
 * A stored block always has both; a block that arrived malformed is given an
 * empty, valid-enough state so the widget can still show its text fallback
 * rather than throwing inside a message.
 */
export function blockState(block) {
  const values = block && typeof block.values === "object" && block.values ? block.values : {};
  const revision = Number.isInteger(block?.state_revision) && block.state_revision >= 0 ? block.state_revision : 0;
  return { values: { ...values }, revision };
}

/**
 * A request id for one save or submit.
 *
 * The server treats a repeated id as the same request, so the id must be
 * unique per action - two quick saves are two requests, a retry of one is one.
 */
export function requestId(prefix = "ui") {
  const random = globalThis.crypto?.randomUUID?.().replace(/-/g, "").slice(0, 20)
    ?? Math.random().toString(36).slice(2, 12);
  return `${prefix}-${Date.now().toString(36)}-${random}`.slice(0, 64);
}

/** The plain-text answer a client without UI would show. */
export function fallbackText(block) {
  const text = block?.document?.fallback_markdown;
  return typeof text === "string" ? text : "";
}
