/**
 * Form controls bound directly to a config object.
 *
 * Each control mutates `target[key]` on input. The settings drawer edits a
 * clone of the server's config and PUTs the whole document back, so two-way
 * binding is the honest model here - there is no intermediate form state to
 * keep in sync, and nothing is persisted until Save.
 */

import { h } from "../lib/dom.js";

/** Label + control + optional hint, the standard row. */
export function field(label, control, hint) {
  return h("label.field", {}, h("span.field__label", { text: label }), control, hint ? h("span.field__hint", { text: hint }) : null);
}

export function textInput(target, key, { placeholder = "", type = "text" } = {}) {
  const input = h("input.control", { type, placeholder, value: target[key] ?? "" });
  input.addEventListener("input", () => {
    target[key] = type === "number" ? Number(input.value) : input.value;
  });
  return input;
}

export function textArea(target, key, { rows = 6, placeholder = "", onInput } = {}) {
  const area = h("textarea.control.control--area", { rows, placeholder, spellcheck: "false" });
  area.value = target[key] ?? "";
  area.addEventListener("input", () => {
    target[key] = area.value;
    onInput?.(area.value);
  });
  return area;
}

/** @param {Array<{value: string, label: string}>} options */
export function select(target, key, options) {
  const node = h(
    "select.control",
    {},
    options.map((option) => h("option", { value: option.value, selected: option.value === target[key] }, option.label)),
  );
  node.addEventListener("change", () => {
    target[key] = node.value;
  });
  return node;
}

export function toggle(target, key, label) {
  const input = h("input", { type: "checkbox", checked: Boolean(target[key]) });
  input.addEventListener("change", () => {
    target[key] = input.checked;
  });
  return h("label.switch", {}, input, h("span.switch__track", {}, h("span.switch__thumb")), h("span", { text: label }));
}

/**
 * A multi-select rendered as a chip grid.
 * @param {string[]} selected current values (mutated in place is avoided; use onChange)
 */
export function chipPicker(selected, options, onChange) {
  const current = new Set(selected);
  const grid = h(
    "div.chipGrid",
    {},
    options.map((option) => {
      const input = h("input", { type: "checkbox", checked: current.has(option.value) });
      input.addEventListener("change", () => {
        if (input.checked) current.add(option.value);
        else current.delete(option.value);
        onChange([...current]);
      });
      return h("label.chipToggle", {}, input, h("span", { text: option.label }));
    }),
  );
  return grid;
}

/** `KEY=value` lines <-> an object, for env blocks. */
export function keyValueArea(target, key) {
  const area = h("textarea.control.control--area", { rows: 4, spellcheck: "false", placeholder: "KEY=value" });
  area.value = Object.entries(target[key] ?? {})
    .map(([name, value]) => `${name}=${value}`)
    .join("\n");
  area.addEventListener("input", () => {
    target[key] = Object.fromEntries(
      area.value
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean)
        .map((line) => {
          const [name, ...rest] = line.split("=");
          return [name, rest.join("=")];
        })
        .filter(([name]) => name),
    );
  });
  return area;
}

/** One argument per line <-> a string array, for server commands. */
export function lineListArea(target, key, { rows = 4, placeholder = "" } = {}) {
  const area = h("textarea.control.control--area", { rows, placeholder, spellcheck: "false" });
  area.value = (target[key] ?? []).join("\n");
  area.addEventListener("input", () => {
    target[key] = area.value
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
  });
  return area;
}

export const optionsFrom = (record, labelOf = (key, value) => value?.name || key) =>
  Object.entries(record ?? {}).map(([key, value]) => ({ value: key, label: labelOf(key, value) }));
