/**
 * "Agent memory": browse and manage what the agent remembers (web_chat/memory_api.py).
 *
 * A drawer like "My agents": a summary (backend, counts per layer, the share of
 * the prompt budget auto-injection takes), layer tabs with a scope filter, a
 * stale-only switch and a live search (the server ranks by BM25 + RRF), the
 * records as cards, and a form beside them to add or change one. A collapsible
 * block shows the prompt section the agent is actually given.
 *
 * Layers: L3 profile, L2 scenarios, L1 facts, L0 history. A record tied to a
 * file is FRESH while the file is as it was and STALE once it changed.
 */

import { debounce, h, replace } from "../lib/dom.js";
import { api } from "../net/api.js";
import { shortStamp } from "../lib/format.js";
import { field, select, textArea, textInput } from "../settings/fields.js";
import { hideOverlay, showOverlay } from "./overlay.js";

export const LAYER_TABS = [
  { value: "", label: "All" },
  { value: "L3", label: "Profile L3" },
  { value: "L2", label: "Scenarios L2" },
  { value: "L1", label: "Facts L1" },
  { value: "L0", label: "History L0" },
];

const LAYER_NAMES = { L3: "Profile", L2: "Scenario", L1: "Fact", L0: "History" };
const LAYER_COUNT_ORDER = ["L3", "L2", "L1", "L0"];
const SCOPES = ["project", "user", "system"];
const CATEGORIES = ["note", "decision", "constraint", "architecture", "bug", "preference", "workflow"];
const SEARCH_DELAY = 250;

const options = (values) => values.map((value) => ({ value, label: value }));

const blankDraft = (layer = "L1") => ({
  layer: layer || "L1",
  scope: "project",
  category: "note",
  title: "",
  content: "",
  file_path: "",
  source_ref: "",
  tagsText: "",
  confidence: 1,
});

const draftOf = (record) => ({
  layer: record.layer,
  scope: record.scope,
  category: record.category,
  title: record.title ?? "",
  content: record.content ?? "",
  file_path: record.file_path ?? "",
  source_ref: record.source_ref ?? "",
  tagsText: (record.tags ?? []).join(", "),
  confidence: record.confidence ?? 1,
});

export const parseTags = (text) => [
  ...new Set(
    String(text ?? "")
      .split(/[,\n]/)
      .map((tag) => tag.trim())
      .filter(Boolean),
  ),
];

/** The request body a draft stands for. */
export function payloadOf(draft, contextId) {
  const confidence = Number(draft.confidence);
  return {
    layer: draft.layer,
    scope: draft.scope,
    category: draft.category,
    title: draft.title.trim(),
    content: draft.content.trim(),
    file_path: draft.file_path.trim(),
    source_ref: draft.source_ref.trim(),
    tags: parseTags(draft.tagsText),
    confidence: Number.isFinite(confidence) ? Math.min(1, Math.max(0, confidence)) : 1,
    ...(contextId ? { context_id: contextId } : {}),
  };
}

const freshness = (status) => (status === "fresh" ? "FRESH" : status === "stale" ? "STALE" : "UNVERIFIED");

export class MemoryDrawer {
  /**
   * @param {object} nodes drawer, backdrop, closeButton, syncButton, newButton, status, backend, counts,
   *   budget, tabs, scope, stale (checkbox), search, previewButton, preview, previewText, list, editor
   * @param {{getContextId?: Function, onChanged?: Function}} callbacks getContextId names the chat whose
   *   system and workspace are meant; onChanged gets each fresh `{overview, config}`
   */
  constructor(nodes, { getContextId = () => null, onChanged = () => {} } = {}) {
    this.nodes = nodes;
    this.getContextId = getContextId;
    this.onChanged = onChanged;
    this.layer = "";
    this.scope = "";
    this.staleOnly = false;
    this.query = "";
    this.data = null;
    this.records = [];
    this.selectedId = null;
    this.draft = null;
    this.loadToken = 0;
    this.busy = false;
    this._bind();
  }

  async open() {
    this.nodes.drawer.classList.add("is-open");
    showOverlay(this.nodes.drawer);
    this.draft = null;
    this.selectedId = null;
    this._renderEditor();
    await this.load();
  }

  close() {
    this.nodes.drawer.classList.remove("is-open");
    hideOverlay(this.nodes.drawer);
  }

  get isOpen() {
    return this.nodes.drawer.classList.contains("is-open");
  }

  _bind() {
    const { drawer, backdrop, closeButton, syncButton, newButton, scope, stale, search, previewButton } = this.nodes;
    backdrop.addEventListener("click", () => this.close());
    closeButton.addEventListener("click", () => this.close());
    syncButton.addEventListener("click", () => this.sync());
    newButton.addEventListener("click", () => this.startNew());
    previewButton.addEventListener("click", () => this.togglePreview());
    scope.addEventListener("change", () => this.setScope(scope.value));
    stale.addEventListener("change", () => this.setStaleOnly(Boolean(stale.checked)));
    const later = debounce(() => this.setQuery(search.value), SEARCH_DELAY);
    search.addEventListener("input", later);
    search.addEventListener("keydown", (event) => {
      if (event.key === "Enter") this.setQuery(search.value);
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && drawer.classList.contains("is-open")) this.close();
    });
    this._renderTabs();
  }

  // -- filters -------------------------------------------------------------

  setLayer(layer) {
    this.layer = layer || "";
    this._renderTabs();
    return this.load();
  }

  setScope(scope) {
    this.scope = scope || "";
    return this.load();
  }

  setStaleOnly(value) {
    this.staleOnly = value;
    return this.load();
  }

  setQuery(text) {
    this.query = String(text ?? "").trim();
    return this.load();
  }

  togglePreview(force) {
    const { preview, previewButton } = this.nodes;
    const open = force ?? preview.hidden;
    preview.hidden = !open;
    previewButton.setAttribute("aria-expanded", String(open));
    previewButton.classList.toggle("is-active", open);
  }

  // -- loading -------------------------------------------------------------

  _params() {
    return {
      context_id: this.getContextId() || undefined,
      layer: this.layer || undefined,
      scope: this.scope || undefined,
      query: this.query || undefined,
      stale_only: this.staleOnly || undefined,
      limit: 200,
    };
  }

  _context() {
    return this.getContextId() || undefined;
  }

  _status(text, tone = "") {
    this.nodes.status.textContent = text;
    this.nodes.status.dataset.tone = tone;
  }

  async load() {
    const token = ++this.loadToken;
    try {
      const data = await api.memoryOverview(this._params());
      if (token !== this.loadToken) return null; // a newer request answers
      this.data = data;
      this.records = data.records ?? [];
      this._renderSummary();
      this._renderTabs();
      this._renderPreview();
      this._renderList();
      this._status(this._countLine());
      this.onChanged({ overview: data.overview, config: data.config });
      return data;
    } catch (error) {
      if (token === this.loadToken) this._status(error.message, "error");
      return null;
    }
  }

  _countLine() {
    const shown = this.records.length;
    const searching = this.query ? ` for "${this.query}"` : "";
    return shown ? `${shown} ${shown === 1 ? "record" : "records"}${searching}` : "";
  }

  // -- summary -------------------------------------------------------------

  _renderSummary() {
    const { overview = {}, config = {} } = this.data ?? {};
    const { backend, counts, budget } = this.nodes;

    const healthy = overview.healthy !== false;
    backend.textContent = "";
    backend.dataset.state = healthy ? (config.enabled_in_config ? "on" : "local") : "error";
    backend.append(
      h("span.memPill__name", { text: config.backend ?? config.configured_backend ?? overview.backend_name ?? "memory" }),
      h("span.memPill__state", {
        text: !healthy ? "unavailable" : config.enabled_in_config ? "active" : "local · not enabled in config",
      }),
    );

    const layerCounts = overview.counts ?? {};
    replace(
      counts,
      LAYER_COUNT_ORDER.map((layer) =>
        h("span.memCount", { title: `${LAYER_NAMES[layer]} records` }, h("b", { text: layer }), ` ${layerCounts[layer] ?? 0}`),
      ),
      h(
        "span.memCount.memCount--stale",
        { dataset: { has: String((overview.stale_count ?? 0) > 0) }, title: "Records whose file changed" },
        h("b", { text: "STALE" }),
        ` ${overview.stale_count ?? 0}`,
      ),
    );

    const limit = Number(overview.auto_inject_tokens ?? config.auto_inject_tokens ?? 0);
    const used = Number(overview.preview_estimated_tokens ?? 0);
    const ratio = limit > 0 ? Math.min(1, used / limit) : 0;
    const fill = h("span.memBudget__fill");
    fill.style.width = `${Math.round(ratio * 100)}%`;
    replace(
      budget,
      h("span.memBudget__label", {
        text: overview.auto_inject === false ? "Auto-inject off" : `Prompt ≈ ${used} / ${limit} tokens`,
      }),
      h("span.memBudget__bar", { role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(limit), "aria-valuenow": String(used) }, fill),
    );
    budget.dataset.over = String(limit > 0 && used >= limit);
    this.nodes.syncButton.disabled = this.busy;
  }

  _renderPreview() {
    const text = this.data?.overview?.preview_prompt_section ?? "";
    this.nodes.previewText.textContent = text || "Nothing is injected into the prompt yet.";
  }

  _renderTabs() {
    const counts = this.data?.overview?.counts ?? {};
    replace(
      this.nodes.tabs,
      LAYER_TABS.map((tab) => {
        const active = tab.value === this.layer;
        const count = tab.value ? counts[tab.value] : this.data?.overview?.total;
        return h(
          "button.tab",
          {
            type: "button",
            role: "tab",
            "aria-selected": String(active),
            class: active ? "is-active" : "",
            dataset: { layer: tab.value },
            on: { click: () => this.setLayer(tab.value) },
          },
          tab.label,
          count != null ? h("span.tab__count", { text: String(count) }) : null,
        );
      }),
    );
  }

  // -- records -------------------------------------------------------------

  _renderList() {
    const { list } = this.nodes;
    if (!this.records.length) {
      const filtered = this.query || this.layer || this.scope || this.staleOnly;
      replace(
        list,
        h("p.pane__empty", {
          text: filtered ? "Nothing matches these filters." : 'Memory is empty. Add a record or use "Extract from chat".',
        }),
      );
      return;
    }
    replace(list, this.records.map((record) => this._card(record)));
  }

  _card(record) {
    const status = record.stale_status;
    const source = record.file_path || record.source_ref;
    const title = record.title || record.content.split("\n", 1)[0].slice(0, 80);
    return h(
      "article.memCard",
      {
        class: record.id === this.selectedId ? "is-active" : "",
        dataset: { id: record.id, layer: record.layer, stale: status },
      },
      h(
        "div.memCard__head",
        {},
        h("span.memTag.memTag--layer", { text: record.layer, title: LAYER_NAMES[record.layer] }),
        h("span.memTag", { text: record.category }),
        h("span.memTag.memTag--scope", { text: record.scope }),
        h("span.memTag.memTag--fresh", { text: freshness(status), dataset: { status } }),
        h("span.memCard__time", { text: shortStamp(record.updated_at || record.created_at) }),
      ),
      h("h4.memCard__title", { text: title }),
      record.title ? h("p.memCard__body", { text: record.content }) : null,
      source
        ? h("p.memCard__source", { title: source }, h("span", { text: record.file_path ? "file" : "source" }), ` ${source}`)
        : null,
      record.tags?.length ? h("div.memCard__tags", {}, record.tags.map((tag) => h("span.memTag.memTag--tag", { text: `#${tag}` }))) : null,
      h(
        "div.memCard__actions",
        {},
        h("button.btn.btn--ghost.btn--xs", { type: "button", dataset: { action: "edit" }, on: { click: () => this.edit(record) } }, "Edit"),
        h(
          "button.btn.btn--ghost.btn--xs.btn--danger",
          { type: "button", dataset: { action: "delete" }, on: { click: () => this.remove(record) } },
          "Delete",
        ),
      ),
    );
  }

  // -- editor --------------------------------------------------------------

  startNew() {
    this.selectedId = null;
    this.draft = blankDraft(this.layer);
    this._renderList();
    this._renderEditor();
  }

  edit(record) {
    this.selectedId = record.id;
    this.draft = draftOf(record);
    this._renderList();
    this._renderEditor();
  }

  cancel() {
    this.selectedId = null;
    this.draft = null;
    this._renderList();
    this._renderEditor();
  }

  _renderEditor() {
    const { editor } = this.nodes;
    const draft = this.draft;
    if (!draft) {
      replace(
        editor,
        h("p.pane__empty", { text: "Pick a record to change it, or add a new one." }),
      );
      return;
    }
    const layers = ["L1", "L2", "L3", ...(draft.layer === "L0" ? ["L0"] : [])];
    const confidence = textInput(draft, "confidence", { type: "number" });
    confidence.setAttribute("min", "0");
    confidence.setAttribute("max", "1");
    confidence.setAttribute("step", "0.05");
    const editing = Boolean(this.selectedId);

    replace(
      editor,
      h(
        "form.pane__form.memForm",
        {
          on: {
            submit: (event) => {
              event.preventDefault?.();
              this.save();
            },
          },
        },
        h("div.pane__head", {}, h("div", {}, h("h3", { text: editing ? "Edit record" : "New record" }))),
        h(
          "div.formGrid",
          {},
          field("Layer", select(draft, "layer", options(layers))),
          field("Scope", select(draft, "scope", options(SCOPES))),
          field("Category", select(draft, "category", options(CATEGORIES))),
          field("Confidence", confidence, "0 to 1"),
        ),
        field("Title", textInput(draft, "title", { placeholder: "A short name" })),
        field("Text", textArea(draft, "content", { rows: 7, placeholder: "What should the agent remember?" })),
        field("File", textInput(draft, "file_path", { placeholder: "src/app.py" }), "Marks the record STALE when the file changes."),
        field("Source", textInput(draft, "source_ref", { placeholder: "chat:… or a link" })),
        field("Tags", textInput(draft, "tagsText", { placeholder: "api, auth" }), "Comma-separated."),
        h(
          "div.drawer__actions.memForm__actions",
          {},
          h("button.btn.btn--ghost", { type: "button", dataset: { action: "cancel" }, on: { click: () => this.cancel() } }, "Cancel"),
          h("button.btn.btn--primary", { type: "submit", dataset: { action: "save" }, on: { click: (event) => { event.preventDefault?.(); this.save(); } } }, editing ? "Save" : "Add"),
        ),
      ),
    );
  }

  async save() {
    const draft = this.draft;
    if (!draft || this.busy) return null;
    const payload = payloadOf(draft, this._context());
    if (!payload.content) {
      this._status("Write what to remember.", "error");
      return null;
    }
    this.busy = true;
    try {
      const saved = this.selectedId
        ? await api.updateMemoryRecord(this.selectedId, payload)
        : await api.createMemoryRecord(payload);
      this.selectedId = null;
      this.draft = null;
      this._renderEditor();
      this.busy = false;
      await this.load();
      this._status("Saved", "ok");
      return saved;
    } catch (error) {
      this._status(error.message, "error");
      return null;
    } finally {
      this.busy = false;
    }
  }

  async remove(record) {
    const label = record.title || record.content.slice(0, 60);
    if (!confirm(`Delete this memory record?\n\n${label}`)) return false;
    try {
      await api.deleteMemoryRecord(record.id, { context_id: this._context() });
      if (this.selectedId === record.id) {
        this.selectedId = null;
        this.draft = null;
        this._renderEditor();
      }
      await this.load();
      this._status("Deleted", "ok");
      return true;
    } catch (error) {
      this._status(error.message, "error");
      return false;
    }
  }

  /** Distil the open chat into memory. */
  async sync() {
    const contextId = this.getContextId();
    if (!contextId) {
      this._status("Open a chat first: memory is extracted from its messages.", "error");
      return null;
    }
    if (this.busy) return null;
    this.busy = true;
    this.nodes.syncButton.disabled = true;
    this.nodes.syncButton.setAttribute("aria-busy", "true");
    this._status("Extracting from the chat…");
    try {
      const result = await api.syncMemory({ context_id: contextId });
      this.busy = false;
      await this.load();
      this._status(
        result.stored
          ? `Stored ${result.stored} new ${result.stored === 1 ? "record" : "records"} from ${result.scanned} messages`
          : `Nothing new to remember in ${result.scanned} messages yet: no decisions, rules, fixes or preferences found.`,
        "ok",
      );
      return result;
    } catch (error) {
      this._status(error.message, "error");
      return null;
    } finally {
      this.busy = false;
      this.nodes.syncButton.disabled = false;
      this.nodes.syncButton.setAttribute("aria-busy", "false");
    }
  }
}
