/**
 * "My agents": the signed-in user's own agents (web_chat/personal_agents.py).
 *
 * A master list and an editor. An agent starts from a template the server
 * offers; the editor can only narrow it - pick a subset of the template's
 * tools, one of the offered models - and add the owner's instructions. The
 * server checks every rule again; this form only offers what it allows.
 *
 * A tool list left untouched is sent as `null`, "the template's tools", so the
 * agent follows the template when the operator changes it.
 */

import { h, replace } from "../lib/dom.js";
import { api } from "../net/api.js";
import { chipPicker, field, select, textArea, textInput, toggle } from "../settings/fields.js";

const templateId = (template) => `${template.system}/${template.agent}`;

export class PersonalAgentsDrawer {
  /**
   * @param {object} nodes drawer, backdrop, closeButton, newButton, list, editor, status, saveButton, deleteButton
   * @param {{onChanged: Function}} callbacks onChanged runs after a save or delete
   */
  constructor(nodes, { onChanged }) {
    this.nodes = nodes;
    this.onChanged = onChanged;
    this.catalog = { agents: [], templates: [], limits: {} };
    this.selectedKey = null;
    this.draft = null;
    this._bind();
  }

  async open() {
    this.nodes.drawer.classList.add("is-open");
    await this._load();
    const [first] = this.catalog.agents;
    if (first) this._edit(first);
    else this._new();
  }

  close() {
    this.nodes.drawer.classList.remove("is-open");
  }

  _bind() {
    const { drawer, backdrop, closeButton, newButton, saveButton, deleteButton } = this.nodes;
    backdrop.addEventListener("click", () => this.close());
    closeButton.addEventListener("click", () => this.close());
    newButton.addEventListener("click", () => this._new());
    saveButton.addEventListener("click", () => this._save());
    deleteButton.addEventListener("click", () => this._delete());
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && drawer.classList.contains("is-open")) this.close();
    });
  }

  async _load() {
    this.catalog = await api.agents();
    this._renderList();
  }

  _status(text, tone = "") {
    this.nodes.status.textContent = text;
    this.nodes.status.dataset.tone = tone;
  }

  _renderList() {
    const { agents } = this.catalog;
    replace(
      this.nodes.list,
      agents.length
        ? agents.map((agent) =>
            h(
              "button.recordRow",
              {
                type: "button",
                class: agent.key === this.selectedKey ? "is-active" : "",
                on: { click: () => this._edit(agent) },
              },
              h("span.recordRow__title", { text: agent.name }),
              h("span.recordRow__meta", { text: `${agent.system} · ${agent.template}` }),
            ),
          )
        : h("p.pane__empty", { text: "No agents yet." }),
    );
    const full = agents.length >= (this.catalog.limits.max_agents ?? Infinity);
    this.nodes.newButton.disabled = full || !this.catalog.templates.length;
  }

  _new() {
    const [template] = this.catalog.templates;
    if (!template) {
      this.selectedKey = null;
      this.draft = null;
      replace(this.nodes.editor, h("p.pane__empty", { text: "The server offers no templates for personal agents." }));
      this._syncActions();
      return;
    }
    this._edit(null, {
      name: "",
      description: "",
      system: template.system,
      template: template.agent,
      model: null,
      tools: null,
      instructions: "",
      routable: false,
    });
  }

  _edit(agent, draft = null) {
    this.selectedKey = agent?.key ?? null;
    this.draft = draft ?? {
      name: agent.name,
      description: agent.description,
      system: agent.system,
      template: agent.template,
      model: agent.model,
      tools: agent.tools,
      instructions: agent.instructions,
      routable: agent.routable,
    };
    this._status("");
    this._renderList();
    this._renderEditor();
    this._syncActions();
  }

  _syncActions() {
    this.nodes.saveButton.disabled = !this.draft;
    this.nodes.deleteButton.hidden = !this.selectedKey;
  }

  _template() {
    return this.catalog.templates.find((t) => t.system === this.draft.system && t.agent === this.draft.template);
  }

  _renderEditor() {
    const draft = this.draft;
    const template = this._template();
    const templatePicker = h(
      "select.control",
      {},
      this.catalog.templates.map((t) =>
        h("option", { value: templateId(t), selected: t === template }, `${t.system_name} / ${t.name}`),
      ),
    );
    templatePicker.addEventListener("change", () => {
      const chosen = this.catalog.templates.find((t) => templateId(t) === templatePicker.value);
      Object.assign(draft, { system: chosen.system, template: chosen.agent, model: null, tools: null });
      this._renderEditor();
    });

    // "" stands for the template's own model (sent as null).
    const modelChoice = { model: draft.model ?? "" };
    const models = template
      ? [
          { value: "", label: `Template's model (${template.models[0]?.name ?? template.model})` },
          ...template.models.slice(1).map((model) => ({ value: model.key, label: model.name })),
        ]
      : [];
    const modelPicker = select(modelChoice, "model", models);
    modelPicker.addEventListener("change", () => {
      draft.model = modelChoice.model || null;
    });

    const allTools = template?.tools ?? [];
    const tools = chipPicker(
      draft.tools ?? allTools.map((tool) => tool.key),
      allTools.map((tool) => ({ value: tool.key, label: tool.key })),
      (chosen) => {
        draft.tools = chosen;
      },
    );

    const limit = this.catalog.limits.max_instructions_chars;
    replace(
      this.nodes.editor,
      h(
        "div.pane",
        {},
        template ? null : h("p.field__hint", { text: "This agent's template is no longer offered; pick another." }),
        field("Name", textInput(draft, "name", { placeholder: "Code reviewer" })),
        field("Description", textInput(draft, "description", { placeholder: "What you use it for" })),
        field("Template", templatePicker, template?.description || null),
        field("Model", modelPicker),
        field("Tools", tools, "Only the template's tools; uncheck what this agent should not use."),
        field(
          "Instructions",
          textArea(draft, "instructions", { rows: 8, placeholder: "How this agent should work for you" }),
          limit ? `Added after the template's prompt. Up to ${limit} characters.` : null,
        ),
        toggle(draft, "routable", "Let the router pick this agent on its own"),
      ),
    );
  }

  async _save() {
    const draft = { ...this.draft, name: this.draft.name.trim() };
    if (!draft.name) {
      this._status("Give the agent a name.", "error");
      return;
    }
    this.nodes.saveButton.disabled = true;
    try {
      const saved = this.selectedKey
        ? await api.updateAgent(this.selectedKey, draft)
        : await api.createAgent(draft);
      await this._load();
      this._edit(this.catalog.agents.find((agent) => agent.key === saved.key) ?? saved);
      this._status("Saved", "ok");
      await this.onChanged();
    } catch (error) {
      this._status(error.message, "error");
    } finally {
      this._syncActions();
    }
  }

  async _delete() {
    const agent = this.catalog.agents.find((item) => item.key === this.selectedKey);
    if (!agent || !confirm(`Delete the agent "${agent.name}"? Its chats stay.`)) return;
    try {
      await api.deleteAgent(agent.key);
      await this._load();
      this._new();
      this._status("Deleted", "ok");
      await this.onChanged();
    } catch (error) {
      this._status(error.message, "error");
    }
  }
}
