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
import { ChatController } from "./chat.js";
import { SettingsDrawer } from "./settings/drawer.js";
import { createRoutePicker } from "./ui/route-picker.js";
import { createComposer } from "./ui/composer.js";
import { createConversationList } from "./ui/sidebar.js";
import { SpeechPlayer, synthesizeSentence } from "./ui/speech.js";
import { ICONS } from "./ui/icons.js";
import { createThemeToggle } from "./ui/theme.js";
import { copyText, toast } from "./ui/toast.js";
import { createTranscript } from "./ui/transcript.js";

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
  streaming: false,
  workspacePath: "",
  isolated: false,
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
    onEditMessage: (text) => composer.setValue(text),
    onSpeakMessage: (id) => chat.toggleSpeech(id),
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
    store,
    onSend: (text) => chat.send(text),
    onStop: () => chat.stop(),
  });

  createConversationList({
    container: $("#chat-list"),
    searchInput: $("#chat-search"),
    store,
    onSelect: async (contextId) => {
      closeRail();
      await chat.openConversation(contextId);
    },
  });

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

  createThemeToggle($("#theme-toggle"));

  const settings = new SettingsDrawer(
    {
      drawer: $("#settings"),
      backdrop: $("#settings-backdrop"),
      closeButton: $("#settings-close"),
      saveButton: $("#settings-save"),
      reloadButton: $("#settings-reload"),
      status: $("#settings-status"),
      tabs: $("#settings-tabs"),
      panes: {
        system: $("#pane-system"),
        agents: $("#pane-agents"),
        tools: $("#pane-tools"),
        prompts: $("#pane-prompts"),
        yaml: $("#pane-yaml"),
      },
      systemForm: $("#system-form"),
      runtimeFacts: $("#runtime-facts"),
      agentsList: $("#agents-list"),
      agentEditor: $("#agent-editor"),
      addAgent: $("#add-agent"),
      toolsList: $("#tools-list"),
      toolEditor: $("#tool-editor"),
      addTool: $("#add-tool"),
      promptsList: $("#prompts-list"),
      promptEditor: $("#prompt-editor"),
      addPrompt: $("#add-prompt"),
      yaml: $("#yaml-editor"),
    },
    { onSaved: reloadRuntime },
  );

  // -- chrome ------------------------------------------------------------
  const openRail = () => document.body.classList.add("rail-open");
  const closeRail = () => document.body.classList.remove("rail-open");
  $("#toggle-rail").addEventListener("click", openRail);
  $("#sidebar-scrim").addEventListener("click", closeRail);
  $("#open-settings").addEventListener("click", () => settings.open());

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
    });
    await refreshConversations();
  }

  store.subscribe(({ workspacePath, isolated, streaming }) => {
    const label = streaming ? "Working" : isolated ? "Container isolated" : "Local runtime";
    $("#runtime-status").textContent = label;
    $("#runtime-status").dataset.state = streaming ? "busy" : "ready";
    $("#workspace-pill").textContent = workspacePath;
    $("#workspace-pill").hidden = !workspacePath;
  });

  await reloadRuntime();
  const [latest] = store.get().conversations;
  if (latest) await chat.openConversation(latest.id);

  globalThis.VoiceUI?.setup({
    getContextId: () => store.get().contextId,
    getDraft: () => composer.value,
    setDraft: (value) => composer.setValue(value),
    isStreaming: () => store.get().streaming,
    ensureConversation: () => chat.startConversation(),
    // Dictated turns - and only these - read their answer back automatically.
    sendVoiceMessage: (text) => chat.send(text, { spoken: true }),
    interruptAgent: () => chat.stop(),
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
