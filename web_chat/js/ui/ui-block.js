/**
 * Inline renderer for a presented interface (web_chat/ui_document.py).
 *
 * The server checks a declarative document and marks it as data; this widget
 * draws only the catalog below - stack, text, metric, table, select, button -
 * as real DOM, built without innerHTML. A component this build does not know
 * is skipped, and a block whose document is missing shows its text fallback:
 * an old page never has to guess what a newer answer meant.
 *
 * Sorting and selection are local (see `ui-model.js`); they never call the
 * model. The chosen values are saved in the background to the message that
 * presented the block, and a button sends them as the user's next ordinary
 * turn. While a save is in flight the status line says so; when the server
 * refuses one, the line says that too - a choice is never shown as saved
 * before the server confirmed it.
 *
 * An answer that arrives late is merged, never pasted: the revision it brings
 * is adopted, but a key the person changed while that request was on the wire
 * keeps the newer local choice (see `mergeAnswer`), so a slow save of an old
 * value cannot erase input made after it.
 */

import { h, replace } from "../lib/dom.js";
import {
  COMPONENTS, blockState, fallbackText, nextSortValue, parseSort, requestId, setValue, sortRows,
} from "./ui-model.js";

const SAVE_DELAY_MS = 400;
const TEXT = {
  saving: "Saving…",
  saved: "Saved",
  unsaved: "Not saved yet",
  stale: "Changed elsewhere — showing the latest values",
  failed: "Could not save — change something to retry",
  localOnly: "Saved with the answer; changes are kept once the turn is stored",
  offline: "Offline — this choice is not saved yet",
};

/** Format one cell for display; a missing cell is empty, never "undefined". */
const cellText = (value) => (value == null ? "" : String(value));

/**
 * Draw one block.
 *
 * @param {{id: string, revision: number, document: object, values: object, state_revision: number}} block
 * @param {{
 *   getMessageId?: () => ?string,
 *   onSave?: (payload: object) => Promise<object>,
 *   onSubmit?: (payload: object) => Promise<boolean|undefined>,
 * }} [options]
 * @returns {{el: HTMLElement, refresh: Function, getValues: Function}}
 */
export function renderUiBlock(block, options = {}) {
  const document_ = block?.document;
  const state = blockState(block);
  const container = h("section.uiBlock", {
    dataset: { uiId: String(block?.id ?? ""), revision: String(block?.revision ?? "") },
    "aria-label": typeof document_?.title === "string" ? document_.title : "Interface",
  });

  const body = h("div.uiBlock__body");
  const status = h("p.uiBlock__status", { role: "status", hidden: true });
  container.append(body, status);

  if (!document_ || !COMPONENTS.has(document_?.root?.type)) {
    // A block without a usable document is still an answer: show its text.
    const text = fallbackText(block);
    container.classList.add("is-fallback");
    body.append(h("pre.uiBlock__fallback", { text: text || "This interface cannot be shown here." }));
    return { el: container, refresh() {}, getValues: () => ({ ...state.values }) };
  }

  const submitButtons = [];
  let repaint = () => {};
  let saveTimer = null;
  let dirty = false;
  let saveChain = Promise.resolve();
  let statusTimer = null;
  let submitted = false;
  // Bumped on every local change: a save (or a press) records the epoch it
  // left with, so a late answer can tell "nothing happened meanwhile" from
  // "the person has chosen something newer".
  let localEpoch = 0;
  let pressMark = null;

  /**
   * Fold a server answer into the values of this page.
   *
   * The answer is the truth for every key this page has not touched since the
   * request left; a key changed in the meantime keeps the newer local choice,
   * so a slow save of an old value can never erase what was typed after it.
   */
  const mergeAnswer = (sent, answer, sentEpoch) => {
    const merged = { ...answer };
    if (localEpoch === sentEpoch) return merged;
    for (const [key, value] of Object.entries(state.values)) {
      if (sent[key] !== value) merged[key] = value;
    }
    for (const key of Object.keys(sent)) {
      if (!(key in state.values) && key in merged) delete merged[key];
    }
    return merged;
  };

  const setStatus = (text, tone = "muted", { autoHide = false } = {}) => {
    clearTimeout(statusTimer);
    status.textContent = text ?? "";
    status.hidden = !text;
    status.dataset.tone = tone;
    if (autoHide) statusTimer = setTimeout(() => { status.hidden = true; }, 2200);
  };

  const messageId = () => {
    const found = options.getMessageId?.();
    return typeof found === "string" && found ? found : null;
  };

  /** Reflect whether the block can talk to the server at all right now. */
  const refresh = () => {
    const ready = Boolean(messageId());
    for (const button of submitButtons) button.disabled = !ready || submitted;
    if (!ready) setStatus(TEXT.localOnly);
    else if (status.textContent === TEXT.localOnly) setStatus(null);
  };

  /** One save of the values as they are now, adopting the server's answer. */
  const saveNow = async (target) => {
    // What this request carries: the snapshot is also the baseline a late
    // answer is merged against.
    const values = { ...state.values };
    const revision = state.revision;
    const epoch = localEpoch;
    setStatus(TEXT.saving);
    try {
      const answer = await options.onSave({
        messageId: target,
        uiId: block.id,
        values,
        stateRevision: revision,
        requestId: requestId("save"),
      });
      if (Number.isInteger(answer?.state_revision)) state.revision = answer.state_revision;
      // The revision is always adopted (the stored state did move on), but the
      // values only where this page has not chosen something newer meanwhile.
      if (answer?.values && typeof answer.values === "object") {
        state.values = mergeAnswer(values, answer.values, epoch);
      }
      setStatus(TEXT.saved, "ok", { autoHide: true });
      repaint();
    } catch (error) {
      const payload = error?.payload ?? null;
      if (payload?.code === "ui.stale" && payload.values && Number.isInteger(payload.state_revision)) {
        // Another tab (or the submit) moved the state: show what the server
        // holds instead of pretending this tab's choice won - except for what
        // this person changed while the request was in flight, which is theirs.
        state.values = mergeAnswer(values, payload.values, epoch);
        state.revision = payload.state_revision;
        setStatus(TEXT.stale, "warn");
        repaint();
      } else {
        setStatus(error?.message ? `${TEXT.failed} (${error.message})` : TEXT.failed, "error");
      }
    }
  };

  /**
   * Save now, and every change made while a save was in flight.
   *
   * Saves are serialized on one chain, so two callers never send the same
   * revision twice. The returned promise resolves when the block is quiet -
   * which is what `submit` waits for before it sends a revision.
   */
  const flush = () => {
    const pending = saveTimer !== null || dirty;
    clearTimeout(saveTimer);
    saveTimer = null;
    const target = messageId();
    if (!target || !options.onSave) {
      // Nothing can be saved right now (the turn is not stored yet, or this
      // host has no save channel): the choice stays local. Dropping the flag
      // matters - a caller that waits for quiet (a press) must not spin on a
      // change nobody can ever save.
      dirty = false;
      return Promise.resolve();
    }
    // Nothing new to save: still wait for a save already in flight, but do not
    // send a redundant one (a quiet press must carry the current revision).
    if (!pending) return saveChain;
    const run = async () => {
      do {
        dirty = false;
        clearTimeout(saveTimer);
        saveTimer = null;
        await saveNow(target);
      } while (dirty || saveTimer !== null);
    };
    saveChain = saveChain.then(run, run);
    return saveChain;
  };

  function scheduleSave() {
    if (!messageId()) { refresh(); return; }
    dirty = true;
    setStatus(TEXT.unsaved);
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => { void flush(); }, SAVE_DELAY_MS);
  }

  const change = (key, value) => {
    state.values = setValue(state.values, key, value);
    localEpoch += 1;
    repaint();
    scheduleSave();
  };

  const submit = async (node) => {
    const target = messageId();
    if (!target || submitted || !options.onSubmit) { refresh(); return; }
    // A save may be in flight, or a debounce may still be pending: the server
    // checks the revision this press carries against the stored one, so the
    // press must land after every local change has been saved. The wait is
    // bounded: if a save can no longer happen, the press goes with what the
    // page holds instead of waiting for a quiet that never comes.
    for (let attempt = 0; attempt < 8; attempt += 1) {
      await flush();
      if (saveTimer === null && !dirty) break;
    }
    if (submitted) return; // another press got there while we waited
    submitted = true;
    refresh();
    setStatus("Sending…");
    // What the press carries, so a late acknowledgement is merged against it
    // rather than pasted over a change made while the press was in flight.
    pressMark = { values: { ...state.values }, epoch: localEpoch };
    try {
      const started = await options.onSubmit?.({
        messageId: target,
        uiId: block.id,
        actionId: node.action,
        documentRevision: block.revision,
        stateRevision: state.revision,
        values: { ...pressMark.values },
        requestId: requestId("submit"),
      });
      if (started === false) {
        submitted = false;
        refresh();
        setStatus("The conversation is busy; try again when this turn ends.", "warn");
      }
      // A started press keeps "Sending…" until the server's ui_submitted
      // acknowledgement (which also carries the new state revision); a refusal
      // arrives as ui_refused and resets the button through acknowledge().
    } catch (error) {
      submitted = false;
      pressMark = null;
      refresh();
      setStatus(error?.message ?? "Could not send this choice.", "error");
    }
  };

  /**
   * The server answered a press: adopt the revision it stored, and either mark
   * the press done or let the person try again.
   */
  const acknowledge = ({ stateRevision, values, sent, failed } = {}) => {
    let redraw = false;
    if (Number.isInteger(stateRevision) && stateRevision >= 0) state.revision = stateRevision;
    if (values && typeof values === "object") {
      state.values = pressMark ? mergeAnswer(pressMark.values, values, pressMark.epoch) : { ...values };
      redraw = true;
    }
    pressMark = null;
    if (redraw) repaint();
    if (failed) {
      submitted = false;
      refresh();
      setStatus("Not sent — change something and try again.", "warn");
    } else if (sent) {
      setStatus("Sent", "ok", { autoHide: true });
    }
  };

  // -- painting ----------------------------------------------------------
  function renderNode(node, parent) {
    if (!node || typeof node !== "object" || !COMPONENTS.has(node.type)) return;
    switch (node.type) {
      case "stack": {
        const stack = h(`div.uiStack.uiStack--${node.gap ?? "m"}`);
        if (node.title) stack.append(h("h4.uiStack__title", { text: node.title }));
        for (const child of node.children ?? []) renderNode(child, stack);
        parent.append(stack);
        return;
      }
      case "text":
        parent.append(h("p.uiText", { text: node.text }));
        return;
      case "metric": {
        const shown = cellText(node.value);
        parent.append(h(
          "div.uiMetric",
          {},
          h("span.uiMetric__label", { text: node.label }),
          h("span.uiMetric__value", {}, shown, node.unit ? h("span.uiMetric__unit", { text: ` ${node.unit}` }) : null),
          node.hint ? h("span.uiMetric__hint", { text: node.hint }) : null,
        ));
        return;
      }
      case "select":
        parent.append(renderSelect(node));
        return;
      case "table":
        parent.append(renderTable(node));
        return;
      case "button": {
        const button = h(
          `button.btn.uiButton${node.tone === "primary" ? ".btn--primary" : ".btn--ghost"}`,
          {
            type: "button",
            text: node.label,
            dataset: { uiNode: node.id },
            on: { click: () => void submit(node) },
          },
        );
        submitButtons.push(button);
        parent.append(button);
        return;
      }
      default:
        return;
    }
  }

  function renderSelect(node) {
    const options_ = Array.isArray(node.options) ? node.options : [];
    const select = h("label.uiField", {}, h("span.uiField__label", { text: node.label }));
    const control = h("select.uiSelect", { "aria-label": node.label, dataset: { uiNode: node.id } });
    for (const option of options_) {
      control.append(h("option", { value: option.value, text: option.label }));
    }
    const current = state.values[node.bind];
    if (typeof current === "string" && !options_.some((option) => option.value === current)) {
      // The value can be newer than this select's list (a table header click,
      // another tab, a stored submit): show it as it is, rather than display a
      // different option while the state says otherwise.
      const parsed = parseSort(current);
      const label = parsed ? `${parsed.column} ${parsed.direction === "asc" ? "↑" : "↓"} (current)` : `${current} (current)`;
      control.append(h("option", { value: current, text: label }));
    }
    control.value = typeof current === "string" ? current : (options_[0]?.value ?? "");
    control.addEventListener("change", () => change(node.bind, control.value));
    select.append(control);
    return select;
  }

  function renderTable(node) {
    const wrap = h("div.uiTable");
    if (node.title) wrap.append(h("div.uiTable__title", { text: node.title }));
    const table = h("table.uiTable__grid");
    const headRow = h("tr");
    const columns = Array.isArray(node.columns) ? node.columns : [];
    const sort = sortValueOf(node);
    for (const column of columns) {
      const sorted = sort?.column === column.key ? (sort.direction === "asc" ? " ↑" : " ↓") : "";
      const label = h("span", { text: `${column.label}${sorted}` });
      const cell = h(column.align === "end" ? "th.uiTable__head.is-end" : "th.uiTable__head", { scope: "col" });
      if (node.sort_state) {
        // The header is a control only when the document gave the table a
        // sort state: sorting stays local and is then saved like any choice.
        cell.append(h("button.uiTable__sort", {
          type: "button",
          "aria-label": `Sort by ${column.label}`,
          "aria-pressed": String(sort?.column === column.key),
          on: { click: () => change(node.sort_state, nextSortValue(state.values[node.sort_state], column.key)) },
        }, label));
      } else cell.append(label);
      headRow.append(cell);
    }
    table.append(h("thead", {}, headRow));

    const body_ = h("tbody");
    const rows = sortRows(node.rows, sort?.column ? `${sort.column}:${sort.direction}` : null);
    const chosen = node.select_state ? state.values[node.select_state] : null;
    for (const row of rows) {
      const selected = chosen != null && row.id === chosen;
      const tr = h("tr.uiTable__row", {
        dataset: { rowId: String(row.id), selected: String(selected) },
        class: selected ? "is-selected" : null,
      });
      if (node.select_state) {
        tr.setAttribute("tabindex", "0");
        tr.setAttribute("role", "button");
        tr.setAttribute("aria-pressed", String(selected));
        tr.addEventListener("click", () => change(node.select_state, row.id));
        tr.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            change(node.select_state, row.id);
          }
        });
      }
      const labelKey = node.row_label ?? columns[0]?.key;
      for (const column of columns) {
        const label = cellText(row[column.key]);
        const align = column.align === "end" ? ".is-end" : "";
        const cell = node.select_state && column.key === labelKey
          // The chosen row carries a mark in its label cell; the mark is part
          // of the cell from the start, so no DOM insert is needed.
          ? h(`td${align}`, {}, h("span.uiTable__check", { text: selected ? "✓" : "", "aria-hidden": "true" }), label)
          : h(`td${align}`, { text: label });
        tr.append(cell);
      }
      body_.append(tr);
    }
    table.append(body_);
    wrap.append(table);
    return wrap;
  }

  function sortValueOf(node) {
    if (node.sort_state) {
      const value = state.values[node.sort_state];
      const parsed = typeof value === "string" ? parseSort(value) : null;
      if (parsed) return parsed;
    }
    // A table may carry a fixed `sort` for its display order; a bound
    // sort_state takes precedence when it holds a usable value.
    const fixed = node.sort;
    if (fixed && typeof fixed.column === "string") {
      return { column: fixed.column, direction: fixed.direction === "desc" ? "desc" : "asc" };
    }
    return null;
  }

  /** Where the keyboard was before a repaint rebuilt the body. */
  function focusPlace() {
    const active = typeof document !== "undefined" ? document.activeElement : null;
    if (!active || !body.contains(active)) return null;
    return {
      node: active.dataset?.uiNode ?? null,
      row: active.dataset?.rowId ?? null,
      tag: active.tagName ?? null,
    };
  }

  function findPlace(place) {
    const walk = (node) => {
      for (const child of node.childNodes ?? []) {
        if (child.tagName && ((place.node && child.dataset?.uiNode === place.node)
          || (place.row && child.dataset?.rowId === place.row))) {
          if (!place.tag || child.tagName === place.tag) return child;
        }
        const nested = walk(child);
        if (nested) return nested;
      }
      return null;
    };
    return walk(body);
  }

  repaint = () => {
    const place = focusPlace();
    submitButtons.length = 0;
    replace(body, ...renderTree(document_.root));
    // A repaint builds fresh buttons: restore the gate they had before it.
    const ready = Boolean(messageId());
    for (const button of submitButtons) button.disabled = !ready || submitted;
    // ...and put the keyboard back where it was, so a select keeps focus.
    if (place) findPlace(place)?.focus?.();
  };
  repaint();
  refresh();

  // The stored id can arrive later (a live turn is saved at its end): ask
  // again when the message knows it, so buttons wake up without a reload.
  container._uiRefresh = refresh;

  const fallback = fallbackText(block);
  if (fallback) {
    container.append(h(
      "details.uiBlock__text",
      {},
      h("summary", { text: "Text version" }),
      h("pre.uiBlock__fallback", { text: fallback }),
    ));
  }

  return {
    el: container,
    refresh,
    acknowledge,
    getValues: () => ({ ...state.values }),
  };

  function renderTree(root) {
    const children = [];
    const holder = h("div");
    renderNode(root, holder);
    children.push(...holder.childNodes);
    return children;
  }
}
