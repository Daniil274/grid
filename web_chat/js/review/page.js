/**
 * The admins' review page (/admin/review): the reports users filed, each with
 * the evidence frozen behind the answer (web_chat/review/evidence.py).
 *
 * Evidence is other people's conversations and an agent's raw output, so it
 * is shown as text only - never as markup - whatever it contains.
 */

import { h, replace } from "../lib/dom.js";
import { api } from "../net/api.js";
import { applyStoredTheme } from "../ui/theme.js";
import { toast } from "../ui/toast.js";

applyStoredTheme();

const STATUSES = [
  ["new", "New"],
  ["in_review", "In review"],
  ["proposed", "Proposed"],
  ["closed", "Closed"],
];
const STATUS_LABEL = Object.fromEntries(STATUSES);
const when = (seconds) => new Date(seconds * 1000).toLocaleString();
const json = (value) => JSON.stringify(value, null, 2);

const nodes = {
  filters: document.getElementById("filters"),
  list: document.getElementById("review-list"),
  caseView: document.getElementById("case"),
  pick: document.getElementById("pick"),
  pickUser: document.getElementById("pick-user"),
  pickChats: document.getElementById("pick-chats"),
  pickAnswers: document.getElementById("pick-answers"),
};

const state = { status: "new", selected: null, evolution: false };
/** How often the page asks how a running analysis turn is doing. */
const POLL_MS = 2000;

// -- the list ---------------------------------------------------------------------
function renderFilters() {
  replace(
    nodes.filters,
    [["", "All"], ...STATUSES].map(([value, label]) =>
      h("button.reviewChip", {
        type: "button",
        role: "tab",
        "aria-selected": String(state.status === value),
        text: label,
        on: { click: () => { state.status = value; renderFilters(); void loadList(); } },
      }),
    ),
  );
}

async function loadList() {
  const { reviews, admin_any_chat: anyChat, evolution } = await api.adminReports(state.status || null);
  nodes.pick.hidden = !anyChat;
  state.evolution = evolution;
  replace(
    nodes.list,
    reviews.length
      ? reviews.map((review) =>
          h(
            "button.reviewItem",
            {
              type: "button",
              dataset: { selected: String(review.id === state.selected), status: review.status },
              on: { click: () => void openCase(review.id) },
            },
            h("span.reviewItem__head", {}, h("strong", { text: review.username }), h("span", { text: when(review.created_at) })),
            h("span.reviewItem__route", { text: [review.system, review.agent].filter(Boolean).join(" · ") }),
            h("span.reviewItem__note", { text: review.note || "No note" }),
            h(
              "span.reviewItem__tags",
              {},
              h("span.reviewTag", { text: STATUS_LABEL[review.status] }),
              review.origin === "admin" ? h("span.reviewTag.reviewTag--warn", { text: "opened by an admin" }) : null,
            ),
          ),
        )
      : h("p.reviewList__empty", { text: "No reviews here." }),
  );
}

// -- one review ---------------------------------------------------------------------
async function openCase(id) {
  state.selected = id;
  const { review, evidence } = await api.adminReport(id);
  const tabs = [{ title: "Analysis", render: () => analysisView(review.id) }, ...evidenceTabs(evidence)];
  const panel = h("div.reviewCase__panel");
  const tabBar = h("div.reviewCase__tabs", { role: "tablist" });
  const show = (index) => {
    for (const [i, button] of [...tabBar.children].entries()) button.setAttribute("aria-selected", String(i === index));
    replace(panel, tabs[index].render());
  };
  tabs.forEach((tab, index) =>
    tabBar.append(h("button.reviewChip", { type: "button", role: "tab", text: tab.title, on: { click: () => show(index) } })),
  );

  const status = h(
    "select.reviewCase__status",
    { "aria-label": "Status", on: { change: () => void changeStatus(review.id, status.value) } },
    STATUSES.map(([value, label]) => h("option", { value, text: label, selected: value === review.status })),
  );

  replace(
    nodes.caseView,
    h(
      "header.reviewCase__head",
      {},
      h("div", {}, h("h2", { text: `${review.username} · ${[review.system, review.agent].filter(Boolean).join(" / ")}` }),
        h("p.reviewCase__meta", { text: `${when(review.created_at)} · conversation ${review.context_id}` })),
      status,
    ),
    h("blockquote.reviewCase__note", { text: review.note || "The user left no note." }),
    review.origin === "admin" ? h("p.reviewCase__origin", { text: "Opened by an admin on a chat the user did not report." }) : null,
    tabBar,
    panel,
  );
  show(0);
  void loadList();
}

async function changeStatus(id, status) {
  try {
    await api.setReportStatus(id, status);
    toast(`Marked ${STATUS_LABEL[status].toLowerCase()}`, { tone: "success" });
    await loadList();
  } catch (error) {
    toast(error.message, { tone: "error" });
  }
}

/** The parts of the evidence, each a tab. */
function evidenceTabs(evidence) {
  const { conversation = {}, turn = {}, model_context: model = {}, agent_session: session = {}, config = {}, grid = {} } = evidence;
  return [
    { title: "Conversation", render: () => messagesView(conversation) },
    { title: `Steps (${(turn.steps ?? []).length})`, render: () => stepsView(turn.steps ?? []) },
    { title: `Agent runs (${(turn.executions ?? []).length})`, render: () => runsView(turn.executions ?? []) },
    { title: "Model context", render: () => modelView(model) },
    { title: `SDK session (${(session.items ?? []).length})`, render: () => sessionView(session) },
    { title: "Config", render: () => h("div", {}, codeBlock(json({ grid, ...config }))) },
  ];
}

function messagesView({ messages = [], earlier_messages_left_out: earlier = 0 }) {
  return h(
    "div.evidence",
    {},
    earlier ? h("p.evidence__note", { text: `${earlier} earlier messages left out.` }) : null,
    messages.map((message, index) =>
      h(
        "article.evidenceMsg",
        { dataset: { role: message.role, target: String(index === messages.length - 1) } },
        h("div.evidenceMsg__head", { text: [message.role, message.agent, message.kind, message.timestamp].filter(Boolean).join(" · ") }),
        h("pre.evidence__text", { text: message.content || (message.images ? `[${message.images} image(s)]` : "") }),
      ),
    ),
  );
}

function stepsView(steps) {
  if (!steps.length) return h("p.evidence__note", { text: "No steps were recorded for this answer." });
  return h(
    "div.evidence",
    {},
    steps.map((step) =>
      h(
        "details.evidenceItem",
        { dataset: { tone: step.tone ?? "" } },
        h("summary", { text: [step.kind, step.title, step.subtitle, step.status].filter(Boolean).join(" · ") }),
        step.detail ? h("pre.evidence__text", { text: step.detail }) : null,
        step.body ? h("pre.evidence__text", { text: step.body }) : null,
        step.policy ? codeBlock(json(step.policy)) : null,
      ),
    ),
  );
}

function runsView(runs) {
  if (!runs.length) return h("p.evidence__note", { text: "No agent run was recorded for this turn." });
  return h(
    "div.evidence",
    {},
    runs.map((run) => h("details.evidenceItem", {}, h("summary", { text: `${run.agent_name} · ${(run.tools_used ?? []).join(", ") || "no tools"}` }), codeBlock(json(run)))),
  );
}

function modelView({ assembly, instructions, note }) {
  if (note) return h("p.evidence__note", { text: note });
  return h(
    "div.evidence",
    {},
    instructions ? h("details.evidenceItem", { open: true }, h("summary", { text: "Agent instructions" }), h("pre.evidence__text", { text: instructions })) : null,
    (assembly?.sections ?? []).map((section) =>
      h(
        "details.evidenceItem",
        {},
        h("summary", { text: `${section.key ?? "section"} · ${section.scope ?? ""} · ${section.length ?? 0} chars` }),
        h("pre.evidence__text", { text: section.content ?? section.preview ?? "" }),
      ),
    ),
    assembly ? null : h("p.evidence__note", { text: "No model context was recorded." }),
  );
}

function sessionView({ session_id: id, items = [], earlier_items_left_out: earlier = 0 }) {
  return h(
    "div.evidence",
    {},
    h("p.evidence__note", { text: `${id ?? "No session"}${earlier ? ` · ${earlier} earlier items left out` : ""}` }),
    items.map((item) =>
      h(
        "details.evidenceItem",
        {},
        h("summary", { text: [item?.role, item?.type, item?.name].filter(Boolean).join(" · ") || "item" }),
        codeBlock(json(item)),
      ),
    ),
  );
}

const codeBlock = (text) => h("pre.evidence__code", {}, h("code", { text }));

// -- the review agents' analysis ------------------------------------------------------
/**
 * The conversation with the review agents and the proposals they recorded.
 * While a turn runs the view polls; it stops once another review is opened.
 */
function analysisView(reviewId) {
  const log = h("div.analysis__log");
  const proposals = h("div.analysis__proposals");
  const status = h("p.analysis__status", { role: "status" });
  const input = h("textarea.analysis__input", {
    rows: 3,
    placeholder: "Ask the review agents a follow-up question…",
    "aria-label": "Question for the review agents",
  });
  const send = h("button.btn.btn--primary", { type: "button", text: "Analyze" });

  const render = (analysis) => {
    replace(
      log,
      analysis.messages.length
        ? analysis.messages.map((message) =>
            h(
              "article.analysisMsg",
              { dataset: { role: message.role } },
              h("div.analysisMsg__head", { text: message.role === "user" ? "Admin" : "Review agents" }),
              h("pre.evidence__text", { text: message.content }),
            ),
          )
        : h("p.evidence__note", { text: "Not analyzed yet. Analyze to let the review agents find the cause." }),
    );
    replace(proposals, analysis.proposals.map((proposal) => proposalCard(reviewId, proposal)));
    status.dataset.tone = analysis.error ? "error" : "";
    status.textContent = analysis.running ? "The review agents are working…" : analysis.error ?? "";
    send.disabled = analysis.running;
    send.textContent = analysis.messages.length ? "Ask" : "Analyze";
  };

  const poll = async () => {
    if (state.selected !== reviewId || !log.isConnected) return;
    const analysis = await api.reportAnalysis(reviewId);
    render(analysis);
    if (analysis.running) setTimeout(poll, POLL_MS);
    else void loadList();
  };

  send.addEventListener("click", async () => {
    send.disabled = true;
    try {
      render(await api.analyseReport(reviewId, input.value.trim()));
      input.value = "";
      setTimeout(poll, POLL_MS);
    } catch (error) {
      toast(error.message, { tone: "error" });
      send.disabled = false;
    }
  });

  queueMicrotask(poll);
  return h(
    "div.analysis",
    {},
    log,
    status,
    h("div.analysis__ask", {}, input, send),
    h("h3.analysis__title", { text: "Proposals" }),
    proposals,
  );
}

function proposalCard(reviewId, proposal) {
  return h(
    "article.proposal",
    { dataset: { confidence: proposal.confidence ?? "" } },
    h("header.proposal__head", {},
      h("strong", { text: proposal.title }),
      h("span.reviewTag", { text: proposal.cause }),
      h("span.reviewTag", { text: `confidence: ${proposal.confidence}` })),
    h("p.proposal__summary", { text: proposal.summary }),
    proposal.evidence?.length ? h("p.proposal__evidence", { text: `Evidence: ${proposal.evidence.join(", ")}` }) : null,
    proposal.scenario ? h("details.evidenceItem", {}, h("summary", { text: "How to check it" }), h("pre.evidence__text", { text: proposal.scenario })) : null,
    proposal.change
      ? h("details.evidenceItem", { open: true },
          h("summary", { text: "Change" }),
          codeBlock(proposal.change),
          h("button.btn.btn--ghost.reviewPick__open", { type: "button", text: "Download patch", on: { click: () => download(`${proposal.id}.patch`, proposal.change) } }))
      : null,
    proposalActions(reviewId, proposal),
  );
}

/** Check the patch against this code; send the proposal on, or show what became of it. */
function proposalActions(reviewId, proposal) {
  const result = h("p.proposal__result", { role: "status" });
  const show = (text, tone = "") => {
    result.textContent = text;
    result.dataset.tone = tone;
  };
  const check = h("button.btn.btn--ghost.reviewPick__open", {
    type: "button",
    text: "Check patch",
    disabled: !proposal.change,
    on: {
      click: async () => {
        const { applies, detail } = await api.checkProposal(reviewId, proposal.id);
        show(applies === true ? "Applies to this code." : applies === false ? `Does not apply: ${detail}` : detail, applies === false ? "error" : "");
      },
    },
  });
  const send = h("button.btn.btn--primary.reviewPick__open", {
    type: "button",
    text: "Send to evolution",
    hidden: Boolean(proposal.task) || !state.evolution,
    on: {
      click: async () => {
        send.disabled = true;
        try {
          const task = await api.evolveProposal(reviewId, proposal.id);
          send.hidden = true;
          show(`Sent as task ${task.id}: the workshop will take it.`);
        } catch (error) {
          send.disabled = false;
          show(error.message, "error");
        }
      },
    },
  });
  if (proposal.task) {
    show(`Sent as task ${proposal.task.id}…`);
    api.proposalTask(reviewId, proposal.id)
      .then((task) => show(`Task ${task.id}: ${task.status}${task.experiment ? ` · experiment ${task.experiment}: ${task.verdict}` : ""}`))
      .catch((error) => show(error.message, "error"));
  }
  return h("div.proposal__actions", {}, check, send, result);
}

function download(name, text) {
  const link = h("a", { href: URL.createObjectURL(new Blob([`${text}\n`], { type: "text/x-diff" })), download: name });
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(link.href);
}

// -- an answer the user did not report ------------------------------------------------
async function setUpPicker() {
  const boot = await api.bootstrap();
  const users = boot.accounts ? await api.adminUsers() : [boot.user];
  replace(
    nodes.pickUser,
    h("option", { value: "", text: "Choose a user" }),
    users.map((user) => h("option", { value: user.id, text: user.username })),
  );
  nodes.pickUser.addEventListener("change", () => void pickUser(nodes.pickUser.value));
}

async function pickUser(userId) {
  replace(nodes.pickAnswers);
  if (!userId) return replace(nodes.pickChats);
  const chats = await api.reviewChats(userId);
  replace(
    nodes.pickChats,
    chats.map((chat) =>
      h("button.reviewPick__chat", { type: "button", text: chat.title || chat.id, on: { click: () => void pickChat(userId, chat.id) } }),
    ),
  );
}

async function pickChat(userId, contextId) {
  const answers = await api.reviewAnswers(userId, contextId);
  replace(
    nodes.pickAnswers,
    answers.length
      ? answers.map((answer) =>
          h(
            "div.reviewPick__answer",
            {},
            h("span", { text: `${answer.agent ?? "agent"}: ${answer.preview}` }),
            h("button.btn.btn--ghost.reviewPick__open", {
              type: "button",
              text: "Review",
              on: { click: () => void openUnreported(userId, contextId, answer.id) },
            }),
          ),
        )
      : h("p.reviewList__empty", { text: "No agent answers in this chat." }),
  );
}

async function openUnreported(userId, contextId, messageId) {
  try {
    const review = await api.openReport(userId, contextId, messageId, "");
    state.status = "new";
    renderFilters();
    await openCase(review.id);
  } catch (error) {
    toast(error.message, { tone: "error" });
  }
}

// -- start ---------------------------------------------------------------------------
renderFilters();
await loadList();
if (!nodes.pick.hidden) await setUpPicker();
