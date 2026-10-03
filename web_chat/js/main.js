/**
 * Application entry point: resolve the DOM, build the store, wire the parts.
 *
 * Every module below owns one concern and receives only the nodes and
 * callbacks it needs. This file is the single place where those wires are
 * visible, which is the point - the dependency graph should be readable in one
 * screen rather than inferred from imports scattered across the app.
 */

import { $, $$, h, icon } from "./lib/dom.js";
import { createStore } from "./lib/store.js";
import { api } from "./net/api.js";
import { tokens } from "./lib/format.js";
import { ChatController } from "./chat.js";
import { AccountsDrawer } from "./accounts/admin.js";
import { monitorServer } from "./net/server-monitor.js";
import { PasswordDrawer } from "./accounts/password.js";
import { PlanDrawer } from "./accounts/plan.js";
import { PersonalAgentsDrawer } from "./agents/drawer.js";
import { SettingsDrawer } from "./settings/drawer.js";
import { createRoutePicker } from "./ui/route-picker.js";
import { createComposer } from "./ui/composer.js";
import { createQueueTray } from "./ui/queue.js";
import { createConversationList } from "./ui/sidebar.js";
import { SpeechPlayer, synthesizeSentence } from "./ui/speech.js";
import { ICONS } from "./ui/icons.js";
import { createThemeToggle } from "./ui/theme.js";
import { copyText, toast } from "./ui/toast.js";
import { createTranscript } from "./ui/transcript.js";
import { openReportDialog } from "./ui/report-dialog.js";
import { createWelcomeSystems } from "./ui/welcome.js";

const store = createStore({
  systems: [],
  /** `null` on either key means "let the router decide, per message". */
  systemKey: null,
  agentKey: null,
  defaultSystem: null,
  multiSystem: false,
  routingEnabled: false,
  conversations: [],
  contextId: null,
  // The conversation the open branch belongs to: the rail's row.
  rootId: null,
  streaming: false,
  compacting: false,
  hasAgentContext: false,
  // The first Stop was sent; the agent is finishing its step.
  stopping: false,
  // The conversation ends with a turn that stopped early and can be continued.
  resumable: false,
  // Messages waiting for the agent (web_chat/delivery.py).
  queue: [],
  workspacePath: "",
  isolated: false,
  // The signed-in user ({id, username, role}); `accounts` is false on a one-user server.
  user: null,
  accounts: false,
  // The server lets this user build agents of their own.
  personalAgents: false,
  // The server offers voice (voice.enabled); off, no voice control is shown.
  voice: false,
  reviews: false,
  uploads: { enabled: false, max_file_mb: 25, max_files: 10 },
});

/** Icon-only buttons declare their glyph in markup; fill them in one pass. */
function paintIcons(scope = document) {
  for (const node of $$("[data-icon]", scope)) {
    node.prepend(icon(ICONS[node.dataset.icon] ?? ICONS.chevron, { size: Number(node.dataset.iconSize) || 16 }));
    delete node.dataset.icon;
  }
}

async function refreshConversations() {
  try {
    store.set({ conversations: await api.listConversations() });
  } catch (error) {
    console.warn("Could not list conversations", error);
  }
}

/**
 * Apply a selection and, when it names a concrete agent, warm it server-side so
 * the first message does not pay for model and tool startup. An `auto`
 * selection has nothing to warm - the agent is not known until a message lands.
 */
async function selectRoute({ systemKey = null, agentKey = null }) {
  store.set({ systemKey, agentKey });
  if (!agentKey) return;
  try {
    await api.prepareAgent(systemKey, agentKey);
  } catch (error) {
    toast(`Could not prepare ${agentKey}: ${error.message}`, { tone: "error" });
  }
}

function bindShortcuts({ composer, search }) {
  document.addEventListener("keydown", (event) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName ?? "");
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      search.focus();
      return;
    }
    if (event.key === "/" && !typing) {
      event.preventDefault();
      composer.focus();
    }
  });
}

async function boot() {
  paintIcons();

  const speech = new SpeechPlayer({
    synthesize: synthesizeSentence,
    onState: (id, state, error) => {
      transcript.setSpeechState(id, state);
      if (state === "error") toast(error?.message ?? "Could not play the answer", { tone: "error" });
    },
  });
  // The voice loop pauses and resumes playback for barge-in; it must talk to
  // the same player the messages do, so there is only ever one voice speaking.
  globalThis.GridSpeech = speech;

  const transcript = createTranscript({
    container: $("#chat-scroll"),
    emptyState: $("#welcome"),
    jumpButton: $("#jump-latest"),
    onEditMessage: (edited) => void chat.editMessage(edited),
    onSwitchVersion: (contextId) => void chat.switchVersion(contextId),
    onSpeakMessage: (id) => chat.toggleSpeech(id),
    onReportMessage: ({ id }) => {
      const contextId = store.get().contextId;
      openReportDialog({
        send: async (note) => {
          await api.reportAnswer(contextId, id, note);
          toast("Report sent. Thank you!", { tone: "success" });
        },
      });
    },
    onContinue: () => chat.continueTurn(),
  });

  const chat = new ChatController({
    store,
    transcript,
    onConversationsChanged: refreshConversations,
    speech,
    voice: globalThis.VoiceUI,
  });

  const composer = createComposer({
    input: $("#composer-input"),
    sendButton: $("#send"),
    stopButton: $("#stop"),
    deliverySelect: $("#delivery"),
    continueButton: $("#continue"),
    attachButton: $("#attach"),
    fileInput: $("#composer-files"),
    tray: $("#composer-tray"),
    store,
    onSend: (text, images, delivery) => chat.send(text, { images, delivery }),
    onStop: () => chat.stop(),
    onContinue: () => chat.continueTurn(),
  });

  createQueueTray({
    container: $("#queue-tray"),
    store,
    onSendNow: (item) => void chat.sendQueued(item),
    onDrop: (item) => void chat.dropQueued(item),
  });

  createConversationList({
    container: $("#chat-list"),
    searchInput: $("#chat-search"),
    store,
    onSelect: async (contextId) => {
      closeRail();
      await chat.openConversation(contextId);
    },
    onRename: async (contextId, title) => {
      try {
        await chat.renameConversation(contextId, title);
      } catch (error) {
        toast(error.message, { tone: "error" });
      }
    },
    onDelete: async (contextId) => {
      try {
        await chat.deleteConversation(contextId);
      } catch (error) {
        toast(error.message, { tone: "error" });
      }
    },
  });

  // Agents keep working in chats that are not on screen; poll while any is
  // running so their spinners stop when they finish.
  setInterval(() => {
    if (store.get().conversations.some((conversation) => conversation.active)) void refreshConversations();
  }, 4000);

  createRoutePicker({
    button: $("#route-chip"),
    nameNode: $("#route-name"),
    metaNode: $("#route-meta"),
    popover: $("#route-popover"),
    systemList: $("#system-list"),
    agentList: $("#agent-list"),
    agentHeading: $("#agent-heading"),
    store,
    onChange: selectRoute,
  });
  createWelcomeSystems({ container: $("#welcome-systems"), store, onChange: selectRoute });

  createThemeToggle($("#theme-toggle"));

  const settings = new SettingsDrawer(
    {
      drawer: $("#settings"),
      backdrop: $("#settings-backdrop"),
      closeButton: $("#settings-close"),
      saveButton: $("#settings-save"),
      reloadButton: $("#settings-reload"),
      status: $("#settings-status"),
      title: $("#settings-title"),
      subtitle: $("#settings-subtitle"),
      files: $("#settings-files"),
      tabs: $("#settings-tabs"),
      body: $("#settings-body"),
    },
    { onSaved: reloadRuntime },
  );

  const personalAgents = new PersonalAgentsDrawer(
    {
      drawer: $("#agents"),
      backdrop: $("#agents-backdrop"),
      closeButton: $("#agents-close"),
      newButton: $("#agents-new"),
      list: $("#agents-list"),
      editor: $("#agents-editor"),
      status: $("#agents-status"),
      saveButton: $("#agents-save"),
      deleteButton: $("#agents-delete"),
    },
    { onChanged: reloadRuntime },
  );

  const accountsAdmin = new AccountsDrawer(
    {
      drawer: $("#admin"),
      backdrop: $("#admin-backdrop"),
      closeButton: $("#admin-close"),
      refreshButton: $("#admin-refresh"),
      restartButton: $("#admin-restart"),
      serverStatus: $("#admin-server-status"),
      reviews: $("#admin-reviews"),
      users: $("#admin-users"),
      invites: $("#admin-invites"),
      inviteForm: $("#admin-invite-form"),
      created: $("#admin-invite-created"),
      status: $("#admin-status"),
    },
    { currentUser: () => store.get().user },
  );
  const password = new PasswordDrawer({
    drawer: $("#password"),
    backdrop: $("#password-backdrop"),
    closeButton: $("#password-close"),
    form: $("#password-form"),
    status: $("#password-status"),
  });

  const plan = new PlanDrawer({
    drawer: $("#plan"),
    backdrop: $("#plan-backdrop"),
    closeButton: $("#plan-close"),
    body: $("#plan-body"),
    status: $("#plan-status"),
    openButton: $("#open-plan"),
  });
  plan.start();

  // -- chrome ------------------------------------------------------------
  const openRail = () => document.body.classList.add("rail-open");
  const closeRail = () => document.body.classList.remove("rail-open");
  $("#toggle-rail").addEventListener("click", openRail);
  $("#sidebar-scrim").addEventListener("click", closeRail);
  $("#open-settings").addEventListener("click", () => settings.open());
  $("#compact-context").addEventListener("click", async () => {
    try {
      const result = await chat.compactContext();
      const count = (value) => Number(value).toLocaleString();
      toast(`Context compacted: ≈${count(result.tokens_before)} → ${count(result.tokens_after)} tokens.`, { tone: "success", timeout: 6000 });
    } catch (error) {
      toast(error.message, { tone: "error" });
    }
  });
  $("#open-usage").addEventListener("click", (event) => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const usageWindow = window.open("/usage", "grid-usage", "popup,width=1280,height=900,resizable=yes,scrollbars=yes");
    if (usageWindow) {
      usageWindow.opener = null;
      usageWindow.focus();
      event.preventDefault();
    }
  });
  $("#open-agents").addEventListener("click", () => {
    closeRail();
    personalAgents.open();
  });
  $("#open-admin").addEventListener("click", () => {
    closeRail();
    accountsAdmin.open();
  });
  $("#open-plan").addEventListener("click", () => {
    closeRail();
    plan.open();
  });
  $("#open-password").addEventListener("click", () => {
    closeRail();
    password.open();
  });
  $("#sign-out").addEventListener("click", async () => {
    await api.signOut();
    location.replace("/login");
  });

  for (const button of [$("#new-chat"), $("#new-chat-top")]) {
    button.addEventListener("click", async () => {
      closeRail();
      await chat.startConversation();
      composer.focus();
    });
  }

  for (const chip of $$("[data-prompt]")) {
    chip.addEventListener("click", () => composer.setValue(chip.dataset.prompt));
  }

  $("#workspace-pill").addEventListener("click", () => copyText(store.get().workspacePath, "Workspace path copied"));

  bindShortcuts({ composer, search: $("#chat-search") });

  // -- data --------------------------------------------------------------
  async function reloadRuntime() {
    const bootstrap = await api.bootstrap();
    const single = bootstrap.multi_system ? null : bootstrap.default_system;
    store.set({
      systems: bootstrap.systems ?? [],
      defaultSystem: bootstrap.default_system ?? null,
      multiSystem: Boolean(bootstrap.multi_system),
      routingEnabled: Boolean(bootstrap.routing_enabled),
      // With a single system there is nothing to route between, so pin it and
      // leave only the agent on `auto`.
      systemKey: store.get().systemKey ?? single,
      workspacePath: bootstrap.workspace_path ?? "",
      isolated: Boolean(bootstrap.isolation_enabled),
      contextId: store.get().contextId ?? bootstrap.current_context_id ?? null,
      user: bootstrap.user ?? null,
      accounts: Boolean(bootstrap.accounts),
      personalAgents: Boolean(bootstrap.personal_agents),
      systemsPage: Boolean(bootstrap.systems_page),
      voice: Boolean(bootstrap.voice),
      reviews: Boolean(bootstrap.reviews),
      uploads: bootstrap.uploads ?? { enabled: false, max_file_mb: 25, max_files: 10 },
    });
    await refreshConversations();
  }

  // Who is signed in, on a server with accounts; the system configs are
  // shared by all users, so only admins get to open them.
  store.subscribe(({ user, accounts, personalAgents: ownAgents, reviews, systemsPage }) => {
    $("#account").hidden = !accounts;
    $("#account-name").textContent = user?.username ?? "";
    $("#open-settings").hidden = user?.role !== "admin";
    $("#open-admin").hidden = !accounts || user?.role !== "admin";
    $("#open-agents").hidden = !ownAgents;
    $("#open-reviews").hidden = !reviews || user?.role !== "admin";
    $("#open-systems").hidden = !systemsPage;
    // The timeline is the operator's local tool (grid-timeline), not the site's.
    $("#open-timeline").hidden = accounts;
  });

  // The user's own use of this server - today's turns and tokens under the
  // account name, all-time behind them - refreshed as each turn settles.
  const accountUsage = $("#account-usage");
  const accountUsageText = (usage) => {
    const spent = (day) => (day?.tokens_in ?? 0) + (day?.tokens_out ?? 0);
    const today = usage?.today ?? {};
    const total = usage?.total ?? {};
    const line = [`Today: ${today.turns ?? 0} turns · ${tokens(spent(today)) || "0"} tokens`];
    if (spent(total) > 0) line.push(`${tokens(spent(total))} all-time`);
    return line.join(" · ");
  };
  const refreshAccountUsage = async () => {
    if (!store.get().accounts) return;
    try {
      accountUsage.textContent = accountUsageText(await api.usage());
    } catch {
      accountUsage.textContent = ""; // signed out, or the server said no
    }
  };
  document.addEventListener("grid:turn-settled", refreshAccountUsage);
  void refreshAccountUsage();

  store.subscribe(({ workspacePath, isolated, streaming, compacting, hasAgentContext, contextId, restartPending, serverUnavailable }) => {
    const label = serverUnavailable ? "Reconnecting to server…" : restartPending ? "Server restarting · waiting for saved steps" : compacting ? "Compacting context…" : streaming ? "Working" : isolated ? "Container isolated" : "Local runtime";
    $("#runtime-status").textContent = label;
    $("#runtime-status").dataset.state = streaming || compacting ? "busy" : "ready";
    $("#compact-context").disabled = !contextId || !hasAgentContext || streaming || compacting || Boolean(restartPending);
    $("#compact-context").setAttribute("aria-busy", String(compacting));
    $("#compact-context .compactLabel").textContent = compacting ? "Compacting…" : "Compact context";
    $("#workspace-pill").textContent = workspacePath;
    $("#workspace-pill").hidden = !workspacePath;
  });

  await reloadRuntime();
  document.addEventListener("grid:server-status", ({ detail }) => {
    store.set({ restartPending: ["preparing", "restarting"].includes(detail.phase), serverUnavailable: false });
  });
  const stopMonitoring = monitorServer({
    read: () => store.get().user?.role === "admin" ? api.adminServer() : api.serverStatus(),
    onStatus: (detail) => document.dispatchEvent(new CustomEvent("grid:server-status", { detail })),
    onRestart: () => chat.reconnectAfterRestart(),
    onUnavailable: () => store.set({ restartPending: true, serverUnavailable: true }),
  });
  window.addEventListener("pagehide", stopMonitoring, { once: true });
  // "Open in chat" on the systems page: a new chat with that system pinned.
  const params = new URLSearchParams(location.search);
  const pinned = params.get("system");
  const [latest] = store.get().conversations;
  if (pinned && store.get().systems.some((system) => system.key === pinned)) {
    history.replaceState(null, "", "/");
    store.set({ systemKey: pinned, agentKey: null });
    await chat.startConversation();
    // "Describe it to the builder": the request waits in the composer, to read and send.
    if (params.get("prompt")) composer.setValue(params.get("prompt"));
  } else if (latest) {
    await chat.openConversation(latest.id);
  }

  // The report button under answers shows only when the server takes reports.
  document.body.dataset.reviews = store.get().reviews ? "on" : "off";
  // Voice controls and read-aloud buttons exist only when the server offers voice.
  document.body.dataset.voice = store.get().voice ? "on" : "off";
  if (store.get().voice) globalThis.VoiceUI?.setup({
    getContextId: () => store.get().contextId,
    getDraft: () => composer.value,
    setDraft: (value) => composer.setValue(value),
    isStreaming: () => store.get().streaming,
    ensureConversation: () => chat.startConversation(),
    // Dictated turns - and only these - read their answer back automatically.
    sendVoiceMessage: (text) => chat.send(text, { spoken: true }),
    // A spoken "stop" means now, not after the current step.
    interruptAgent: () => chat.stop({ now: true }),
    // Voice control of the whole chat (web_chat/voice_turns.py).
    getVoiceState: () => chat.voiceState(),
    command: (action, argument) => chat.voiceCommand(action, argument, { chooseAgent: selectRoute }),
    confirmationQuestion: (action) =>
      action === "delete_chat"
        ? `Удалить чат «${chat.voiceState().chat_title || "без названия"}»? Скажите «да» или «нет».`
        : "Подтвердите: да или нет?",
    getAssistantText: () => chat.currentAnswer,
    sendMessage: (text) => chat.send(text),
  });

  await selectRoute({ systemKey: store.get().systemKey, agentKey: store.get().agentKey });
  composer.focus();
}

boot().catch((error) => {
  console.error(error);
  document.body.append(
    h("div.bootError", {}, h("strong", { text: "Grid chat failed to start" }), h("span", { text: error.message })),
  );
});
