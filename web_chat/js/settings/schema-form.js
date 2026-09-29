/**
 * Forms drawn from the config's JSON schema (schemas.GridConfig).
 *
 * Every setting the schema knows gets a control, so a field added on the
 * server is editable here without a line of UI code. Controls read and write
 * through a *slot* - `{get(), set(value)}` - rather than an object and a key:
 * a nested section is only created in the document when one of its fields is
 * actually set, and clearing a field removes the key, so the file keeps
 * relying on the default instead of spelling it out.
 *
 * `refs` turns a free-text field into a choice between keys defined elsewhere
 * in the same file (an agent's models, a model's provider). It is keyed by
 * path, with `*` for a record key: `"agents.*.model"`.
 */

import { h, icon } from "../lib/dom.js";
import { ICONS } from "../ui/icons.js";

/** Keys whose values are prose, drawn as text areas even when empty. */
const LONG_TEXT = new Set([
  "custom_prompt",
  "tools_common_rules",
  "prompt_addition",
  "description",
  "system_prompt",
  "instructions",
]);

/** Words a plain title-casing gets wrong. */
const WORDS = { mcp: "MCP", api: "API", url: "URL", id: "ID", ttl: "TTL", tts: "TTS", stt: "STT", cpus: "CPUs", pids: "PIDs", jpeg: "JPEG", mb: "MB" };

export const humanize = (key) =>
  String(key)
    .split(/[_-]+/)
    .filter(Boolean)
    .map((word, index) => WORDS[word.toLowerCase()] ?? (index ? word : word[0].toUpperCase() + word.slice(1)))
    .join(" ");

/** A slot for `key` inside another slot's object, created on first write. */
export function child(slot, key) {
  return {
    get: () => slot.get()?.[key],
    set: (value) => {
      const parent = slot.get();
      if (value === undefined) {
        if (parent && typeof parent === "object") delete parent[key];
        return;
      }
      if (parent && typeof parent === "object") parent[key] = value;
      else slot.set({ [key]: value });
    },
  };
}

export const rootSlot = (getObject) => ({ get: getObject, set: () => {} });

export class SchemaKit {
  /**
   * @param {object} root the whole JSON schema, for `$defs`
   * @param {Record<string, {kind: "select"|"multi"|"ordered", options: () => string[]}>} refs
   */
  constructor(root, refs = {}) {
    this.defs = root?.$defs ?? {};
    this.root = root ?? {};
    this.refs = refs;
  }

  /** `$ref` followed and `X | null` unwrapped, keeping the outer title, default and hint. */
  resolve(schema) {
    if (!schema) return {};
    let node = schema;
    const outer = { title: schema.title, description: schema.description, default: schema.default };
    let nullable = false;
    if (node.anyOf) {
      const variants = node.anyOf.filter((variant) => variant.type !== "null");
      nullable = variants.length < node.anyOf.length;
      if (variants.length !== 1) return { ...outer, nullable, kind: "json", variants };
      node = variants[0];
    }
    if (node.$ref) node = { ...this.defs[node.$ref.split("/").pop()], ...Object.fromEntries(Object.entries(node).filter(([key]) => key !== "$ref")) };
    if (node.allOf?.length === 1) node = { ...this.resolve(node.allOf[0]), ...node, allOf: undefined };
    const merged = { ...node, nullable };
    for (const [key, value] of Object.entries(outer)) if (value !== undefined) merged[key] = value;
    return merged;
  }

  def(name) {
    return this.defs[name] ?? {};
  }

  /** The schema of a top-level section of the file, resolved. */
  section(key) {
    return this.resolve(this.root.properties?.[key]);
  }

  // -- forms ---------------------------------------------------------------
  /**
   * Every property of an object schema: short fields in a grid, prose and
   * lists under it, nested sections last, each in its own fold.
   */
  objectForm(schema, slot, path, { skip = [], only = null } = {}) {
    const resolved = this.resolve(schema);
    const required = new Set(resolved.required ?? []);
    const grid = [];
    const wide = [];
    const groups = [];
    for (const [key, property] of Object.entries(resolved.properties ?? {})) {
      if (skip.includes(key) || (only && !only.includes(key))) continue;
      const node = this.resolve(property);
      const control = this.field(key, node, child(slot, key), [...path, key], required.has(key));
      if (!control) continue;
      if (control.dataset.shape === "group") groups.push(control);
      else if (control.dataset.shape === "wide") wide.push(control);
      else grid.push(control);
    }
    return h(
      "div.schemaForm",
      {},
      grid.length ? h("div.formGrid", {}, grid) : null,
      wide,
      groups,
    );
  }

  /** One property: its label, its control, its hint. */
  field(key, node, slot, path, required = false) {
    const label = humanize(key);
    const hint = node.description ?? "";
    const ref = this.refs[pathKey(path)];

    if (ref) return this._refField(label, hint, node, slot, ref, required);
    if (node.kind === "json") return wrap(label, hint, jsonArea(slot), "wide");

    switch (node.type) {
      case "boolean":
        return node.nullable ? wrap(label, hint, triState(slot, node.default)) : switchField(label, hint, slot, node.default);
      case "integer":
      case "number":
        // Unset, the default is the field's placeholder; set, the hint keeps it in view.
        return wrap(label, hint, numberInput(slot, node), "", slot.get() == null ? undefined : node.default);
      case "string":
        if (node.enum) return wrap(label, hint, enumSelect(slot, node.enum, node.default, required));
        if (LONG_TEXT.has(key) || String(slot.get() ?? "").includes("\n"))
          return wrap(label, hint, textArea(slot, node, required, key === "description" ? 3 : 7), "wide");
        return wrap(label, hint, textInput(slot, node, required));
      case "array":
        return this._arrayField(label, hint, node, slot);
      case "object":
        return this._objectField(key, label, hint, node, slot, path);
      default:
        return wrap(label, hint, jsonArea(slot), "wide");
    }
  }

  _arrayField(label, hint, node, slot) {
    const items = this.resolve(node.items);
    if (items.enum) return wrap(label, hint, chips(slot, () => items.enum), "wide");
    if (items.type === "string") return wrap(label, hint, lineList(slot), "wide");
    return wrap(label, hint, jsonArea(slot), "wide");
  }

  _objectField(key, label, hint, node, slot, path) {
    if (node.properties) {
      const body = this.objectForm(node, slot, path);
      return group(label, hint, body, slot);
    }
    const values = node.additionalProperties;
    if (values && typeof values === "object") {
      const item = this.resolve(values);
      if (item.type === "string") return wrap(label, hint, keyValue(slot), "wide");
      if (item.properties) return group(label, hint, this.mapForm(item, slot, [...path, "*"], humanize(key)), slot);
    }
    return wrap(label, hint, jsonArea(slot), "wide");
  }

  /** A record of objects nested in a section (the catalog's systems): a fold per entry. */
  mapForm(item, slot, path, noun) {
    const list = h("div.mapForm");
    const render = () => {
      const entries = Object.entries(slot.get() ?? {});
      list.replaceChildren(
        ...entries.map(([key]) => {
          const entrySlot = child(slot, key);
          const remove = h(
            "button.btn.btn--ghost.btn--small",
            {
              type: "button",
              on: {
                click: () => {
                  if (!window.confirm(`Remove "${key}"?`)) return;
                  entrySlot.set(undefined);
                  render();
                },
              },
            },
            icon(ICONS.close, { size: 12 }),
            "Remove",
          );
          return h(
            "div.mapEntry",
            {},
            h("div.mapEntry__head", {}, h("strong", { text: key }), remove),
            this.objectForm(item, entrySlot, path),
          );
        }),
        h(
          "button.btn.btn--ghost",
          {
            type: "button",
            on: {
              click: () => {
                const key = window.prompt(`New ${noun.toLowerCase()} key`)?.trim();
                if (!key || slot.get()?.[key]) return;
                child(slot, key).set(blankOf(item));
                render();
              },
            },
          },
          icon(ICONS.plus, { size: 14 }),
          "Add",
        ),
      );
    };
    render();
    return list;
  }

  _refField(label, hint, node, slot, ref, required) {
    const options = ref.options();
    if (ref.kind === "multi") return wrap(label, hint, chips(slot, () => options), "wide");
    if (ref.kind === "ordered") return wrap(label, hint, orderedPicker(slot, options), "wide");
    return wrap(label, hint, keySelect(slot, options, node.default, required));
  }
}

// -- controls ----------------------------------------------------------------

const pathKey = (path) => path.join(".");

function wrap(label, hint, control, shape = "", fallback) {
  const hintText = [hint, fallback !== undefined && fallback !== null && `Default: ${fallback}`].filter(Boolean).join(" · ");
  const node = h("label.field", {}, h("span.field__label", { text: label }), control, hintText ? h("span.field__hint", { text: hintText }) : null);
  if (shape) node.dataset.shape = shape;
  return node;
}

function switchField(label, hint, slot, fallback) {
  const input = h("input", { type: "checkbox", checked: Boolean(slot.get() ?? fallback) });
  input.addEventListener("change", () => slot.set(input.checked));
  return h(
    "div.field.field--switch",
    {},
    h("label.switch", {}, input, h("span.switch__track", {}, h("span.switch__thumb")), h("span", { text: label })),
    hint ? h("span.field__hint", { text: hint }) : null,
  );
}

function triState(slot, fallback) {
  const current = slot.get();
  const node = h(
    "select.control",
    {},
    h("option", { value: "", selected: current == null }, `Default${fallback != null ? ` (${fallback ? "on" : "off"})` : ""}`),
    h("option", { value: "true", selected: current === true }, "On"),
    h("option", { value: "false", selected: current === false }, "Off"),
  );
  node.addEventListener("change", () => slot.set(node.value === "" ? undefined : node.value === "true"));
  return node;
}

function numberInput(slot, node) {
  const input = h("input.control", {
    type: "number",
    value: slot.get() ?? "",
    placeholder: node.default ?? "",
    min: node.minimum ?? node.exclusiveMinimum,
    max: node.maximum,
    step: node.type === "integer" ? 1 : "any",
  });
  input.addEventListener("input", () => {
    if (input.value === "") slot.set(undefined);
    else if (!Number.isNaN(input.valueAsNumber)) slot.set(input.valueAsNumber);
  });
  return input;
}

function textInput(slot, node, required) {
  const input = h("input.control", { type: "text", value: slot.get() ?? "", placeholder: node.default ?? "" });
  input.addEventListener("input", () => slot.set(input.value === "" && !required ? undefined : input.value));
  return input;
}

function textArea(slot, node, required, rows) {
  const area = h("textarea.control.control--area", { rows, spellcheck: "false", placeholder: node.default ?? "" });
  area.value = slot.get() ?? "";
  area.addEventListener("input", () => slot.set(area.value === "" && !required ? undefined : area.value));
  return area;
}

function enumSelect(slot, values, fallback, required) {
  const current = slot.get();
  const node = h(
    "select.control",
    {},
    required ? null : h("option", { value: "", selected: current == null }, `Default${fallback != null ? ` (${fallback})` : ""}`),
    values.map((value) => h("option", { value, selected: value === current }, value)),
  );
  node.addEventListener("change", () => slot.set(node.value === "" ? undefined : node.value));
  return node;
}

/** A choice among keys defined elsewhere in the file; a missing one stays visible. */
function keySelect(slot, keys, fallback, required) {
  const current = slot.get();
  const known = current == null || keys.includes(current) ? keys : [...keys, current];
  const node = h(
    "select.control",
    {},
    required ? null : h("option", { value: "", selected: current == null }, fallback ? `Default (${fallback})` : "—"),
    known.map((key) =>
      h("option", { value: key, selected: key === current }, keys.includes(key) ? key : `${key} (not defined)`),
    ),
  );
  node.addEventListener("change", () => slot.set(node.value === "" ? undefined : node.value));
  return node;
}

function chips(slot, options) {
  const current = new Set(slot.get() ?? []);
  const all = [...new Set([...options(), ...current])];
  if (!all.length) return h("p.field__hint", { text: "Nothing to choose from yet." });
  return h(
    "div.chipGrid",
    {},
    all.map((value) => {
      const input = h("input", { type: "checkbox", checked: current.has(value) });
      input.addEventListener("change", () => {
        if (input.checked) current.add(value);
        else current.delete(value);
        // Keep the order the options are listed in.
        slot.set(all.filter((item) => current.has(item)));
      });
      return h("label.chipToggle", {}, input, h("span", { text: value }));
    }),
  );
}

/** Keys in an order that matters (an agent's models: tried top to bottom). */
function orderedPicker(slot, options) {
  const root = h("div.ordered");
  const read = () => {
    const value = slot.get();
    return Array.isArray(value) ? [...value] : value ? [value] : [];
  };
  const write = (items) => slot.set(items.length > 1 ? items : items[0] ?? "");
  const render = () => {
    const items = read();
    const move = (from, to) => {
      const next = [...items];
      next.splice(to, 0, ...next.splice(from, 1));
      write(next);
      render();
    };
    const rows = items.map((item, index) =>
      h(
        "div.ordered__row",
        {},
        h("span.ordered__rank", { text: String(index + 1) }),
        h("span.ordered__name", { text: item, class: options.includes(item) ? "" : "is-missing" }),
        h("button.iconBtn.iconBtn--small", { type: "button", title: "Up", disabled: index === 0, on: { click: () => move(index, index - 1) } }, "↑"),
        h(
          "button.iconBtn.iconBtn--small",
          { type: "button", title: "Down", disabled: index === items.length - 1, on: { click: () => move(index, index + 1) } },
          "↓",
        ),
        h(
          "button.iconBtn.iconBtn--small",
          {
            type: "button",
            title: "Remove",
            on: {
              click: () => {
                write(items.filter((_, position) => position !== index));
                render();
              },
            },
          },
          icon(ICONS.close, { size: 12 }),
        ),
      ),
    );
    const free = options.filter((option) => !items.includes(option));
    const add = h(
      "select.control.ordered__add",
      {},
      h("option", { value: "" }, free.length ? "Add…" : "All added"),
      free.map((option) => h("option", { value: option }, option)),
    );
    add.disabled = !free.length;
    add.addEventListener("change", () => {
      if (!add.value) return;
      write([...items, add.value]);
      render();
    });
    root.replaceChildren(...rows, add);
  };
  render();
  return root;
}

function lineList(slot) {
  const area = h("textarea.control.control--area", { rows: 3, spellcheck: "false", placeholder: "One per line" });
  area.value = (slot.get() ?? []).join("\n");
  area.addEventListener("input", () => {
    const lines = area.value
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    slot.set(lines.length ? lines : undefined);
  });
  return area;
}

function keyValue(slot) {
  const area = h("textarea.control.control--area", { rows: 3, spellcheck: "false", placeholder: "KEY=value" });
  area.value = Object.entries(slot.get() ?? {})
    .map(([name, value]) => `${name}=${value}`)
    .join("\n");
  area.addEventListener("input", () => {
    const entries = area.value
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => {
        const [name, ...rest] = line.split("=");
        return [name.trim(), rest.join("=")];
      })
      .filter(([name]) => name);
    slot.set(entries.length ? Object.fromEntries(entries) : undefined);
  });
  return area;
}

/** Anything without a better control - lists of rules, free-form sections - as JSON. */
function jsonArea(slot) {
  const value = slot.get();
  const area = h("textarea.control.control--area.control--code", { rows: value == null ? 2 : 6, spellcheck: "false", placeholder: "JSON" });
  area.value = value == null ? "" : JSON.stringify(value, null, 2);
  area.addEventListener("input", () => {
    if (!area.value.trim()) {
      area.classList.remove("is-invalid");
      slot.set(undefined);
      return;
    }
    try {
      slot.set(JSON.parse(area.value));
      area.classList.remove("is-invalid");
    } catch {
      area.classList.add("is-invalid");
    }
  });
  return area;
}

function group(label, hint, body, slot) {
  const set = slot.get() != null;
  const node = h(
    "details.group",
    { open: false },
    h(
      "summary.group__head",
      {},
      h("span.group__title", { text: label }),
      h("span.group__state", { text: set ? "set in this file" : "defaults" }),
    ),
    hint ? h("p.field__hint.group__hint", { text: hint }) : null,
    h("div.group__body", {}, body),
  );
  node.dataset.shape = "group";
  return node;
}

/** A new record: its required fields, empty. */
function blankOf(schema) {
  const record = {};
  for (const key of schema.required ?? []) {
    const property = schema.properties?.[key] ?? {};
    record[key] = property.type === "array" ? [] : "";
  }
  return record;
}

export { blankOf };
