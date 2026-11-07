/**
 * Tracker (beads) operations as cards: a list of tasks with their states,
 * one task with what the tracker recorded, a plan with its ready and waiting
 * waves, a log read as chat lines, or a single line for a write.
 *
 * The tools answer in bd's JSON or in short confirmation text; neither says
 * at a glance what changed. Cards read the payload they recognise and return
 * null for anything else, so an unexpected shape keeps the generic JSON view.
 * Nothing here is an action: every row is a read-only link into the tracker,
 * and the step's Source button still shows the untouched payload.
 *
 * Untrusted task text never becomes markup: everything goes through h() text
 * nodes. Ids are checked before they are put into a link, and every list is
 * bounded - the reader asks for the rest in chunks, never all at once.
 */

import { h } from "../lib/dom.js";

const BEAD_TOOLS = new Set([
  "beads_init", "beads_ready", "beads_create", "beads_show", "beads_update",
  "beads_close", "beads_sync", "beads_dep", "beads_plan", "beads_list",
  "beads_log_append", "beads_log_read",
]);

const FIRST_ROWS = 8; // rows shown without asking
const CHUNK_ROWS = 20; // rows revealed per press
const MAX_ROWS = 50; // hard bound on rendered list rows
const MAX_DEPS = 12; // dependency chips on one card
const MAX_LOG_LINES = 120; // hard bound on rendered log lines
const LOG_FIRST = 30; // log lines shown without asking
const LOG_CHUNK = 30; // log lines revealed per press
const MAX_BODY = 4000; // characters of a description kept on a card
const MAX_MESSAGE = 240; // characters of an error or confirmation line

const isObject = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value);
const clean = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
const shorten = (value, limit) => {
  const text = clean(value);
  return text.length > limit ? `${text.slice(0, limit).trimEnd()}…` : text;
};
const plural = (count, word, many = `${word}s`) => `${count} ${count === 1 ? word : many}`;

/** What each tracker state is called and marked with, in the words the rest
 * of the interface uses (plan-card.js says the same). */
const STATES = {
  open: { label: "Open", mark: "○" },
  in_progress: { label: "In progress", mark: "●" },
  blocked: { label: "Blocked", mark: "!" },
  deferred: { label: "Deferred", mark: "◌" },
  closed: { label: "Closed", mark: "✓" },
};
const stateOf = (status) => STATES[String(status ?? "").toLowerCase()] ?? null;

/** A tracker id that is safe in a URL path and a link's text. */
const BEAD_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const beadId = (value) => (typeof value === "string" && BEAD_ID.test(value.trim()) ? value.trim() : "");

/** The tool behind a (possibly `server.`-prefixed) step name, or "". */
function beadTool(tool) {
  const key = String(tool ?? "").toLowerCase().split(".").pop();
  return BEAD_TOOLS.has(key) ? key : "";
}

const trackerLink = (id, label, className = "beadCard__id") =>
  h(`a.${className}`, { href: `/tasks?task=${encodeURIComponent(id)}`, title: `Open ${id} in the tracker`, text: label ?? id });

/** The id as a deep link, or as plain text when it is not a usable id. */
function idView(id, className) {
  const valid = beadId(id);
  return valid ? trackerLink(valid, valid, className) : h(`span.${className}`, { text: clean(id) });
}

function stateChip(status) {
  const state = stateOf(status);
  const raw = clean(status);
  if (!state) return raw ? h("span.beadCard__tag", { text: raw }) : null;
  return h("span.beadCard__state", { dataset: { status: String(status).toLowerCase() } },
    h("span.beadCard__mark", { text: state.mark, "aria-hidden": "true" }),
    h("span", { text: state.label }));
}

const priorityTag = (priority) => (priority == null || priority === "" || !Number.isFinite(Number(priority))
  ? null
  : h("span.beadCard__tag.beadCard__pri", { dataset: { pri: String(Number(priority)) }, text: `P${Number(priority)}`, title: `Priority ${Number(priority)}` }));

const typeTag = (type) => (clean(type) ? h("span.beadCard__tag", { text: shorten(type, 24) }) : null);

const fact = (key, value) => (clean(value)
  ? h("div.beadCard__fact", {}, h("span.beadCard__key", { text: `${key}: ` }), h("span.beadCard__value", { text: String(value) }))
  : null);

/** The day a tracker timestamp names; the time of day is noise on a card. */
function dateText(value) {
  const text = clean(value);
  if (!text) return "";
  const match = /^(\d{4}-\d{2}-\d{2})/.exec(text);
  return match ? match[1] : shorten(text, 32);
}

/** One line of a confirmation or a refusal: a tone, a bold verb and details. */
const note = (tone, ...children) => h("p.beadCard__note", { dataset: { tone } }, ...children);

const list = (className, children) => h(`ul.${className}`, {}, children);

/**
 * Bounded rows: the first few are there, the rest wait behind one button and
 * arrive in chunks. A list longer than the hard bound says how much is left
 * instead of silently dropping it.
 */
function boundedRows(entries, makeRow, { className = "beadCard__rows", first = FIRST_ROWS, chunk = CHUNK_ROWS, cap = MAX_ROWS, noun = "tasks" } = {}) {
  const capped = entries.slice(0, cap);
  const rows = list(className, capped.slice(0, first).map(makeRow));
  const box = h("div.beadCard__list", {}, rows);
  const rest = capped.slice(first).map(makeRow);
  if (rest.length) {
    let at = 0;
    const more = h("button.beadCard__more", { type: "button" });
    const label = () => {
      const left = rest.length - at;
      more.textContent = `Show ${Math.min(chunk, left)} more ${noun}`;
    };
    label();
    more.addEventListener("click", () => {
      const slice = rest.slice(at, at + chunk);
      at += slice.length;
      rows.append(...slice);
      if (at >= rest.length) more.remove();
      else { label(); more.focus?.(); }
    });
    box.append(more);
  }
  if (entries.length > cap) box.append(h("small.beadCard__notice", { text: `${entries.length - cap} more ${noun} · see source` }));
  return box;
}

/* ------------------------------------------------------------------------- *
 * One task
 * ------------------------------------------------------------------------- */

function dependencyChip(dep) {
  const target = beadId(dep.depends_on_id) || beadId(dep.id);
  const kind = clean(dep.dependency_type || dep.type);
  const title = clean(dep.title);
  const label = [kind, target || clean(dep.depends_on_id || dep.id)].filter(Boolean).join(" ");
  const chip = h("span.beadCard__dep", {}, title ? h("span.beadCard__depTitle", { text: shorten(title, 60) }) : null);
  if (!target) return chip;
  chip.append(trackerLink(target, label || target, "beadCard__depLink"));
  return chip;
}

function dependencyList(issue) {
  if (!Array.isArray(issue.dependencies) || !issue.dependencies.length) return null;
  const deps = issue.dependencies.filter(isObject);
  if (!deps.length) return null;
  const shown = deps.slice(0, MAX_DEPS).map(dependencyChip);
  return h("div.beadCard__deps", {},
    h("span.beadCard__section", { text: `Dependencies (${deps.length})` }),
    h("div.beadCard__depRows", {}, shown),
    deps.length > MAX_DEPS ? h("small.beadCard__notice", { text: `${deps.length - MAX_DEPS} more · see source` }) : null);
}

/** A task's card: state, id, title, what the tracker recorded. */
function issueCard(issue, { body = false, notes = false, deps = false } = {}) {
  const id = beadId(issue.id);
  const title = shorten(issue.title, 160) || id || "Untitled task";
  const description = typeof issue.description === "string" ? issue.description.trim() : "";
  const acceptanceText = typeof issue.acceptance_criteria === "string" ? issue.acceptance_criteria.trim() : "";
  const notesText = typeof issue.notes === "string" ? issue.notes.trim() : "";
  const closeReason = clean(issue.close_reason);
  const facts = [
    fact("created", dateText(issue.created_at)),
    fact("updated", dateText(issue.updated_at)),
    fact("closed", dateText(issue.closed_at)),
  ].filter(Boolean);
  return h("article.beadCard__issue", { dataset: { status: clean(issue.status) } },
    h("div.beadCard__head", {},
      stateChip(issue.status),
      idView(issue.id),
      priorityTag(issue.priority),
      typeTag(issue.issue_type),
      clean(issue.assignee) ? h("span.beadCard__tag.beadCard__tag--who", { text: `@${shorten(issue.assignee, 40)}` }) : null),
    h("h5.beadCard__title", { text: title }),
    closeReason ? h("p.beadCard__text.beadCard__text--muted", { text: `Closed · ${shorten(closeReason, 300)}` }) : null,
    body && description ? h("p.beadCard__text", { text: shorten(description, 400) }) : null,
    body && description.length > 400
      ? h("details.beadCard__details", {}, h("summary", { text: "Full description" }),
        h("p.beadCard__text", { text: description.slice(0, MAX_BODY) }))
      : null,
    acceptanceText
      ? h("details.beadCard__details", {}, h("summary", { text: "Acceptance criteria" }),
        h("p.beadCard__text", { text: acceptanceText.slice(0, MAX_BODY) }))
      : null,
    notesText
      ? h("details.beadCard__details", {}, h("summary", { text: "Notes" }),
        h("p.beadCard__text", { text: notesText.slice(0, MAX_BODY) }))
      : null,
    facts.length ? h("div.beadCard__facts", {}, facts) : null,
    deps ? dependencyList(issue) : null,
    id ? h("div.beadCard__foot", {}, trackerLink(id, "Open in tracker", "beadCard__open")) : null);
}

/** A single issue from a bd answer, which may be an issue or `[issue]`. */
function issueOf(data) {
  if (isObject(data) && (data.id != null || data.title != null)) return data;
  if (Array.isArray(data) && data.length === 1 && isObject(data[0])) return data[0];
  return null;
}

/* ------------------------------------------------------------------------- *
 * Lists
 * ------------------------------------------------------------------------- */

function countStatuses(items) {
  const counts = new Map();
  for (const item of items) {
    const status = clean(item.status) || "unknown";
    counts.set(status, (counts.get(status) ?? 0) + 1);
  }
  return [...counts.entries()];
}

function countLine(items, { onPage = false } = {}) {
  const counts = countStatuses(items);
  return counts.length
    ? h("span.beadCard__counts", {},
      onPage ? h("span.beadCard__count", { text: "on this page:" }) : null,
      counts.map(([status, count]) =>
        h("span.beadCard__count", { dataset: { status }, text: `${count} ${stateOf(status)?.label.toLowerCase() ?? status}` })))
    : null;
}

function taskRow(issue) {
  const id = beadId(issue.id);
  const title = shorten(issue.title, 120) || id || "Untitled task";
  const deps = Number(issue.dependency_count);
  const state = stateOf(issue.status);
  const meta = [
    id,
    Number.isFinite(Number(issue.priority)) && issue.priority !== null ? `P${Number(issue.priority)}` : "",
    clean(issue.issue_type),
    Number.isFinite(deps) && deps > 0 ? plural(deps, "dependency", "dependencies") : "",
  ].filter(Boolean).join(" · ");
  const inner = [
    h("span.beadCard__mark", { text: state?.mark ?? "·", title: state?.label ?? clean(issue.status), "aria-hidden": "true" }),
    h("span.beadCard__rowMain", {},
      h("span.beadCard__title", { text: title }),
      h("span.beadCard__meta", { text: meta })),
    // The mark is decoration; the state is read out here.
    h("span.beadCard__sr", { text: state?.label ?? clean(issue.status) }),
  ];
  return h("li.beadCard__row", { dataset: { status: clean(issue.status) } },
    id ? h("a.beadCard__rowLink", { href: `/tasks?task=${encodeURIComponent(id)}`, title: `Open ${id} in the tracker` }, inner)
      : h("div.beadCard__rowLink", {}, inner));
}

function listView(data) {
  if (!isObject(data) || !Array.isArray(data.items)) return null;
  const items = data.items.filter(isObject);
  const count = Number.isFinite(Number(data.total)) ? Number(data.total) : null;
  const summary = [];
  if (Number.isFinite(count)) summary.push(plural(count, "task"));
  else summary.push(plural(items.length, "task"));
  const currentPage = Number.isFinite(Number(data.page)) ? Number(data.page) : null;
  const totalPages = Number.isFinite(Number(data.total_pages)) ? Number(data.total_pages) : null;
  if (Number.isFinite(currentPage) && Number.isFinite(totalPages) && totalPages > 1) summary.push(`page ${currentPage} of ${totalPages}`);
  if (!items.length) {
    return h("div.beadCard", {}, note("empty", h("span", { text: count === 0 || count == null ? "No tasks match." : "Nothing on this page." })));
  }
  return h("div.beadCard", {},
    note("info", h("span", { text: summary.join(" · ") }), countLine(items, { onPage: count > items.length })),
    boundedRows(items, taskRow));
}

function readyView(data) {
  if (!Array.isArray(data)) return null;
  const items = data.filter(isObject);
  if (!items.length) return h("div.beadCard", {}, note("empty", h("span", { text: "No tasks ready to start." })));
  return h("div.beadCard", {},
    note("info", h("strong", { text: "Ready to start" }), h("span", { text: ` · ${plural(items.length, "task")}` })),
    boundedRows(items, taskRow));
}

/* ------------------------------------------------------------------------- *
 * Writes: create, update, close, dependency
 * ------------------------------------------------------------------------- */

function actionCard(issue, verb, extra) {
  return h("div.beadCard", {},
    note("ok", h("strong", { text: verb }), extra ? h("span", { text: ` · ${extra}` }) : null),
    issueCard(issue, { body: true, deps: true }));
}

/** What a call asked to change, before its result says what changed. */
function changeLine(tool, args) {
  if (!isObject(args)) return null;
  const id = beadId(args.bead_id || args.id);
  const changes = [];
  if (tool === "beads_update") {
    if (clean(args.status)) changes.push(`status → ${clean(args.status)}`);
    if (args.claim === true) changes.push("claim it");
    if (clean(args.title)) changes.push("new title");
    if (clean(args.description)) changes.push("description replaced");
    if (clean(args.notes)) changes.push("notes added");
    if (!changes.length) return null;
    return h("div.beadCard", {}, note("info",
      h("strong", { text: "Update" }), h("span", { text: ` ${id || "task"}: ${changes.join(" · ")}` })));
  }
  if (tool === "beads_close") {
    if (!id) return null;
    return h("div.beadCard", {}, note("info",
      h("strong", { text: "Close" }), h("span", { text: ` ${id}${clean(args.reason) ? ` · ${shorten(args.reason, 160)}` : ""}` })));
  }
  if (tool === "beads_create") {
    if (!clean(args.title)) return null;
    return h("div.beadCard", {}, note("info",
      h("strong", { text: "New task" }),
      h("span", { text: ` · ${shorten(args.title, 160)}` }),
      typeTag(args.type),
      priorityTag(args.priority)));
  }
  if (tool === "beads_dep") {
    const child = beadId(args.child_id);
    const parent = beadId(args.parent_id);
    if (!child || !parent) return null;
    return h("div.beadCard", {}, note("info",
      h("strong", { text: args.action === "remove" ? "Unlink" : "Link" }),
      h("span", { text: " " }, idView(child, "beadCard__id"), h("span", { text: args.action === "remove" ? " no longer depends on " : " depends on " }), idView(parent, "beadCard__id"))));
  }
  return null;
}

/** `✓ Added dependency: child (T) depends on parent (T) (blocks)` as a line. */
const DEP_TEXT = /^✓\s+(Added|Removed)\s+dependency:\s+(.+?)\s+\(([^)]*)\)\s+(depends on|blocks)\s+(.+?)\s+\(([^)]*)\)(?:\s+\(([^)]*)\))?\s*$/;

function dependencyText(text) {
  const match = DEP_TEXT.exec(text.trim());
  if (!match) return null;
  const [, action, child, , verb, parent, , kind] = match;
  const childId = beadId(child);
  const parentId = beadId(parent);
  if (!childId || !parentId) return null;
  return h("div.beadCard", {}, note("ok",
    h("strong", { text: action }),
    h("span", { text: " " }, idView(childId, "beadCard__id"),
      h("span", { text: ` ${verb} ` }), idView(parentId, "beadCard__id"),
      kind ? h("span", { text: ` (${kind})` }) : null)));
}

/* ------------------------------------------------------------------------- *
 * Plans
 * ------------------------------------------------------------------------- */

const keyChip = (key) => (clean(key) ? h("span.beadCard__key.beadCard__tag", { text: clean(key) }) : null);

function planTaskRow(task, waiting) {
  const id = beadId(task.id);
  const waits = Array.isArray(task.waits_for) ? task.waits_for.filter((key) => typeof key === "string" && key) : [];
  return h("li.beadCard__row.beadCard__row--plan", { dataset: { state: waiting ? "waiting" : "ready" } },
    h("span.beadCard__rowMain", {},
      h("span.beadCard__rowTop", {},
        h("span.beadCard__mark", { text: waiting ? "◌" : "○", title: waiting ? "Waiting" : "Ready", "aria-hidden": "true" }),
        h("span.beadCard__title", { text: shorten(task.title, 140) || id || clean(task.key) }),
        keyChip(task.key)),
      h("span.beadCard__meta", {},
        id ? trackerLink(id, id, "beadCard__id") : h("span", { text: clean(task.id) }),
        waits.length ? h("span", { text: ` · waits for ${waits.join(", ")}`, title: waits.join(", ") }) : null)));
}

function planView(data, role) {
  if (!isObject(data)) return null;
  if (role === "input") {
    if (!Array.isArray(data.tasks)) return null;
    const tasks = data.tasks.filter(isObject);
    if (!tasks.length) return null;
    const links = tasks.reduce((sum, task) => sum + (Array.isArray(task.depends_on) ? task.depends_on.length : 0), 0);
    return h("div.beadCard", {},
      note("info", h("strong", { text: `Plan of ${plural(tasks.length, "task")}` }), h("span", { text: ` · ${plural(links, "dependency link")}` })),
      boundedRows(tasks, (task) => h("li.beadCard__row.beadCard__row--plan", {},
        h("span.beadCard__rowMain", {},
          h("span.beadCard__rowTop", {},
            h("span.beadCard__title", { text: shorten(task.title, 140) || clean(task.key) }),
            keyChip(task.key)),
          Array.isArray(task.depends_on) && task.depends_on.length
            ? h("span.beadCard__meta", { text: `depends on ${task.depends_on.join(", ")}` })
            : h("span.beadCard__meta", { text: "no dependencies" })))));
  }
  // An error is never a plan: a failed call returns `{error}` and says so.
  if (typeof data.error === "string" && data.error) return null;
  const ready = Array.isArray(data.ready) ? data.ready.filter(isObject) : [];
  const waiting = Array.isArray(data.waiting) ? data.waiting.filter(isObject) : [];
  const ids = isObject(data.ids) ? Object.entries(data.ids).filter(([, id]) => beadId(id)).slice(0, MAX_ROWS) : [];
  if (!ready.length && !waiting.length && !ids.length) return null;
  const total = ready.length + waiting.length;
  const head = note("info",
    h("strong", { text: "Plan" }),
    h("span", { text: ` · ${plural(total, "task")} · ${ready.length} ready, ${waiting.length} waiting` }));
  const cards = [
    ready.length ? h("div.beadCard__wave", {}, h("div.beadCard__section", { text: "Ready now" }), boundedRows(ready, (task) => planTaskRow(task, false))) : null,
    waiting.length ? h("div.beadCard__wave", {}, h("div.beadCard__section", { text: "Waiting" }), boundedRows(waiting, (task) => planTaskRow(task, true))) : null,
    !ready.length && !waiting.length ? note("empty", h("span", { text: "The plan was created; no waves were reported." })) : null,
  ];
  return h("div.beadCard", {}, head, ...cards);
}

/* ------------------------------------------------------------------------- *
 * Logs
 * ------------------------------------------------------------------------- */

const channelText = (channel) => (clean(channel) ? `#${clean(channel)}` : "the log");

function clockText(ts) {
  if (typeof ts !== "number" || !Number.isFinite(ts)) return "";
  const date = new Date(ts * 1000);
  if (Number.isNaN(date.getTime())) return "";
  return `${String(date.getUTCHours()).padStart(2, "0")}:${String(date.getUTCMinutes()).padStart(2, "0")}`;
}

/** JSONL lines as chat lines; a line this code cannot read stays its own text. */
function logLines(newData) {
  return String(newData ?? "").split("\n").map((line) => {
    const text = line.trim();
    if (!text) return null;
    try {
      const entry = JSON.parse(text);
      if (isObject(entry) && typeof entry.text === "string") {
        return { author: clean(entry.author) || "unknown", text: entry.text, ts: entry.ts };
      }
    } catch { /* Keep unreadable lines visible as text. */ }
    return { raw: line };
  }).filter(Boolean);
}

function logLine(entry) {
  if (entry.raw != null) {
    return h("li.beadLog__line.beadLog__line--raw", {}, h("span.beadLog__text", { text: entry.raw }));
  }
  const clock = clockText(entry.ts);
  return h("li.beadLog__line", {},
    h("span.beadLog__author", { text: shorten(entry.author, 40) }),
    clock ? h("time.beadLog__time", { text: clock, title: new Date(entry.ts * 1000).toISOString() }) : null,
    h("span.beadLog__text", { text: String(entry.text).slice(0, MAX_BODY) }));
}

function logView(data, role) {
  if (!isObject(data)) return null;
  if (role === "input") {
    if (!clean(data.channel)) return null;
    if (data.text !== undefined) {
      return h("div.beadCard", {}, note("info",
        h("strong", { text: "Append" }), h("span", { text: ` to ${channelText(data.channel)}` }),
        clean(data.author) ? h("span", { text: ` as ${shorten(data.author, 40)}` }) : null,
        clean(data.text) ? h("span.beadCard__text.beadCard__text--muted", { text: shorten(data.text, 200) }) : null));
    }
    if (data.reset_to_end) return h("div.beadCard", {}, note("info", h("strong", { text: "Mark as read" }), h("span", { text: ` ${channelText(data.channel)}` })));
    if (data.from_start) return h("div.beadCard", {}, note("info", h("strong", { text: "Read from the start" }), h("span", { text: ` ${channelText(data.channel)}${clean(data.reader) ? ` as ${clean(data.reader)}` : ""}` })));
    return h("div.beadCard", {}, note("info",
      h("strong", { text: "Read" }), h("span", { text: ` ${channelText(data.channel)}` }),
      clean(data.reader) ? h("span", { text: ` as ${shorten(data.reader, 40)}` }) : null));
  }
  const channel = channelText(data.channel);
  if (data.log_exists === false) {
    return h("div.beadCard", {}, note("empty", h("strong", { text: channel }), h("span", { text: " has no log yet." })));
  }
  if (data.reset_to_end === true) {
    return h("div.beadCard", {}, note("ok", h("strong", { text: "Marked as read" }), h("span", { text: ` · ${channel}${data.cursor_after != null ? ` · cursor ${data.cursor_after}` : ""}` })));
  }
  if (data.new_data === undefined) {
    if (data.bytes_appended != null) {
      return h("div.beadCard", {}, note("ok", h("strong", { text: "Appended" }),
        h("span", { text: ` to ${channel} · ${plural(Number(data.bytes_appended) || 0, "byte")}` })));
    }
    return null;
  }
  const lines = logLines(data.new_data);
  if (!lines.length) {
    return h("div.beadCard", {}, note("empty",
      h("span", { text: `No new entries in ${channel}${clean(data.reader) ? ` for ${clean(data.reader)}` : ""}.` })));
  }
  const shown = lines.slice(0, MAX_LOG_LINES);
  const visible = list("beadLog", shown.slice(0, LOG_FIRST).map(logLine));
  const rest = shown.slice(LOG_FIRST).map(logLine);
  if (rest.length) {
    let at = 0;
    const more = h("button.beadCard__more", { type: "button" });
    const label = () => { more.textContent = `Show ${Math.min(LOG_CHUNK, rest.length - at)} more lines`; };
    label();
    more.addEventListener("click", () => {
      visible.append(...rest.slice(at, at + LOG_CHUNK));
      at += LOG_CHUNK;
      if (at >= rest.length) more.remove();
      else { label(); visible.append(more); }
    });
    visible.append(more);
  }
  return h("div.beadCard", {},
    note("info",
      h("strong", { text: channel }),
      h("span", { text: ` · ${lines.length} new ${lines.length === 1 ? "entry" : "entries"}` }),
      clean(data.reader) ? h("span", { text: ` · for ${shorten(data.reader, 40)}` }) : null),
    visible,
    lines.length > MAX_LOG_LINES ? h("small.beadCard__notice", { text: `${lines.length - MAX_LOG_LINES} more entries · see source` }) : null,
    data.truncated ? h("small.beadCard__notice", { text: "The log has more bytes than this read; read again for the rest." }) : null);
}

/* ------------------------------------------------------------------------- *
 * Confirmations and errors
 * ------------------------------------------------------------------------- */

/** `✅ …` / `❌ …`: a tracker confirmation or a refusal, as its own line. */
function statusText(text, tool) {
  const value = String(text ?? "").trim();
  if (value.startsWith("❌")) {
    const message = value.replace(/^❌\s*(?:Error\b:?\s*)?/i, "");
    return h("div.beadCard", {},
      note("error", h("strong", { text: "Beads error" }), h("span", { text: ` — ${shorten(message, MAX_MESSAGE)}` })),
      clean(message).length > MAX_MESSAGE
        ? h("details.beadCard__details", {}, h("summary", { text: "Full message" }), h("p.beadCard__text", { text: message.slice(0, MAX_BODY) }))
        : null);
  }
  if (value.startsWith("✅")) {
    const message = value.replace(/^✅\s*/, "");
    const already = /already/i.test(message);
    if (tool === "beads_init") {
      return h("div.beadCard", {}, note("ok",
        h("strong", { text: already ? "Tracker already initialized" : "Tracker initialized" }),
        h("span", { text: message && !already ? ` · ${shorten(message, MAX_MESSAGE)}` : " · in this directory" })));
    }
    return h("div.beadCard", {}, note("ok", h("strong", { text: "Done" }), h("span", { text: message ? ` · ${shorten(message, MAX_MESSAGE)}` : "" })));
  }
  if (tool === "beads_sync") {
    return h("div.beadCard", {},
      note("ok", h("strong", { text: "Tracker synced" })),
      value ? h("details.beadCard__details", {}, h("summary", { text: "Sync output" }), h("pre.beadCard__pre", { text: value.slice(0, MAX_BODY) })) : null);
  }
  return null;
}

/** The typed error object shared by beads_plan and the log tools. */
function errorView(data) {
  if (!isObject(data)) return null;
  const message = typeof data.error === "string" ? data.error : "";
  if (!message) return null;
  return h("div.beadCard", {},
    note("error", h("strong", { text: "Beads error" }), h("span", { text: ` — ${shorten(message, MAX_MESSAGE)}` })),
    clean(message).length > MAX_MESSAGE
      ? h("details.beadCard__details", {}, h("summary", { text: "Full message" }), h("p.beadCard__text", { text: message.slice(0, MAX_BODY) }))
      : null);
}

/* ------------------------------------------------------------------------- *
 * Entry points, called by tool-payload.js
 * ------------------------------------------------------------------------- */

/** A tracker view for a typed JSON part, or null to keep the generic one. */
export function beadJson(data, tool, role = "result") {
  const key = beadTool(tool);
  if (!key) return null;
  try {
    if (isObject(data) && (typeof data.error === "string" || data.success === false)) {
      return errorView(data);
    }
    switch (key) {
      case "beads_list": return listView(data);
      case "beads_ready": return readyView(data);
      case "beads_show": {
        const issue = issueOf(data);
        return issue ? h("div.beadCard", {}, issueCard(issue, { body: true, notes: true, deps: true })) : null;
      }
      case "beads_create": {
        if (role === "input") return changeLine(key, data);
        const issue = issueOf(data);
        return issue ? actionCard(issue, "Created") : null;
      }
      case "beads_update": {
        const issue = role === "input" ? null : issueOf(data);
        if (issue) return actionCard(issue, "Updated", clean(issue.status) ? stateOf(issue.status)?.label ?? clean(issue.status) : "");
        return role === "input" ? changeLine(key, data) : null;
      }
      case "beads_close": {
        const issue = role === "input" ? null : issueOf(data);
        return issue ? actionCard(issue, "Closed") : (role === "input" ? changeLine(key, data) : null);
      }
      case "beads_dep": return role === "input" ? changeLine(key, data) : null;
      case "beads_plan": return planView(data, role);
      case "beads_log_append":
      case "beads_log_read": return logView(data, role);
      default: return null;
    }
  } catch { return null; }
}

/** A tracker view for a plain-text part (confirmations, bd failures, dep
 * lines), or null to keep the prepared text block. */
export function beadText(text, tool, role = "result") {
  const key = beadTool(tool);
  if (!key || typeof text !== "string" || !text.trim()) return null;
  try {
    if (key === "beads_dep" && role !== "input") {
      const dep = dependencyText(text);
      if (dep) return dep;
    }
    if (key === "beads_create" && role === "input") {
      try {
        return changeLine(key, JSON.parse(text));
      } catch { /* not parameters: fall through */ }
    }
    if (key === "beads_update" || key === "beads_close") {
      if (role === "input") {
        try {
          return changeLine(key, JSON.parse(text));
        } catch { /* not parameters: fall through */ }
      }
    }
    if (key === "beads_plan") {
      if (role === "input") {
        try {
          return planView(JSON.parse(text), role);
        } catch { /* not a plan: fall through */ }
      }
      try {
        const parsed = JSON.parse(text);
        if (isObject(parsed)) return planView(parsed, role) ?? errorView(parsed);
      } catch { /* not JSON: fall through to text handling */ }
    }
    return statusText(text, key);
  } catch { return null; }
}
