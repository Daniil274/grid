/**
 * The master-detail editor shared by agents, tools and prompts.
 *
 * All three are "a record keyed by name, pick one and edit it", so they share
 * one implementation and differ only in how a row is described and what the
 * detail pane contains.
 */

import { h, icon, replace } from "../lib/dom.js";
import { ICONS } from "../ui/icons.js";

export class CollectionEditor {
  /**
   * @param {object} options
   * @param {HTMLElement} options.list row container
   * @param {HTMLElement} options.editor detail container
   * @param {HTMLButtonElement} options.addButton
   * @param {string} options.noun singular name, used in prompts and empty states
   * @param {() => Record<string, any>} options.records live record map from the config
   * @param {(key: string, value: any) => {title: string, meta: string}} options.describe
   * @param {(key: string, value: any) => Node} options.renderDetail
   * @param {() => any} options.blank a new, empty record
   * @param {(key: string) => void} [options.onDelete] extra cleanup (e.g. drop references)
   */
  constructor({ list, editor, addButton, noun, records, describe, renderDetail, blank, onDelete }) {
    Object.assign(this, { list, editor, addButton, noun, records, describe, renderDetail, blank, onDelete });
    this.selected = null;
    addButton.addEventListener("click", () => this._add());
  }

  render() {
    const entries = Object.entries(this.records() ?? {});
    if (!entries.some(([key]) => key === this.selected)) this.selected = entries[0]?.[0] ?? null;

    replace(
      this.list,
      entries.length
        ? entries.map(([key, value]) => this._row(key, value))
        : h("p.rail__empty", { text: `No ${this.noun}s defined yet.` }),
    );
    this._renderDetail();
  }

  select(key) {
    this.selected = key;
    this.render();
  }

  _row(key, value) {
    const { title, meta } = this.describe(key, value);
    return h(
      "button.recordRow",
      { type: "button", class: key === this.selected ? "is-active" : "", on: { click: () => this.select(key) } },
      h("span.recordRow__title", { text: title }),
      h("span.recordRow__meta", { text: meta }),
    );
  }

  _renderDetail() {
    const value = this.selected == null ? null : this.records()[this.selected];
    if (value == null) {
      replace(this.editor, h("p.pane__empty", { text: `Select a ${this.noun}, or create one.` }));
      return;
    }
    const { title } = this.describe(this.selected, value);
    replace(
      this.editor,
      h(
        "header.pane__head",
        {},
        h("div", {}, h("h3", { text: title }), h("p.pane__sub", { text: `Key: ${this.selected}` })),
        h(
          "button.btn.btn--danger",
          { type: "button", on: { click: () => this._delete() } },
          icon(ICONS.close, { size: 14 }),
          "Delete",
        ),
      ),
      this.renderDetail(this.selected, value),
    );
  }

  _add() {
    const key = window.prompt(`New ${this.noun} key`, `new_${this.noun}`)?.trim();
    if (!key) return;
    const records = this.records();
    if (records[key]) return;
    records[key] = this.blank();
    this.select(key);
  }

  _delete() {
    if (!window.confirm(`Delete ${this.noun} "${this.selected}"?`)) return;
    delete this.records()[this.selected];
    this.onDelete?.(this.selected);
    this.selected = null;
    this.render();
  }
}
