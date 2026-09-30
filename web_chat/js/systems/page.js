/**
 * The systems page (/systems, web_chat/system_hub.py).
 *
 * Admins make created systems (a copy of an existing one, then edited), test
 * them, publish them, and import the systems users submit. A user builds
 * systems of their own from the operator's templates, tests them, lets the
 * router use them, and submits them to the admins.
 *
 * Every system has the same tabs: what it is (its agents and health), how to
 * test it (open it in the chat, ask the router where sample messages go), what
 * it did (the activity the server records), and, when the user may edit it,
 * its editor. Text from configs and users is shown as text, never as markup.
 */

import { h, replace } from "../lib/dom.js";
import { api } from "../net/api.js";
import { chipPicker, field } from "../settings/fields.js";
import { applyStoredTheme } from "../ui/theme.js";
import { toast } from "../ui/toast.js";

applyStoredTheme();

const nodes = {
  filters: document.getElementById("filters"),
  list: document.getElementById("system-list"),
  detail: document.getElementById("detail"),
  newButton: document.getElementById("new-system"),
  hint: document.getElementById("hint"),
};

const state = {
  overview: null,
  filter: "",
  selected: null, // {kind, key}
  detail: null,
  tab: "overview",
};

const STATUS_LABEL = {
  catalog: "catalog",
  draft: "draft",
  published: "published",
  archived: "archived",
  active: "active for me",
  submitted: "submitted",
  pending: "waiting for admins",
  imported: "imported",
  declined: "declined",
  withdrawn: "withdrawn",
};

const when = (seconds) => (seconds ? new Date(seconds * 1000).toLocaleString() : "—");
const duration = (ms) => (ms >= 60000 ? `${Math.round(ms / 6000) / 10} min` : `${Math.round(ms / 100) / 10} s`);
const statusChip = (status) => h("span.statusChip", { dataset: { status }, text: STATUS_LABEL[status] ?? status });
const input = (props = {}) => h("input.control", { type: "text", ...props });
const area = (props = {}) => h("textarea.control.control--area", { spellcheck: "false", ...props });
const section = (title, ...children) => h("section.systemSection", {}, title ? h("h3", { text: title }) : null, ...children);
const hintText = (text) => h("p.systemSection__hint", { text });
const statusLine = () => h("p.systemStatus");
const say = (line, text, tone = "") => {
  line.textContent = text;
  line.dataset.tone = tone;
};

/** Run a change, report a refusal inline, and reload what it touched. */
async function act(line, change, done, reselect) {
  try {
    say(line, "Working…");
    const result = await change();
    say(line, done, "ok");
    toast(done);
    await loadOverview();
    if (reselect !== undefined) {
      const next = typeof reselect === "function" ? reselect(result) : reselect;
      if (next) await select(next.kind, next.key);
      else showEmpty();
    } else if (state.selected) {
      await select(state.selected.kind, state.selected.key);
    }
  } catch (error) {
    say(line, error.message, "error");
  }
}

// -- the list ---------------------------------------------------------------------
function filters() {
  const admin = state.overview?.admin;
  const mineOn = state.overview?.user_systems?.enabled || state.overview?.items.some((item) => ["mine", "built"].includes(item.kind));
  const all = [["", "All"]];
  if (admin) {
    all.push(["draft", "Drafts"], ["published", "Published"], ["submitted", "Submitted"]);
  }
  if (mineOn) all.push(["mine", "Mine"]);
  if (admin) all.push(["catalog", "Catalog"], ["archived", "Archived"]);
  return all;
}

function matches(item) {
  const filter = state.filter;
  if (!filter) return item.status !== "archived";
  if (filter === "mine") return item.kind === "mine" || item.kind === "built";
  if (filter === "catalog") return item.kind === "catalog";
  if (filter === "draft") return item.kind === "created" && item.status === "draft";
  return item.status === filter;
}

function renderFilters() {
  replace(
    nodes.filters,
    filters().map(([value, label]) =>
      h("button.reviewChip", {
        type: "button",
        role: "tab",
        "aria-selected": String(state.filter === value),
        text: label,
        on: { click: () => { state.filter = value; renderFilters(); renderList(); } },
      }),
    ),
  );
}

function renderList() {
  const items = (state.overview?.items ?? []).filter(matches);
  replace(
    nodes.list,
    items.length
      ? items.map((item) =>
          h(
            "button.reviewItem",
            {
              type: "button",
              dataset: { selected: String(state.selected?.kind === item.kind && state.selected?.key === item.key) },
              on: { click: () => void select(item.kind, item.key) },
            },
            h("span.reviewItem__head", {}, h("strong", { text: item.name }), statusChip(item.status)),
            h("span.reviewItem__route", {
              text: [
                item.kind === "mine" ? "mine" : item.kind === "submission" ? `from ${item.by}` : item.key,
                `${item.turns} turn${item.turns === 1 ? "" : "s"}`,
                item.errors ? `${item.errors} failed` : null,
                item.last_at ? `last ${when(item.last_at)}` : null,
              ]
                .filter(Boolean)
                .join(" · "),
            }),
            item.description ? h("span.reviewItem__note", { text: item.description }) : null,
          ),
        )
      : h("p.reviewList__empty", { text: "Nothing here yet." }),
  );
}

async function loadOverview() {
  state.overview = await api.systems();
  const { admin, created_enabled: created, user_systems: users } = state.overview;
  nodes.newButton.hidden = !(state.overview.builder || (admin && created) || users?.enabled);
  nodes.hint.textContent = admin
    ? "Make systems, test them, follow them, publish them; import what users submit."
    : "Describe a system to the builder, try your draft and let your router use it.";
  renderFilters();
  renderList();
}

function showEmpty() {
  state.selected = null;
  state.detail = null;
  replace(nodes.detail, h("p.reviewCase__empty", { text: "Choose a system on the left, or make a new one." }));
  renderList();
}

// -- one system -------------------------------------------------------------------
async function select(kind, key) {
  state.selected = { kind, key };
  renderList();
  try {
    state.detail = await api.system(kind, key);
  } catch (error) {
    replace(nodes.detail, h("p.reviewCase__empty", { text: error.message }));
    return;
  }
  if (!tabs().some(([value]) => value === state.tab)) state.tab = "overview";
  renderDetail();
}

function tabs() {
  const detail = state.detail;
  const list = [["overview", "Overview"]];
  if (detail.kind !== "submission") list.push(["test", "Test"]);
  list.push(["activity", "Activity"]);
  if (["created", "mine", "built"].includes(detail.kind)) list.push(["edit", "Edit"]);
  return list;
}

function renderDetail() {
  const detail = state.detail;
  const line = statusLine();
  replace(
    nodes.detail,
    h(
      "div.reviewCase__head",
      {},
      h(
        "div",
        {},
        h("h2", { text: detail.name }),
        h("p.reviewCase__meta", { text: origin(detail) }),
      ),
      statusChip(detail.status),
    ),
    h("div.systemActions", {}, actions(detail, line)),
    line,
    h(
      "div.reviewCase__tabs",
      { role: "tablist" },
      tabs().map(([value, label]) =>
        h("button.reviewChip", {
          type: "button",
          role: "tab",
          "aria-selected": String(state.tab === value),
          text: label,
          on: { click: () => { state.tab = value; renderDetail(); } },
        }),
      ),
    ),
    tabBody(detail),
  );
}

function origin(detail) {
  if (detail.kind === "built") return `your system · made with the builder · changed ${when(detail.updated_at)}`;
  if (detail.kind === "catalog") return `${detail.key} · written by the operator in the catalog; edit it in the chat's settings`;
  if (detail.kind === "created") {
    const from = detail.origin?.kind === "user"
      ? `imported from ${detail.origin.username || detail.origin.user_id}'s system`
      : `copy of ${detail.origin?.source} · by ${detail.origin?.username || "an admin"}`;
    return `${detail.key} · ${from} · changed ${when(detail.updated_at)}`;
  }
  if (detail.kind === "mine") return `your system · built on ${detail.base} · changed ${when(detail.updated_at)}`;
  const record = detail.submission;
  return `submitted by ${record.username || record.user_id} on ${when(record.at)} · built on ${detail.spec.base}`;
}

/** Open a new chat with the system pinned (web_chat/js/main.js reads ?system=). */
const openInChat = (key) => h("a.btn.btn--ghost", { href: `/?system=${encodeURIComponent(key)}`, text: "Open in chat" });

function actions(detail, line) {
  const key = detail.key;
  const buttons = [];
  if (detail.kind === "built") {
    buttons.push(openInChat(detail.chat_key));
    buttons.push(detail.active
      ? button("Stop routing to it", "ghost", () => act(line, () => api.activateBuiltSystem(key, false), "The router no longer uses it"))
      : button("Let the router use it", "primary", () => act(line, () => api.activateBuiltSystem(key, true), "Your router may now use it")));
    buttons.push(h("a.btn.btn--ghost", {
      href: `/?system=${encodeURIComponent(state.overview.builder)}&prompt=${encodeURIComponent(`Измени мою систему ${key}. `)}`,
      text: "Edit with builder",
    }));
    buttons.push(button("Delete", "danger", () => {
      if (confirm(`Delete ${detail.name} and its files?`)) void act(line, () => api.deleteBuiltSystem(key), "Deleted", null);
    }));
  }
  if (detail.kind === "catalog") {
    buttons.push(openInChat(key));
  }
  if (detail.kind === "created") {
    if (detail.status !== "archived") buttons.push(openInChat(key));
    if (detail.status === "draft") {
      buttons.push(button("Publish", "primary", () =>
        act(line, () => api.setSystemStatus(key, "published"), "Published: the router now offers it to everyone"),
      ));
    }
    if (detail.status === "published") {
      buttons.push(button("Withdraw to draft", "ghost", () =>
        act(line, () => api.setSystemStatus(key, "draft"), "Withdrawn: only admins see it again"),
      ));
    }
    if (detail.status === "archived") {
      buttons.push(button("Restore as draft", "ghost", () => act(line, () => api.setSystemStatus(key, "draft"), "Restored")));
    } else {
      buttons.push(button("Archive", "ghost", () => act(line, () => api.setSystemStatus(key, "archived"), "Archived")));
    }
    if (detail.status !== "published") {
      buttons.push(button("Delete", "danger", () => {
        if (!confirm(`Delete ${detail.name} and its files for good?`)) return;
        void act(line, () => api.deleteSystem(key), "Deleted", null);
      }));
    }
  }
  if (detail.kind === "mine") {
    buttons.push(openInChat(key));
    buttons.push(
      detail.active
        ? button("Stop routing to it", "ghost", () => act(line, () => api.activateMySystem(key, false), "The router no longer uses it"))
        : button("Let the router use it", "primary", () =>
            act(line, () => api.activateMySystem(key, true), "The router may now send your messages to it"),
          ),
    );
    const submission = detail.submission;
    if (submission?.state === "pending") {
      buttons.push(button("Withdraw submission", "ghost", () => act(line, () => api.withdrawMySystem(key), "Submission withdrawn")));
    } else {
      buttons.push(button("Offer to everyone", "ghost", () => {
        const note = prompt("A note for the admins (optional): what is it for, how did you test it?", "");
        if (note === null) return;
        void act(line, () => api.submitMySystem(key, note), "Submitted: the admins will look at it");
      }));
    }
    buttons.push(button("Delete", "danger", () => {
      if (!confirm(`Delete ${detail.name}?`)) return;
      void act(line, () => api.deleteMySystem(key), "Deleted", null);
    }));
  }
  if (detail.kind === "submission" && detail.status === "pending") {
    const keyInput = input({ placeholder: "system key, e.g. research-team", "aria-label": "Key for the new system" });
    buttons.push(keyInput);
    buttons.push(button("Import as draft", "primary", () =>
      act(line, () => api.importSubmission(key, keyInput.value.trim()), "Imported as a draft", (manifest) => ({
        kind: "created",
        key: manifest.key,
      })),
    ));
    buttons.push(button("Decline", "danger", () => {
      const note = prompt("Why (the user sees this):", "");
      if (note === null) return;
      void act(line, () => api.declineSubmission(key, note), "Declined", null);
    }));
  }
  return buttons;
}

function button(text, tone, onClick) {
  return h(`button.btn.btn--${tone}`, { type: "button", text, on: { click: onClick } });
}

function tabBody(detail) {
  if (state.tab === "test") return testTab(detail);
  if (state.tab === "activity") return activityTab(detail);
  if (state.tab === "edit") return ["created", "built"].includes(detail.kind) ? createdEditor(detail) : mineEditor(detail);
  return overviewTab(detail);
}

// -- overview ---------------------------------------------------------------------
function overviewTab(detail) {
  const inspection = detail.inspection;
  const submission = detail.kind === "mine" ? detail.submission : null;
  return h(
    "div.reviewCase",
    { style: { padding: "0" } },
    section(
      "What the router reads",
      detail.description
        ? h("p.systemText", { text: detail.description })
        : hintText("No description yet. The router picks a system by it: without one it will not be offered."),
    ),
    submission
      ? section(
          "Offered to everyone",
          h("p.systemText", {
            text: `${STATUS_LABEL[submission.state] ?? submission.state} since ${when(submission.at)}`
              + (submission.system_key ? ` · imported as ${submission.system_key}` : "")
              + (submission.note ? ` · admins: ${submission.note}` : ""),
          }),
        )
      : null,
    detail.kind === "submission" && detail.submission.note
      ? section("The user's note", h("p.systemText", { text: detail.submission.note }))
      : null,
    section("Configuration", health(inspection)),
    detail.quality ? qualityPanel(detail.quality) : null,
    section("Agents", agentsTable(inspection.agents)),
  );
}

function health(inspection) {
  if (!inspection.loaded) {
    return h("ul.checkList", { dataset: { tone: "error" } }, h("li", { text: `The config does not load: ${inspection.error}` }));
  }
  const parts = [];
  if (inspection.config.length) {
    parts.push(hintText("Config problems - the system breaks where they are; fix them before publishing:"));
    parts.push(h("ul.checkList", { dataset: { tone: "error" } }, inspection.config.map((text) => h("li", { text }))));
  }
  if (inspection.environment.length) {
    parts.push(hintText("What this machine lacks - the config is fine, these tools will fail until it is fixed:"));
    parts.push(h("ul.checkList", { dataset: { tone: "warn" } }, inspection.environment.map((text) => h("li", { text }))));
  }
  return parts.length ? parts : h("p.checkOk", { text: "No configuration problems found. Task quality and runtime connections require separate tests." });
}

function qualityPanel(quality) {
  const report = quality.evaluation;
  const contract = quality.contract;
  return section(
    "Quality checks",
    h("p", { text: `Scenarios: ${quality.state.replaceAll("_", " ")} · Package tests: ${quality.tools_tested ? "current" : "missing or stale"}` }),
    hintText(quality.ready ? "The current version passed its development checks." : "This version has not completed its quality checks."),
    quality.error ? hintText(quality.error) : null,
    contract ? h("div", {},
      h("p", { text: `Result: ${contract.result}` }),
      h("p", { text: `Acceptance: ${contract.acceptance}` }),
      h("p", { text: `On failure: ${contract.failure_behavior}` }),
      h("ul", {}, contract.examples.map((text) => h("li", { text }))),
      h("ul", {}, contract.limitations.map((text) => h("li", { text }))),
    ) : hintText("Ask the builder to add a quality contract and acceptance scenarios."),
    report ? h("div", {},
      h("p", { text: `${report.results.filter((r) => r.passed).length}/${report.expected_runs} passed · ${report.seconds}s · ${report.tokens} tokens · ${report.tool_calls} tool calls` }),
      report.error ? hintText(report.error) : null,
      h("ul", {}, report.results.map((r) => h("li", { text: `${r.suite || "development"} / ${r.name} #${r.repetition}: ${r.passed ? "passed" : "failed"}${r.error ? ` — ${r.error}` : ""}` }))),
    ) : null,
    quality.design ? h("ul", {}, [...quality.design.errors, ...quality.design.warnings].map((text) => h("li", { text }))) : null,
    hintText(quality.evidence_scope),
  );
}

function agentsTable(agents) {
  if (!agents.length) return hintText("No agents.");
  return h(
    "table.systemTable",
    {},
    h("thead", {}, h("tr", {}, ["Agent", "Model", "Tools", "Takes messages"].map((text) => h("th", { text })))),
    h(
      "tbody",
      {},
      agents.map((agent) =>
        h(
          "tr",
          {},
          h("td", {}, h("strong", { text: agent.name }), h("div.statTile__label", { text: `${agent.key}${agent.default ? " · default" : ""}` }), agent.description ? h("div", { text: agent.description }) : null),
          h("td", { text: agent.model }),
          h("td", { text: agent.tools.join(", ") || "—" }),
          h("td", { text: agent.default ? "yes, the default" : agent.routable ? "when routed" : "only when called" }),
        ),
      ),
    ),
  );
}

// -- test -------------------------------------------------------------------------
function testTab(detail) {
  const messages = area({ rows: 6, placeholder: "One message per line, the way users would write them.\nWhat is new in Qt 6.10?\nRefactor the parser and add tests" });
  const line = statusLine();
  const results = h("div");
  const run = async () => {
    const lines = messages.value.split("\n").map((text) => text.trim()).filter(Boolean).slice(0, 10);
    if (!lines.length) return say(line, "Write a message or two first.", "error");
    say(line, "Asking the router…");
    try {
      const probe = await api.probeSystem(detail.kind, detail.key, lines);
      say(line, `${probe.hits} of ${probe.results.length} would go to ${detail.name}.`, probe.hits ? "ok" : "");
      replace(results, probeTable(detail, probe));
    } catch (error) {
      say(line, error.message, "error");
    }
  };
  return h(
    "div.reviewCase",
    { style: { padding: "0" } },
    section(
      "Try it",
      hintText(
        detail.kind === "created" && detail.status === "draft"
          ? "A draft is offered to admins only: open a chat with it pinned and talk to it as a user would."
          : "Open a chat with the system pinned and talk to it; the turns show up under Activity.",
      ),
      h("div.systemActions", {}, openInChat(detail.chat_key ?? detail.key)),
    ),
    ["created", "built"].includes(detail.kind) ? acceptanceEditor(detail) : null,
    section(
      "Where would the router send these?",
      hintText("The router picks from the systems it offers now, with this one among them, by their descriptions. Messages that should land here and do not mean the description needs work."),
      messages,
      h("div.systemActions", {}, button("Ask the router", "primary", () => void run())),
      line,
      results,
    ),
  );
}

function acceptanceEditor(detail) {
  const request = area({ rows: 3, placeholder: "A task you expect this system to solve." });
  const expected = area({ rows: 3, placeholder: "Expected text, or JSON when checking a JSON file." });
  const inputPath = input({ placeholder: "Optional input file, e.g. input.txt" });
  const inputContent = area({ rows: 3, placeholder: "Contents of the input file for this test." });
  const path = input({ placeholder: "Optional output file, e.g. result.json" });
  const kind = h("select.control", {},
    h("option", { value: "contains", text: "Contains this text" }),
    h("option", { value: "equals", text: "Exactly this text" }),
    h("option", { value: "json_equals", text: "Equals this JSON" }),
  );
  const line = statusLine();
  const suite = detail.acceptance || { scenarios: [], repetitions: 1 };
  const save = async () => {
    if (!request.value.trim()) return say(line, "Enter a test request.", "error");
    try {
      const value = kind.value === "json_equals" ? JSON.parse(expected.value) : expected.value;
      const assertion = { kind: kind.value, value };
      if (path.value.trim()) assertion.path = path.value.trim();
      const updated = { ...suite, scenarios: [...suite.scenarios, {
        name: `owner-${Date.now()}`, message: request.value.trim(), assertions: [assertion],
        fixtures: inputPath.value.trim() ? { [inputPath.value.trim()]: inputContent.value } : {},
      }] };
      await act(line, () => api.saveSystemAcceptance(detail.kind, detail.key, JSON.stringify(updated)), "Acceptance case saved; run the checks again");
    } catch (error) {
      say(line, error.message, "error");
    }
  };
  return section("Your acceptance cases",
    hintText("Add your own examples. The builder cannot edit these checks. Each run starts with a fresh test workspace. You can supply an input file below."),
    h("ul", {}, suite.scenarios.map((scenario, index) => h("li", {},
      h("span", { text: scenario.message }),
      suite.scenarios.length > 1 ? button("Remove", "ghost", () => {
        const updated = { ...suite, scenarios: suite.scenarios.filter((_, i) => i !== index) };
        return act(line, () => api.saveSystemAcceptance(detail.kind, detail.key, JSON.stringify(updated)), "Case removed; run the checks again");
      }) : null,
    ))),
    field("Test request", request), field("Input file", inputPath), field("Input contents", inputContent),
    field("Check", kind), field("Output file (leave empty to check the answer)", path),
    field("Expected result", expected),
    h("div.systemActions", {}, button("Add acceptance case", "primary", () => void save()),
      state.overview.builder ? h("a.btn.btn--ghost", {
        href: `/?system=${encodeURIComponent(state.overview.builder)}&prompt=${encodeURIComponent(`Проверь систему ${detail.key} инструментом builder_evaluate, сообщи результаты. Не меняй ожидаемые результаты проверок.`)}`,
        text: "Run checks with builder",
      }) : null,
    ), line,
  );
}

function probeTable(detail, probe) {
  return h(
    "table.systemTable",
    {},
    h("thead", {}, h("tr", {}, ["Message", "Router picks"].map((text) => h("th", { text })))),
    h(
      "tbody",
      {},
      probe.results.map((result) =>
        h(
          "tr",
          {},
          h("td", { text: result.message }),
          h(`td.${result.hit ? "is-hit" : "is-miss"}`, { text: result.hit ? `✓ ${detail.name}` : result.chosen }),
        ),
      ),
    ),
  );
}

// -- activity ---------------------------------------------------------------------
function activityTab(detail) {
  const activity = detail.activity;
  const outcomes = activity.outcomes ?? {};
  const tile = (value, label) => h("div.statTile", {}, h("span.statTile__value", { text: String(value) }), h("span.statTile__label", { text: label }));
  return h(
    "div.reviewCase",
    { style: { padding: "0" } },
    section(
      "Use",
      h(
        "div.statTiles",
        {},
        tile(activity.turns, "turns"),
        tile(outcomes.answered ?? 0, "answered"),
        tile(outcomes.error ?? 0, "failed"),
        tile((outcomes.interrupted ?? 0) + (outcomes.stopped ?? 0), "interrupted or stopped"),
        tile(activity.policy_blocks, "calls the policy held"),
        tile(activity.user_count, "users"),
        tile(activity.turns ? duration(activity.average_ms) : "—", "average turn"),
        tile(activity.last_at ? when(activity.last_at) : "never", "last used"),
      ),
    ),
    section(
      "Recent turns",
      activity.recent.length
        ? h(
            "table.systemTable",
            {},
            h("thead", {}, h("tr", {}, ["When", "Who", "Agent", "Ended", "Took", "Held"].map((text) => h("th", { text })))),
            h(
              "tbody",
              {},
              activity.recent.map((turn) =>
                h(
                  "tr",
                  {},
                  h("td", { text: when(turn.at) }),
                  h("td", { text: turn.user }),
                  h("td", { text: turn.agent }),
                  h("td", { text: turn.outcome }),
                  h("td", { text: duration(turn.duration_ms) }),
                  h("td", { text: String(turn.policy_blocks || "") }),
                ),
              ),
            ),
          )
        : hintText("No turns yet. Open it in the chat to try it."),
    ),
    section(
      "History",
      activity.events.length
        ? h(
            "table.systemTable",
            {},
            h(
              "tbody",
              {},
              activity.events.map((event) =>
                h("tr", {}, h("td", { text: when(event.at) }), h("td", { text: event.event }), h("td", { text: event.by }), h("td", { text: event.note })),
              ),
            ),
          )
        : hintText("Nothing recorded yet."),
    ),
  );
}

// -- created systems: details and config ------------------------------------------
function createdEditor(detail) {
  const describe = detail.kind === "built" ? api.describeBuiltSystem : api.describeSystem;
  const saveConfig = detail.kind === "built" ? api.saveBuiltSystemConfig : api.saveSystemConfig;
  const draft = { name: detail.name, description: detail.description, requires: (detail.requires ?? []).join(", ") };
  const name = input({ value: draft.name });
  const description = area({ rows: 4 });
  description.value = draft.description;
  const requires = input({ value: draft.requires, placeholder: "ffmpeg, git" });
  const detailsLine = statusLine();
  const yaml = h("textarea.control.codeArea", { spellcheck: "false" });
  yaml.value = detail.config_yaml;
  const yamlLine = statusLine();
  return h(
    "div.reviewCase",
    { style: { padding: "0" } },
    section(
      "Name and description",
      field("Name", name),
      field("Description", description, "What the router reads to decide whether a message is for this system."),
      field("Programs it needs on PATH", requires, "Comma-separated; the health check looks for them."),
      h(
        "div.systemActions",
        {},
        button("Save", "primary", () =>
          act(detailsLine, () =>
            describe(detail.key, {
              name: name.value.trim(),
              description: description.value.trim(),
              requires: requires.value.split(",").map((item) => item.trim()).filter(Boolean),
            }), "Saved"),
        ),
      ),
      detailsLine,
    ),
    section(
      "config.yaml",
      hintText(
        detail.kind === "built" ? "Your system's config. Changes apply to your own chats; new Python tools run as tool packages."
        : detail.status === "published"
          ? "This system is published: a saved change reaches every user on their next turn."
          : `The system's Grid config (${detail.config_path}). Saving checks it against the config schema; the health check runs again on the Overview.`,
      ),
      yaml,
      h("div.systemActions", {}, button("Save config", "primary", () => act(yamlLine, () => saveConfig(detail.key, yaml.value), "Config saved"))),
      yamlLine,
    ),
  );
}

// -- user systems: the member editor ----------------------------------------------
function blankMember(template, index) {
  return {
    id: index === 0 ? "lead" : `helper${index}`,
    name: index === 0 ? "Lead" : `Helper ${index}`,
    description: "",
    template: template?.agent ?? "",
    model: null,
    tools: null,
    instructions: "",
    delegates: [],
  };
}

function specFrom(detail) {
  const fields = ["name", "description", "base", "entry", "members"];
  return structuredClone(Object.fromEntries(fields.map((key) => [key, detail[key]])));
}

function mineEditor(detail, { creating = false } = {}) {
  const offer = state.overview.user_systems;
  const bases = offer.bases;
  if (!bases.length) return section("", hintText("The server offers no templates to build systems from."));
  const spec = creating
    ? { name: "", description: "", base: bases[0].system, entry: "lead", members: [blankMember(bases[0].templates[0], 0)] }
    : specFrom(detail);
  const container = h("div.reviewCase", { style: { padding: "0" } });
  const line = statusLine();

  const baseOf = () => bases.find((base) => base.system === spec.base) ?? bases[0];
  const templateOf = (member) => baseOf().templates.find((template) => template.agent === member.template);

  const render = () => {
    const name = input({ value: spec.name, placeholder: "Research team" });
    name.addEventListener("input", () => { spec.name = name.value; });
    const description = area({ rows: 3, placeholder: "What the router should send here, e.g. \"Market research with sources: competitors, prices, reviews\"." });
    description.value = spec.description;
    description.addEventListener("input", () => { spec.description = description.value; });
    const base = h(
      "select.control",
      { disabled: !creating },
      bases.map((item) => h("option", { value: item.system, selected: item.system === spec.base, text: item.name })),
    );
    base.addEventListener("change", () => {
      spec.base = base.value;
      spec.members = [blankMember(baseOf().templates[0], 0)];
      spec.entry = spec.members[0].id;
      render();
    });
    const entry = h(
      "select.control",
      {},
      spec.members.map((member) => h("option", { value: member.id, selected: member.id === spec.entry, text: member.name || member.id })),
    );
    entry.addEventListener("change", () => { spec.entry = entry.value; });

    replace(
      container,
      section(
        creating ? "New system of your own" : "The system",
        hintText("Agents built from the server's templates. Each can use its template's tools or fewer, and hand work to the agents you let it."),
        h("div.formGrid", {}, field("Name", name), field("Built on", base, creating ? "" : "Fixed once the system exists.")),
        field("Description", description, "The router reads it when you let it use this system."),
        field("Takes your messages", entry, "The agent that answers you; the others work when it asks them."),
      ),
      spec.members.map((member, index) => memberCard(member, index)),
      h(
        "div.systemActions",
        {},
        button("Add an agent", "ghost", () => {
          if (spec.members.length >= offer.limits.max_members) return say(line, `At most ${offer.limits.max_members} agents.`, "error");
          spec.members.push(blankMember(baseOf().templates[0], spec.members.length));
          render();
        }),
        button(creating ? "Create" : "Save", "primary", () => void save()),
      ),
      line,
    );
  };

  const memberCard = (member, index) => {
    const template = templateOf(member);
    const id = input({ value: member.id, placeholder: "researcher" });
    id.addEventListener("change", () => {
      const old = member.id;
      member.id = id.value.trim();
      for (const other of spec.members) other.delegates = other.delegates.map((value) => (value === old ? member.id : value));
      if (spec.entry === old) spec.entry = member.id;
      render();
    });
    const name = input({ value: member.name });
    name.addEventListener("input", () => { member.name = name.value; });
    const description = input({ value: member.description, placeholder: "What it is good at" });
    description.addEventListener("input", () => { member.description = description.value; });
    const templateSelect = h(
      "select.control",
      {},
      baseOf().templates.map((item) => h("option", { value: item.agent, selected: item.agent === member.template, text: item.name })),
    );
    templateSelect.addEventListener("change", () => {
      member.template = templateSelect.value;
      member.model = null;
      member.tools = null;
      render();
    });
    const model = h(
      "select.control",
      {},
      (template?.models ?? []).map((item) =>
        h("option", { value: item.key, selected: item.key === (member.model ?? template.model), text: item.name }),
      ),
    );
    model.addEventListener("change", () => { member.model = model.value === template?.model ? null : model.value; });
    const tools = (template?.tools ?? []).map((tool) => ({ value: tool.key, label: tool.key }));
    const toolPicker = chipPicker(member.tools ?? tools.map((tool) => tool.value), tools, (chosen) => {
      member.tools = chosen.length === tools.length ? null : chosen;
    });
    const others = spec.members.filter((other) => other !== member);
    const delegates = others.length
      ? chipPicker(member.delegates, others.map((other) => ({ value: other.id, label: other.name || other.id })), (chosen) => {
          member.delegates = chosen;
        })
      : hintText("Add another agent to let this one hand work to it.");
    const instructions = area({ rows: 5, placeholder: "How this agent should work: its role, what to hand to whom, the format of its answer." });
    instructions.value = member.instructions;
    instructions.addEventListener("input", () => { member.instructions = instructions.value; });
    return h(
      "div.memberCard",
      {},
      h(
        "div.memberCard__head",
        {},
        h("span", { text: `${member.name || member.id}${member.id === spec.entry ? " · takes your messages" : ""}` }),
        spec.members.length > 1
          ? button("Remove", "ghost", () => {
              spec.members.splice(index, 1);
              for (const other of spec.members) other.delegates = other.delegates.filter((value) => value !== member.id);
              if (spec.entry === member.id) spec.entry = spec.members[0].id;
              render();
            })
          : null,
      ),
      h("div.formGrid", {}, field("Id", id, "Lowercase letters, digits, _"), field("Name", name), field("Template", templateSelect, template?.description), field("Model", model)),
      field("Good at", description, "Shown to the agents that can hand work to it."),
      field("Tools", toolPicker),
      field("Can hand work to", delegates),
      field("Instructions", instructions),
    );
  };

  const save = async () => {
    try {
      say(line, "Saving…");
      const saved = creating ? await api.createMySystem(spec) : await api.updateMySystem(detail.key, spec);
      toast(creating ? "System created" : "Saved");
      await loadOverview();
      state.tab = creating ? "test" : "edit";
      await select("mine", saved.key);
    } catch (error) {
      say(line, error.message, "error");
    }
  };

  render();
  return container;
}

// -- new systems ------------------------------------------------------------------
function newSystem() {
  const { admin, created_enabled: created, user_systems: users } = state.overview;
  state.selected = null;
  renderList();
  const views = [];
  if (state.overview.builder) views.push(["builder", "Describe it to the builder"]);
  if (admin && created) views.push(["created", "Blank or a copy"]);
  if (users?.enabled) views.push(["mine", "Of my own agents"]);
  let chosen = views[0][0];
  const body = h("div");
  const draw = () =>
    replace(body, chosen === "builder" ? newWithBuilder() : chosen === "created" ? newCreated() : mineEditor(null, { creating: true }));
  replace(
    nodes.detail,
    h("div.reviewCase__head", {}, h("h2", { text: "New system" })),
    views.length > 1
      ? h(
          "div.reviewCase__tabs",
          { role: "tablist" },
          views.map(([value, label]) =>
            h("button.reviewChip", {
              type: "button",
              role: "tab",
              "aria-selected": String(chosen === value),
              text: label,
              on: { click: (event) => {
                chosen = value;
                for (const chip of event.currentTarget.parentNode.children) chip.setAttribute("aria-selected", String(chip === event.currentTarget));
                draw();
              } },
            }),
          ),
        )
      : null,
    body,
  );
  draw();
}

function newWithBuilder() {
  const request = area({
    rows: 8,
    placeholder:
      "What the system is for, who uses it, what it gets and gives back, which tools it needs.\n\n" +
      "E.g.: a system for text analysis - word and sentence statistics of files in the workspace, repeated phrases, a short report.",
  });
  return section(
    "",
    hintText(
      "The builder is the system that makes systems: it designs the agents, writes the config, prompts and skills from scratch, " +
        "writes the tools the system lacks as tested tool packages, and leaves a draft here for you to try " +
        (state.overview.admin ? "and publish. " : "and activate for your own chats. ") +
        "It runs as a chat: it may ask you something, and you can ask it for changes.",
    ),
    field("What should the new system do?", request),
    h(
      "div.systemActions",
      {},
      button("Open the builder", "primary", () => {
        const text = request.value.trim();
        const prompt = text ? `Создай новую систему. ${text}` : "";
        location.assign(`/?system=${encodeURIComponent(state.overview.builder)}${prompt ? `&prompt=${encodeURIComponent(prompt)}` : ""}`);
      }),
    ),
  );
}

function newCreated() {
  const sources = state.overview.sources;
  const key = input({ placeholder: "research-team" });
  const name = input({ placeholder: "Research team" });
  const description = area({ rows: 3, placeholder: "What the router should send here." });
  const source = h(
    "select.control",
    {},
    h("option", { value: "", text: "Blank: one agent, no tools" }),
    sources.map((item) => h("option", { value: item, text: `Copy of ${item}` })),
  );
  const agentsBox = h("div");
  const line = statusLine();
  let agents = null;
  let allAgents = [];

  const loadAgents = async () => {
    agents = null;
    if (!source.value) {
      replace(agentsBox, hintText("A blank system starts with one agent on the server's flash model; edit its config after."));
      return;
    }
    replace(agentsBox, hintText("Loading its agents…"));
    try {
      const kind = state.overview.items.some((item) => item.kind === "catalog" && item.key === source.value) ? "catalog" : "created";
      const detail = await api.system(kind, source.value);
      allAgents = detail.inspection.agents.map((agent) => agent.key);
      replace(
        agentsBox,
        chipPicker(allAgents, detail.inspection.agents.map((agent) => ({ value: agent.key, label: agent.name })), (chosen) => {
          agents = chosen.length === allAgents.length ? null : chosen;
        }),
      );
    } catch (error) {
      replace(agentsBox, hintText(error.message));
    }
  };
  source.addEventListener("change", () => void loadAgents());
  void loadAgents();

  return section(
    "",
    hintText("A new system starts blank or as a copy of an existing one - its agents, tools, models and skills - in systems/<key>/ of the repository. It stays a draft, seen by admins only, until you publish it."),
    h("div.formGrid", {}, field("Key", key, "The directory and the router id: a-z, 0-9, - and _."), field("Name", name)),
    field("Description", description, "What the router reads to decide whether a message is for this system."),
    field("Start from", source),
    field("Agents to keep", agentsBox),
    h(
      "div.systemActions",
      {},
      button("Create draft", "primary", () =>
        act(line, () =>
          api.createSystem({
            key: key.value.trim(),
            name: name.value.trim() || key.value.trim(),
            description: description.value.trim(),
            source: source.value || null,
            agents,
          }), "Draft created", (manifest) => {
            state.tab = "edit";
            return { kind: "created", key: manifest.key };
          }),
      ),
    ),
    line,
  );
}

nodes.newButton.addEventListener("click", () => newSystem());

loadOverview()
  .then(() => {
    const params = new URLSearchParams(location.search);
    if (params.get("kind") && params.get("key")) void select(params.get("kind"), params.get("key"));
  })
  .catch((error) => replace(nodes.detail, h("p.reviewCase__empty", { text: error.message })));
