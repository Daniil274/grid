/**
 * Configuration drawer: every config file the server runs, every setting in it.
 *
 * The server lists the files an admin may edit - the routing catalog and each
 * system's config (web_chat.deployment.config_files) - and the drawer switches
 * between them. The forms are drawn from the config schema (schema-form.js),
 * so nothing the schema knows needs the YAML tab; agents, tools, models,
 * providers and prompts get a master-detail editor on top.
 *
 * Edits go to a clone of the file's document and are written back whole; the
 * server merges them into the file, keeping its comments. The raw YAML tab is
 * the other way in: whichever tab is open on Save decides which is sent.
 */

import { h, replace } from "../lib/dom.js";
import { api } from "../net/api.js";
import { toast } from "../ui/toast.js";
import { CollectionEditor } from "./collection.js";
import { SchemaKit, blankOf, child, rootSlot } from "./schema-form.js";

/** Settings shown on the General tab; the rest of `settings` is on Advanced. */
const GENERAL_SETTINGS = [
  "default_agent",
  "max_history",
  "max_turns",
  "agent_timeout",
  "max_tool_output_tokens",
  "max_tool_output",
  "working_directory",
  "config_directory",
  "logs_directory",
  "proxy",
  "allowed_models",
  "mcp_enabled",
  "allow_path_override",
  "debug",
  "tools_common_rules",
];

export class SettingsDrawer {
  /** @param {Record<string, HTMLElement>} nodes ids resolved by the caller */
  constructor(nodes, { onSaved } = {}) {
    this.nodes = nodes;
    this.onSaved = onSaved;
    this.payload = null;
    this.config = null;
    this.snapshot = "";
    this.target = null;
    this.activeTab = null;
    this._bindChrome();
  }

  async open() {
    await this.load(this.target);
    this.nodes.drawer.classList.add("is-open");
    document.body.classList.add("has-overlay");
  }

  close() {
    if (this._dirty() && !window.confirm("Discard unsaved changes?")) return;
    this.nodes.drawer.classList.remove("is-open");
    document.body.classList.remove("has-overlay");
  }

  async load(target) {
    this._accept(await api.getSettings(target));
  }

  _accept(payload) {
    this.payload = payload;
    this.target = payload.target;
    this.config = structuredClone(payload.config ?? {});
    this.snapshot = JSON.stringify(this.config);
    this.kit = new SchemaKit(payload.schema, this._refs());
    this._renderFiles();
    this._renderTabs();
    // A server started before config files could be chosen sends neither the
    // files nor the schema: it can only edit its default system.
    if (!payload.files || !payload.schema) {
      this._status("The server runs older code: only the default system can be edited. Restart it to edit every system.", "error");
    } else {
      this._status("");
    }
  }

  _dirty() {
    return this.config != null && JSON.stringify(this.config) !== this.snapshot;
  }

  // -- chrome ------------------------------------------------------------
  _bindChrome() {
    const { drawer, backdrop, closeButton, saveButton, reloadButton } = this.nodes;
    backdrop.addEventListener("click", () => this.close());
    closeButton.addEventListener("click", () => this.close());
    saveButton.addEventListener("click", () => this._save());
    reloadButton.addEventListener("click", async () => {
      if (this._dirty() && !window.confirm("Discard unsaved changes?")) return;
      await this.load(this.target);
      this._status("Re-read from disk.");
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && drawer.classList.contains("is-open")) this.close();
    });
  }

  /** The files to switch between: the catalog, then one per system. */
  _renderFiles() {
    const { files = [], target } = this.payload;
    const current = files.find((file) => file.key === target);
    this.nodes.title.textContent = current?.kind === "catalog" ? "Routing and shared settings" : `System · ${current?.name ?? ""}`;
    this.nodes.subtitle.textContent = current?.path ?? "";
    replace(
      this.nodes.files,
      files.map((file) =>
        h(
          "button.fileTab",
          {
            type: "button",
            class: file.key === target ? "is-active" : "",
            title: file.path,
            on: { click: () => this._switchFile(file.key) },
          },
          h("span.fileTab__kind", { text: file.kind === "catalog" ? "Catalog" : "System" }),
          h("span.fileTab__name", { text: file.name }),
        ),
      ),
    );
    this.nodes.files.hidden = files.length < 2;
  }

  async _switchFile(key) {
    if (key === this.target) return;
    if (this._dirty() && !window.confirm("Discard unsaved changes to this file?")) return;
    try {
      await this.load(key);
    } catch (error) {
      toast(error.message, { tone: "error" });
    }
  }

  _tabs() {
    if (this.payload.kind === "catalog") {
      return [
        ["routing", "Systems", () => this._routingPane()],
        ["models", "Models", () => this._collectionPane("models", "ModelConfig", "model")],
        ["providers", "Providers", () => this._collectionPane("providers", "ProviderConfig", "provider")],
        ["policy", "Policy", () => this._sectionsPane([["settings", "Settings", { only: ["action_policy", "proxy", "debug"] }], ["compact", "Context compaction"]])],
        ["users", "Users", () => this._sectionsPane([["user_limits", "User limits"], ["personal_agents", "Personal agents"], ["review", "Answer reviews"]])],
        ["voice", "Voice", () => this._sectionsPane([["voice", "Voice"]])],
        ["yaml", "YAML", () => this._yamlPane()],
      ];
    }
    return [
      ["general", "General", () => this._generalPane()],
      ["agents", "Agents", () => this._collectionPane("agents", "AgentConfig", "agent")],
      ["tools", "Tools", () => this._collectionPane("tools", "ToolConfig", "tool")],
      ["models", "Models", () => this._collectionPane("models", "ModelConfig", "model")],
      ["providers", "Providers", () => this._collectionPane("providers", "ProviderConfig", "provider")],
      ["prompts", "Prompts", () => this._promptsPane()],
      [
        "advanced",
        "Advanced",
        () =>
          this._sectionsPane([
            ["settings", "Action policy, logging, images, project tools", { skip: GENERAL_SETTINGS }],
            ["compact", "Context compaction"],
            ["routing", "Routing between this system's agents"],
            ["user_limits", "User limits"],
            ["personal_agents", "Personal agents"],
            ["review", "Answer reviews"],
            ["voice", "Voice"],
            ["scenarios", "Scenarios"],
          ]),
      ],
      ["yaml", "YAML", () => this._yamlPane()],
    ];
  }

  _renderTabs() {
    const tabs = this._tabs();
    if (!tabs.some(([id]) => id === this.activeTab)) this.activeTab = tabs[0][0];
    replace(
      this.nodes.tabs,
      tabs.map(([id, label]) =>
        h("button.tab", { type: "button", class: id === this.activeTab ? "is-active" : "", on: { click: () => this._switchTab(id) } }, label),
      ),
    );
    const [, , render] = tabs.find(([id]) => id === this.activeTab);
    replace(this.nodes.body, h("section.pane", {}, render()));
  }

  _switchTab(id) {
    this.activeTab = id;
    this._renderTabs();
  }

  _status(text, tone = "info") {
    this.nodes.status.textContent = text;
    this.nodes.status.dataset.tone = tone;
  }

  async _save() {
    try {
      this._status("Saving…");
      const payload =
        this.activeTab === "yaml"
          ? await api.saveYaml(this.yamlArea.value, this.target)
          : await api.saveSettings(this.config, this.target);
      this._accept(payload);
      this._status("Saved and validated.", "ok");
      toast("Configuration saved", { tone: "success" });
      await this.onSaved?.();
    } catch (error) {
      this._status(error.message, "error");
      toast(error.message, { tone: "error" });
    }
  }

  // -- references between keys of the file ----------------------------------
  _refs() {
    const keys = (section) => () => Object.keys(this.config?.[section] ?? {});
    const models = keys("models");
    return {
      "settings.default_agent": { kind: "select", options: keys("agents") },
      "settings.allowed_models": { kind: "multi", options: models },
      "settings.action_policy.validator.model": { kind: "select", options: models },
      "agents.*.model": { kind: "ordered", options: models },
      "agents.*.tools": { kind: "multi", options: keys("tools") },
      "agents.*.base_prompt": { kind: "select", options: () => [...new Set(["base", ...keys("prompt_templates")()])] },
      "tools.*.target_agent": { kind: "select", options: keys("agents") },
      "models.*.provider": { kind: "select", options: keys("providers") },
      "compact.summary_model": { kind: "select", options: models },
      "routing.model": { kind: "select", options: models },
      "routing.default_system": { kind: "select", options: () => Object.keys(this.config?.routing?.systems ?? {}) },
      "personal_agents.models": { kind: "multi", options: models },
      "voice.decision_model": { kind: "select", options: models },
    };
  }

  // -- panes -------------------------------------------------------------
  _slot() {
    return rootSlot(() => this.config);
  }

  _card(title, description, body) {
    return h("div.card", {}, h("h3", { text: title }), description ? h("p.card__hint", { text: description }) : null, body);
  }

  _generalPane() {
    const kit = this.kit;
    const root = this._slot();
    const meta = this.payload.meta ?? {};
    const facts = [
      ["Config file", meta.config_path],
      ["Agents", Object.keys(this.config.agents ?? {}).length],
      ["Tools", Object.keys(this.config.tools ?? {}).length],
      ["Models", Object.keys(this.config.models ?? {}).length],
      ["Your workspace", meta.workspace_path],
      ["Isolation", meta.isolation_enabled ? `on (${meta.container_id || "docker"})` : "off"],
    ];
    return [
      this._card("General", "", kit.objectForm(kit.section("settings"), child(root, "settings"), ["settings"], { only: GENERAL_SETTINGS })),
      this._card(
        "Isolation",
        "Where this system's tools run. A server with accounts always runs users' agents in their own container.",
        kit.objectForm(kit.section("isolation"), child(root, "isolation"), ["isolation"]),
      ),
      this._card(
        "Runtime",
        "",
        h(
          "div.factGrid",
          {},
          facts.map(([label, value]) =>
            h("div.fact", {}, h("span.fact__label", { text: label }), h("span.fact__value", { text: String(value ?? "—") })),
          ),
        ),
      ),
    ];
  }

  _routingPane() {
    const kit = this.kit;
    const form = kit.objectForm(kit.section("routing"), child(this._slot(), "routing"), ["routing"]);
    // The systems are what this tab is for: no fold to open first.
    for (const group of form.querySelectorAll("details.group")) group.open = true;
    return this._card(
      "Routing",
      "The systems the chat routes between, and the model that picks one for each message. Each system's own agents, tools and models are in its tab above.",
      form,
    );
  }

  _sectionsPane(sections) {
    const kit = this.kit;
    const root = this._slot();
    return sections.map(([key, title, options]) => {
      const schema = kit.section(key);
      const slot = child(root, key);
      const body = schema.properties
        ? kit.objectForm(schema, slot, [key], options)
        : kit.field(key, schema, slot, [key]);
      return this._card(title, options ? "" : schema.description ?? "", body);
    });
  }

  _collectionPane(section, defName, noun) {
    const kit = this.kit;
    const schema = kit.resolve({ $ref: `#/$defs/${defName}` });
    const list = h("div.recordList");
    const editor = h("div.master__detail");
    const addButton = h("button.btn.btn--ghost", { type: "button" }, "Add");
    const collection = new CollectionEditor({
      list,
      editor,
      addButton,
      noun,
      records: () => (this.config[section] ??= {}),
      describe: (key, value) => describeRecord(section, key, value),
      blank: () => this._blank(section, schema),
      renderDetail: (key) =>
        h("div.pane__form", {}, kit.objectForm(schema, child(child(this._slot(), section), key), [section, "*"])),
      onDelete: (key) => this._dropReferences(section, key),
    });
    collection.render();
    return h(
      "div.master",
      {},
      h("div.master__list", {}, h("div.master__head", {}, h("h3", { text: `${capitalize(section)}` }), addButton), list),
      editor,
    );
  }

  _promptsPane() {
    const list = h("div.recordList");
    const editor = h("div.master__detail");
    const addButton = h("button.btn.btn--ghost", { type: "button" }, "Add");
    const collection = new CollectionEditor({
      list,
      editor,
      addButton,
      noun: "prompt",
      records: () => (this.config.prompt_templates ??= {}),
      describe: (key, value) => ({ title: key, meta: `${String(value ?? "").length} characters` }),
      blank: () => "",
      renderDetail: (key) => {
        const area = h("textarea.control.control--area", { rows: 18, spellcheck: "false", placeholder: "System prompt…" });
        area.value = this.config.prompt_templates[key] ?? "";
        area.addEventListener("input", () => {
          this.config.prompt_templates[key] = area.value;
        });
        return h("label.field", {}, h("span.field__label", { text: "Template body" }), area);
      },
    });
    collection.render();
    return h("div.master", {}, h("div.master__list", {}, h("div.master__head", {}, h("h3", { text: "Prompts" }), addButton), list), editor);
  }

  _yamlPane() {
    this.yamlArea = h("textarea.yamlEditor", { spellcheck: "false" });
    this.yamlArea.value = this.payload.raw_yaml ?? "";
    return h(
      "div.card.card--flush",
      {},
      h("h3", { text: "Raw YAML" }),
      h("p.card__hint", { text: "The file as it is on disk. Saving from this tab writes it as typed; edits made in the other tabs are not in it." }),
      this.yamlArea,
    );
  }

  _blank(section, schema) {
    const record = blankOf(schema);
    if (section === "agents") {
      Object.assign(record, { name: "New agent", model: Object.keys(this.config.models ?? {})[0] ?? "", tools: [], description: "" });
    } else if (section === "tools") {
      Object.assign(record, { type: "function", description: "" });
    } else if (section === "models") {
      record.provider = Object.keys(this.config.providers ?? {})[0] ?? "";
    }
    return record;
  }

  /** Deleting a record takes its references with it, so the file stays valid. */
  _dropReferences(section, key) {
    if (section === "agents" && this.config.settings?.default_agent === key) delete this.config.settings.default_agent;
    if (section === "tools") {
      for (const agent of Object.values(this.config.agents ?? {})) {
        agent.tools = (agent.tools ?? []).filter((tool) => tool !== key);
      }
    }
    if (section === "models") {
      for (const agent of Object.values(this.config.agents ?? {})) {
        if (Array.isArray(agent.model)) agent.model = agent.model.filter((model) => model !== key);
      }
    }
  }
}

const capitalize = (text) => text.charAt(0).toUpperCase() + text.slice(1);

function describeRecord(section, key, value) {
  switch (section) {
    case "agents": {
      const model = Array.isArray(value.model) ? value.model.join(" → ") : value.model || "no model";
      return { title: value.name || key, meta: `${key} · ${model} · ${(value.tools ?? []).length} tools` };
    }
    case "tools":
      return { title: value.name || key, meta: `${key} · ${value.type || "function"}` };
    case "models":
      return { title: key, meta: `${value.name || "?"} · ${value.provider || "no provider"}` };
    case "providers":
      return { title: value.name || key, meta: value.base_url || key };
    default:
      return { title: key, meta: "" };
  }
}
