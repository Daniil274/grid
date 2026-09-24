/**
 * System configuration drawer.
 *
 * Edits a clone of the server's config document and writes the whole thing
 * back, which is what the YAML file on disk is anyway. The structured tabs and
 * the raw YAML tab are two views of that same document: whichever tab is open
 * when you hit Save decides which representation is sent.
 */

import { $$, h, replace } from "../lib/dom.js";
import { api } from "../net/api.js";
import { toast } from "../ui/toast.js";
import { CollectionEditor } from "./collection.js";
import { chipPicker, field, keyValueArea, lineListArea, optionsFrom, select, textArea, textInput, toggle } from "./fields.js";

const SYSTEM_FIELDS = [
  { key: "default_agent", label: "Default agent", kind: "agent" },
  { key: "max_history", label: "Max history", kind: "number", hint: "Messages kept in context" },
  { key: "max_turns", label: "Max turns", kind: "number", hint: "Tool-calling rounds per message" },
  { key: "agent_timeout", label: "Agent timeout", kind: "number", hint: "Seconds" },
  { key: "working_directory", label: "Working directory", kind: "text" },
  { key: "config_directory", label: "Config directory", kind: "text" },
  { key: "mcp_enabled", label: "MCP enabled", kind: "toggle" },
  { key: "allow_path_override", label: "Allow path override", kind: "toggle" },
];

export class SettingsDrawer {
  /** @param {Record<string, HTMLElement>} nodes ids resolved by the caller */
  constructor(nodes, { onSaved } = {}) {
    this.nodes = nodes;
    this.onSaved = onSaved;
    this.config = null;
    this.meta = null;
    this.activeTab = "system";

    this._buildCollections();
    this._bindChrome();
  }

  async open() {
    await this.load();
    this.nodes.drawer.classList.add("is-open");
    document.body.classList.add("has-overlay");
  }

  close() {
    this.nodes.drawer.classList.remove("is-open");
    document.body.classList.remove("has-overlay");
  }

  async load() {
    const payload = await api.getSettings();
    this.config = structuredClone(payload.config);
    this.meta = payload.meta;
    this.nodes.yaml.value = payload.raw_yaml;
    this._renderAll();
  }

  // -- chrome ------------------------------------------------------------
  _bindChrome() {
    const { drawer, backdrop, closeButton, saveButton, reloadButton, tabs } = this.nodes;
    backdrop.addEventListener("click", () => this.close());
    closeButton.addEventListener("click", () => this.close());
    saveButton.addEventListener("click", () => this._save());
    reloadButton.addEventListener("click", async () => {
      await this.load();
      this._status("Re-read from disk.");
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && drawer.classList.contains("is-open")) this.close();
    });
    for (const tab of $$("[data-tab]", tabs)) {
      tab.addEventListener("click", () => this._switchTab(tab.dataset.tab));
    }
    this._switchTab(this.activeTab);
  }

  _switchTab(name) {
    this.activeTab = name;
    for (const tab of $$("[data-tab]", this.nodes.tabs)) tab.classList.toggle("is-active", tab.dataset.tab === name);
    for (const [key, pane] of Object.entries(this.nodes.panes)) pane.hidden = key !== name;
  }

  _status(text, tone = "info") {
    this.nodes.status.textContent = text;
    this.nodes.status.dataset.tone = tone;
  }

  async _save() {
    try {
      this._status("Saving…");
      const payload =
        this.activeTab === "yaml" ? await api.saveYaml(this.nodes.yaml.value) : await api.saveSettings(this.config);
      this.config = structuredClone(payload.config);
      this.meta = payload.meta;
      this.nodes.yaml.value = payload.raw_yaml;
      this._renderAll();
      this._status("Saved and validated.", "ok");
      toast("Configuration saved", { tone: "success" });
      await this.onSaved?.();
    } catch (error) {
      this._status(error.message, "error");
      toast(error.message, { tone: "error" });
    }
  }

  // -- panes -------------------------------------------------------------
  _renderAll() {
    this._renderSystem();
    this.agents.render();
    this.tools.render();
    this.prompts.render();
  }

  _renderSystem() {
    const settings = (this.config.settings ??= {});
    const agentOptions = optionsFrom(this.config.agents);

    replace(
      this.nodes.systemForm,
      SYSTEM_FIELDS.map(({ key, label, kind, hint }) => {
        if (kind === "toggle") return h("div.field", {}, toggle(settings, key, label));
        if (kind === "agent") return field(label, select(settings, key, agentOptions), hint);
        return field(label, textInput(settings, key, { type: kind === "number" ? "number" : "text" }), hint);
      }),
    );

    const facts = [
      // In catalog mode this is the default system's config, not routing.yaml:
      // it is the file the tabs above edit.
      ["Config file", this.meta.config_path],
      ["Workspace", this.meta.workspace_path],
      ["Persisted to", this.meta.persist_path],
      ["Isolation", this.meta.isolation_enabled ? `on (${this.meta.container_id || "docker"})` : "off"],
      ["Agents", Object.keys(this.config.agents ?? {}).length],
      ["Tools", Object.keys(this.config.tools ?? {}).length],
      ["Prompt templates", Object.keys(this.config.prompt_templates ?? {}).length],
    ];
    replace(
      this.nodes.runtimeFacts,
      facts.map(([label, value]) =>
        h("div.fact", {}, h("span.fact__label", { text: label }), h("span.fact__value", { text: String(value ?? "—") })),
      ),
    );
  }

  _buildCollections() {
    const { nodes } = this;

    this.agents = new CollectionEditor({
      list: nodes.agentsList,
      editor: nodes.agentEditor,
      addButton: nodes.addAgent,
      noun: "agent",
      records: () => (this.config.agents ??= {}),
      describe: (key, value) => ({ title: value.name || key, meta: `${key} · ${value.model || "no model"}` }),
      blank: () => ({
        name: "New agent",
        model: Object.keys(this.config.models ?? {})[0] ?? "",
        tools: [],
        base_prompt: Object.keys(this.config.prompt_templates ?? {})[0] ?? "base",
        custom_prompt: "",
        description: "",
        mcp_enabled: false,
      }),
      renderDetail: (key, agent) => this._agentForm(agent),
      onDelete: (key) => {
        if (this.config.settings?.default_agent === key) delete this.config.settings.default_agent;
        this._renderSystem();
      },
    });

    this.tools = new CollectionEditor({
      list: nodes.toolsList,
      editor: nodes.toolEditor,
      addButton: nodes.addTool,
      noun: "tool",
      records: () => (this.config.tools ??= {}),
      describe: (key, value) => ({ title: value.name || key, meta: `${key} · ${value.type || "function"}` }),
      blank: () => ({ type: "function", name: "", description: "", prompt_addition: "" }),
      renderDetail: (key, tool) => this._toolForm(tool),
      onDelete: (key) => {
        for (const agent of Object.values(this.config.agents ?? {})) {
          agent.tools = (agent.tools ?? []).filter((toolKey) => toolKey !== key);
        }
        this.agents.render();
      },
    });

    this.prompts = new CollectionEditor({
      list: nodes.promptsList,
      editor: nodes.promptEditor,
      addButton: nodes.addPrompt,
      noun: "prompt",
      records: () => (this.config.prompt_templates ??= {}),
      describe: (key, value) => ({ title: key, meta: `${String(value ?? "").length} characters` }),
      blank: () => "",
      renderDetail: (key) =>
        field("Template body", textArea(this.config.prompt_templates, key, { rows: 18, placeholder: "System prompt…" })),
    });
  }

  _agentForm(agent) {
    const toolOptions = optionsFrom(this.config.tools);
    return h(
      "div.pane__form",
      {},
      h(
        "div.formGrid",
        {},
        field("Display name", textInput(agent, "name")),
        field(
          "Models (fallback order)",
          lineListArea(
            {
              get model() {
                return Array.isArray(agent.model) ? agent.model : agent.model ? [agent.model] : [];
              },
              set model(value) {
                agent.model = value;
              },
            },
            "model",
            { rows: 3, placeholder: Object.keys(this.config.models ?? {}).join("\n") },
          ),
          "One model key per line. Requests try them from top to bottom.",
        ),
        field("Base prompt", select(agent, "base_prompt", optionsFrom(this.config.prompt_templates, (key) => key))),
        field("Description", textInput(agent, "description", { placeholder: "Shown in the agent picker" })),
        h("div.field", {}, toggle(agent, "mcp_enabled", "MCP enabled")),
      ),
      field("Custom prompt", textArea(agent, "custom_prompt", { rows: 8, placeholder: "Appended to the base prompt" })),
      field(
        "Tools",
        toolOptions.length
          ? chipPicker(agent.tools ?? [], toolOptions, (next) => {
              agent.tools = next;
            })
          : h("p.pane__empty", { text: "No tools defined yet." }),
      ),
    );
  }

  _toolForm(tool) {
    return h(
      "div.pane__form",
      {},
      h(
        "div.formGrid",
        {},
        field("Display name", textInput(tool, "name")),
        field(
          "Type",
          select(
            tool,
            "type",
            ["function", "mcp", "agent"].map((value) => ({ value, label: value })),
          ),
        ),
        field("Target agent", textInput(tool, "target_agent", { placeholder: "agent key" })),
        field("Context strategy", textInput(tool, "context_strategy")),
      ),
      field("Description", textArea(tool, "description", { rows: 3 })),
      field("Prompt addition", textArea(tool, "prompt_addition", { rows: 3 })),
      field("Server command", lineListArea(tool, "server_command", { placeholder: "one argument per line" })),
      field("Environment variables", keyValueArea(tool, "env_vars")),
    );
  }
}
