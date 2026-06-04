const state = {
  bootstrap: null,
  settings: null,
  rawYaml: "",
  currentContextId: null,
  currentAgentKey: null,
  conversations: [],
  isStreaming: false,
  socket: null,
  currentAssistant: null,
  selectedAgentEditorKey: null,
  selectedToolEditorKey: null,
  selectedPromptKey: null,
  waitingForFirstToken: false,
};

const els = {
  shell: document.getElementById("shell"),
  sidebar: document.getElementById("sidebar"),
  sidebarBackdrop: document.getElementById("sidebar-backdrop"),
  conversationsList: document.getElementById("conversations-list"),
  messagesContainer: document.getElementById("messages-container"),
  emptyState: document.getElementById("empty-state"),
  messageInput: document.getElementById("message-input"),
  sendBtn: document.getElementById("send-btn"),
  stopBtn: document.getElementById("stop-btn"),
  newChatBtn: document.getElementById("new-chat-btn"),
  topbarNewChatBtn: document.getElementById("topbar-new-chat-btn"),
  toggleSidebarBtn: document.getElementById("toggle-sidebar-btn"),
  jumpLatestBtn: document.getElementById("jump-latest-btn"),
  runtimePill: document.getElementById("runtime-pill"),
  workspacePill: document.getElementById("workspace-pill"),
  activeAgentName: document.getElementById("active-agent-name"),
  activeAgentMeta: document.getElementById("active-agent-meta"),
  agentButton: document.getElementById("agent-button"),
  agentPopover: document.getElementById("agent-popover"),
  closeAgentPopover: document.getElementById("close-agent-popover"),
  agentGrid: document.getElementById("agent-grid"),
  openSettingsBtn: document.getElementById("open-settings-btn"),
  settingsDrawer: document.getElementById("settings-drawer"),
  settingsBackdrop: document.getElementById("settings-backdrop"),
  closeSettingsBtn: document.getElementById("close-settings-btn"),
  settingsTabs: [...document.querySelectorAll(".drawerTab")],
  settingsPanes: {
    system: document.getElementById("pane-system"),
    agents: document.getElementById("pane-agents"),
    tools: document.getElementById("pane-tools"),
    prompts: document.getElementById("pane-prompts"),
    advanced: document.getElementById("pane-advanced"),
  },
  systemForm: document.getElementById("system-form"),
  runtimeFacts: document.getElementById("runtime-facts"),
  agentsList: document.getElementById("agents-list"),
  agentEditor: document.getElementById("agent-editor"),
  toolsList: document.getElementById("tools-list"),
  toolEditor: document.getElementById("tool-editor"),
  promptsList: document.getElementById("prompts-list"),
  promptEditor: document.getElementById("prompt-editor"),
  yamlEditor: document.getElementById("yaml-editor"),
  settingsStatus: document.getElementById("settings-status"),
  saveSettingsBtn: document.getElementById("save-settings-btn"),
  reloadSettingsBtn: document.getElementById("reload-settings-btn"),
  addAgentBtn: document.getElementById("add-agent-btn"),
  addToolBtn: document.getElementById("add-tool-btn"),
  addPromptBtn: document.getElementById("add-prompt-btn"),
  promptChips: [...document.querySelectorAll("[data-prompt]")],
};

function renderMarkdown(text) {
  const parser = globalThis.marked;
  if (parser && typeof parser.parse === "function") {
    return parser.parse(text || "");
  }
  return escapeHtml(text || "").replace(/\n/g, "<br>");
}

function renderMessageContent(node, text, markdown = true) {
  setTypingState(node, false);
  if (markdown) {
    node.innerHTML = renderMarkdown(text);
  } else {
    node.textContent = text || "";
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function debounce(fn, delay = 120) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

function deepClone(obj) {
  return JSON.parse(JSON.stringify(obj));
}

function autoResizeTextarea() {
  els.messageInput.style.height = "auto";
  els.messageInput.style.height = `${Math.min(els.messageInput.scrollHeight, 220)}px`;
}

function getRuntimeReadyLabel() {
  return state.bootstrap?.isolation_enabled ? "container isolated" : "local runtime";
}

function formatTime(ts) {
  if (!ts) return "";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

function formatConversationTime(ts) {
  if (!ts) return "";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  if (date.toDateString() === now.toDateString()) {
    return formatTime(ts);
  }
  return date.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" });
}

function nowTime() {
  return new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

function openSidebar() {
  if (window.innerWidth > 1100) return;
  els.shell.classList.add("sidebar-open");
  document.body.classList.add("sidebar-open");
}

function closeSidebar() {
  els.shell.classList.remove("sidebar-open");
  document.body.classList.remove("sidebar-open");
}

function setComposerValue(value) {
  els.messageInput.value = value;
  autoResizeTextarea();
  els.sendBtn.disabled = !els.messageInput.value.trim() || state.isStreaming;
}

function switchSettingsTab(tab) {
  els.settingsTabs.forEach((node) => node.classList.toggle("active", node.dataset.tab === tab));
  Object.entries(els.settingsPanes).forEach(([key, pane]) => pane.classList.toggle("active", key === tab));
}

function setSettingsStatus(text, tone = "") {
  els.settingsStatus.textContent = text;
  els.settingsStatus.style.color = tone === "error" ? "var(--error)" : tone === "ok" ? "var(--accent)" : "var(--muted)";
}

async function fetchJson(url, options = {}) {
  const res = await fetch(url, options);
  let payload = null;
  try {
    payload = await res.json();
  } catch {
    payload = null;
  }
  if (!res.ok) {
    throw new Error(payload?.detail || `HTTP ${res.status}`);
  }
  return payload;
}

function getAgentOption(agentKey) {
  return state.bootstrap?.agents?.find((item) => item.key === agentKey) || null;
}

function openAgentPopover() {
  els.agentPopover.classList.remove("hidden");
}

function closeAgentPopover() {
  els.agentPopover.classList.add("hidden");
}

function openSettingsDrawer() {
  els.settingsDrawer.classList.remove("hidden");
}

function closeSettingsDrawer() {
  els.settingsDrawer.classList.add("hidden");
}

function updateActiveAgentButton() {
  const agent = getAgentOption(state.currentAgentKey) || state.bootstrap?.agents?.[0];
  if (!agent) {
    els.activeAgentName.textContent = "Agent not found";
    els.activeAgentMeta.textContent = "";
    return;
  }
  state.currentAgentKey = agent.key;
  els.activeAgentName.textContent = agent.name;
  const bits = [agent.model_description || agent.model_key, `${agent.tool_count} tools`];
  if (agent.mcp_enabled) bits.push("MCP");
  els.activeAgentMeta.textContent = bits.filter(Boolean).join(" · ");
}

function renderAgentPicker() {
  els.agentGrid.innerHTML = "";
  (state.bootstrap?.agents || []).forEach((agent) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `agentOption ${state.currentAgentKey === agent.key ? "active" : ""}`;
    btn.innerHTML = `
      <div class="agentOption__name">${escapeHtml(agent.name)}</div>
      <div class="agentOption__key">${escapeHtml(agent.key)}</div>
      <div class="agentOption__desc">${escapeHtml(agent.description || "No description")}</div>
      <div class="agentOption__meta">${escapeHtml(agent.model_description || agent.model_key || "")} · ${agent.tool_count} tools${agent.mcp_enabled ? " · MCP" : ""}</div>
    `;
    btn.addEventListener("click", async () => {
      state.currentAgentKey = agent.key;
      updateActiveAgentButton();
      renderAgentPicker();
      closeAgentPopover();
      await prepareAgent(agent.key);
    });
    els.agentGrid.appendChild(btn);
  });
}

function renderConversations() {
  els.conversationsList.innerHTML = "";
  state.conversations.forEach((conversation) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `conversationCard ${conversation.id === state.currentContextId ? "active" : ""}`;
    btn.innerHTML = `
      <div class="conversationCard__title">${escapeHtml(conversation.title || "New chat")}</div>
      <div class="conversationCard__title">${escapeHtml(conversation.title || "New chat")}</div>
      <div class="conversationCard__meta">
        <span>${escapeHtml(conversation.agent_name || conversation.agent_key || "")} · ${conversation.message_count || 0}</span>
        <span>${escapeHtml(formatConversationTime(conversation.updated_at))}</span>
      </div>
    `;
    btn.addEventListener("click", () => loadConversation(conversation.id));
    els.conversationsList.appendChild(btn);
  });
}

function createMessageElement(role, subtitle = "") {
  const wrapper = document.createElement("article");
  wrapper.className = `message message--${role}`;
  const avatar = role === "assistant" ? "AI" : role === "user" ? "U" : "S";
  const author = role === "assistant" ? "Grid" : role === "user" ? "You" : "System";
  wrapper.innerHTML = `
    <div class="message__panel">
      <div class="message__avatar">${avatar}</div>
      <div class="message__header">
        <div class="message__author">${author}</div>
        <div class="message__subtitle">${escapeHtml(subtitle)}</div>
      </div>
      <div class="message__content"></div>
    </div>
  `;
  els.messagesContainer.appendChild(wrapper);
  return wrapper;
}

function createAssistantTrace() {
  const details = document.createElement("details");
  details.className = "trace hidden";
  details.innerHTML = `
    <summary class="trace__summary">
      <span>
        <span class="trace__title">Execution trace</span>
        <span class="trace__caption">Detailed runtime trace of this response</span>
      </span>
      <span class="trace__caption trace__counter">0 events</span>
    </summary>
    <div class="trace__body"></div>
  `;
  return {
    root: details,
    body: details.querySelector(".trace__body"),
    counter: details.querySelector(".trace__counter"),
    count: 0,
  };
}

function setTypingState(node, active) {
  node.classList.toggle("is-typing", active);
  if (active) {
    node.innerHTML = `
      <div class="typingIndicator" aria-label="Grid typing">
        <span></span>
        <span></span>
        <span></span>
      </div>
    `;
  } else if (node.querySelector(".typingIndicator")) {
    node.innerHTML = "";
  }
}

function appendMessage(role, content, subtitle = "") {
  els.emptyState.classList.add("hidden");
  const wrapper = createMessageElement(role, subtitle);
  const contentNode = wrapper.querySelector(".message__content");
  renderMessageContent(contentNode, content, role !== "user");
  let trace = null;
  if (role === "assistant") {
    trace = createAssistantTrace();
    wrapper.querySelector(".message__panel").appendChild(trace.root);
  }
  return { wrapper, contentNode, trace };
}

function appendTraceEvent(traceState, event) {
  if (!traceState) return;
  traceState.root.classList.remove("hidden");
  traceState.count += 1;
  traceState.counter.textContent = `${traceState.count} events`;
  const item = document.createElement("div");
  item.className = `traceEvent traceEvent--${event.status || "done"}`;
  
  let detailsHtml = "";
  if (event.details && event.details !== "{}" && event.details.trim() !== "") {
    detailsHtml = `<pre class="traceEvent__details">${escapeHtml(event.details)}</pre>`;
  }

  item.innerHTML = `
    <div class="traceEvent__header">
      <div class="traceEvent__title">${escapeHtml(event.title || "Event")}</div>
      <div class="traceEvent__meta">${escapeHtml(event.ts != null ? `${event.ts} ms` : nowTime())}</div>
    </div>
    ${event.subtitle ? `<div class="traceEvent__subtitle">${escapeHtml(event.subtitle)}</div>` : ""}
    ${detailsHtml}
  `;
  traceState.body.appendChild(item);
}

function isNearBottom() {
  return els.messagesContainer.scrollHeight - els.messagesContainer.scrollTop - els.messagesContainer.clientHeight < 160;
}

function updateJumpButton() {
  const shouldHide = countRenderedMessages() === 0 || isNearBottom();
  els.jumpLatestBtn.classList.toggle("hidden", shouldHide);
}

function scrollMessagesToBottom(force = false) {
  if (force || isNearBottom()) {
    els.messagesContainer.scrollTop = els.messagesContainer.scrollHeight;
  }
  updateJumpButton();
}

function countRenderedMessages() {
  return els.messagesContainer.querySelectorAll(".message").length;
}

async function loadBootstrap() {
  state.bootstrap = await fetchJson("/api/chat/bootstrap");
  state.currentContextId = state.bootstrap.current_context_id || state.currentContextId;
  state.currentAgentKey = state.currentAgentKey || state.bootstrap.default_agent;
  els.runtimePill.textContent = getRuntimeReadyLabel();
  els.workspacePill.textContent = state.bootstrap.workspace_path || "";
  updateActiveAgentButton();
  renderAgentPicker();
}

async function loadConversations() {
  state.conversations = await fetchJson("/api/chat/conversations");
  renderConversations();
}

async function loadConversation(contextId) {
  if (!contextId || state.isStreaming) return;
  const payload = await fetchJson(`/api/chat/conversations/${contextId}`);
  state.currentContextId = payload.id;
  closeSidebar();
  if (payload.metadata?.agent_key) {
    state.currentAgentKey = payload.metadata.agent_key;
    updateActiveAgentButton();
    renderAgentPicker();
  }
  els.messagesContainer.innerHTML = "";
  payload.messages.forEach((message) => {
    const { wrapper, contentNode, trace } = appendMessage(message.role, message.content, formatTime(message.timestamp));
    if (message.role === "assistant" && message.metadata?.trace_events) {
      message.metadata.trace_events.forEach((event) => appendTraceEvent(trace, event));
    }
  });
  if (!payload.messages.length) {
    els.messagesContainer.appendChild(els.emptyState);
    els.emptyState.classList.remove("hidden");
  }
  renderConversations();
  scrollMessagesToBottom(true);
}

async function prepareAgent(agentKey) {
  if (!agentKey) return;
  els.runtimePill.textContent = `prepare ${agentKey}`;
  try {
    await fetchJson("/api/chat/prepare-agent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent_key: agentKey }),
    });
    els.runtimePill.textContent = getRuntimeReadyLabel();
  } catch (error) {
    els.runtimePill.textContent = "prepare failed";
    console.error(error);
  }
}

async function createConversation() {
  const payload = await fetchJson("/api/chat/conversations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agent_key: state.currentAgentKey }),
  });
  state.currentContextId = payload.id;
  closeSidebar();
  els.messagesContainer.innerHTML = "";
  els.messagesContainer.appendChild(els.emptyState);
  els.emptyState.classList.remove("hidden");
  updateJumpButton();
  await loadConversations();
}

function openSocket() {
  if (state.socket) {
    state.socket.close();
  }
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  state.socket = new WebSocket(`${protocol}//${location.host}/api/chat/ws/${state.currentContextId}`);
  return new Promise((resolve, reject) => {
    state.socket.addEventListener("open", resolve, { once: true });
    state.socket.addEventListener("error", reject, { once: true });
  });
}

function finishStreaming() {
  if (!state.isStreaming) return;
  state.isStreaming = false;
  state.waitingForFirstToken = false;
  els.stopBtn.classList.add("hidden");
  els.sendBtn.disabled = !els.messageInput.value.trim();
  els.runtimePill.textContent = getRuntimeReadyLabel();
  if (state.currentAssistant?.contentNode && !state.currentAssistant.text) {
    setTypingState(state.currentAssistant.contentNode, false);
  }
  if (state.socket) {
    state.socket.onmessage = null;
    state.socket.onclose = null;
    if (state.socket.readyState === WebSocket.OPEN || state.socket.readyState === WebSocket.CONNECTING) {
      state.socket.close();
    }
    state.socket = null;
  }
  state.currentAssistant = null;
  loadConversations();
  reconcileCurrentConversation();
}

async function reconcileCurrentConversation() {
  if (!state.currentContextId) return;
  try {
    const payload = await fetchJson(`/api/chat/conversations/${state.currentContextId}`);
    const renderedCount = countRenderedMessages();
    const lastRenderedText = els.messagesContainer.querySelector(".message:last-of-type .message__content")?.textContent?.trim() || "";
    const lastServerText = payload.messages[payload.messages.length - 1]?.content?.trim() || "";
    if (payload.messages.length !== renderedCount || (lastServerText && lastServerText !== lastRenderedText)) {
      els.messagesContainer.innerHTML = "";
      payload.messages.forEach((message) => {
        const { wrapper, contentNode, trace } = appendMessage(message.role, message.content, formatTime(message.timestamp));
        if (message.role === "assistant" && message.metadata?.trace_events) {
          message.metadata.trace_events.forEach((event) => appendTraceEvent(trace, event));
        }
      });
      if (!payload.messages.length) {
        els.messagesContainer.appendChild(els.emptyState);
        els.emptyState.classList.remove("hidden");
      }
      scrollMessagesToBottom(true);
    }
    if (payload.metadata?.agent_key && payload.metadata.agent_key !== state.currentAgentKey) {
      state.currentAgentKey = payload.metadata.agent_key;
      updateActiveAgentButton();
      renderAgentPicker();
    }
  } catch (error) {
    console.error("Failed to reconcile conversation", error);
  }
}

function stopStreaming() {
  if (!state.socket || state.socket.readyState !== WebSocket.OPEN) return;
  state.socket.send(JSON.stringify({ action: "stop" }));
}

async function sendMessage() {
  const message = els.messageInput.value.trim();
  if (!message || state.isStreaming) return;
  if (!state.currentContextId) {
    await createConversation();
  }

  els.messageInput.value = "";
  autoResizeTextarea();
  els.sendBtn.disabled = true;
  state.isStreaming = true;
  state.waitingForFirstToken = true;
  els.stopBtn.classList.remove("hidden");
  els.emptyState.classList.add("hidden");
  els.runtimePill.textContent = `${getAgentOption(state.currentAgentKey)?.name || state.currentAgentKey} thinking`;

  appendMessage("user", message, nowTime());
  const assistant = appendMessage(
    "assistant",
    "",
    `${getAgentOption(state.currentAgentKey)?.name || state.currentAgentKey} · ${nowTime()}`
  );
  setTypingState(assistant.contentNode, true);
  state.currentAssistant = { contentNode: assistant.contentNode, trace: assistant.trace, text: "" };
  scrollMessagesToBottom(true);

  try {
    await prepareAgent(state.currentAgentKey);
    await openSocket();
    state.socket.send(JSON.stringify({ message, agent_key: state.currentAgentKey }));

    state.socket.onmessage = (evt) => {
      const payload = JSON.parse(evt.data);
      if (payload.type === "token") {
        state.waitingForFirstToken = false;
        els.runtimePill.textContent = `${getAgentOption(state.currentAgentKey)?.name || state.currentAgentKey} responds`;
        state.currentAssistant.text += payload.content;
        renderMessageContent(state.currentAssistant.contentNode, state.currentAssistant.text, true);
      } else if (payload.type === "final_output") {
        state.waitingForFirstToken = false;
        state.currentAssistant.text = payload.content;
        renderMessageContent(state.currentAssistant.contentNode, state.currentAssistant.text, true);
      } else if (payload.type === "trace") {
        if (payload.kind === "thinking") {
          state.currentAssistant.text = "";
          renderMessageContent(state.currentAssistant.contentNode, "", true);
        }
        appendTraceEvent(state.currentAssistant.trace, payload);
      } else if (payload.type === "error") {
        state.waitingForFirstToken = false;
        setTypingState(state.currentAssistant.contentNode, false);
        appendTraceEvent(state.currentAssistant.trace, {
          title: "Execution error",
          subtitle: state.currentAgentKey,
          details: payload.content,
          status: "error",
          ts: "!",
        });
      } else if (payload.type === "done") {
        finishStreaming();
      }
      scrollMessagesToBottom();
    };

    state.socket.onclose = () => finishStreaming();
  } catch (error) {
    appendTraceEvent(state.currentAssistant.trace, {
      title: "Failed to send request",
      subtitle: state.currentAgentKey,
      details: error.message,
      status: "error",
    });
    finishStreaming();
  }
}

function renderEditorList(container, entries, activeKey, onPick, labelBuilder) {
  container.innerHTML = "";
  entries.forEach(([key, value]) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = `editorRow ${key === activeKey ? "active" : ""}`;
    const label = labelBuilder(key, value);
    row.innerHTML = `
      <div class="editorRow__title">${escapeHtml(label.title)}</div>
      <div class="editorRow__meta">${escapeHtml(label.meta || key)}</div>
    `;
    row.addEventListener("click", () => onPick(key));
    container.appendChild(row);
  });
}

function renderSystemSettings(meta) {
  els.systemForm.innerHTML = "";
  const fields = [
    { key: "default_agent", label: "Default agent", type: "select", options: Object.keys(state.settings.agents || {}) },
    { key: "max_history", label: "Max history", type: "number" },
    { key: "max_turns", label: "Max turns", type: "number" },
    { key: "agent_timeout", label: "Agent timeout", type: "number" },
    { key: "working_directory", label: "Working directory", type: "text" },
    { key: "config_directory", label: "Config directory", type: "text" },
    { key: "mcp_enabled", label: "MCP enabled", type: "checkbox" },
    { key: "allow_path_override", label: "Allow path override", type: "checkbox" },
  ];

  fields.forEach((field) => {
    const wrap = document.createElement("div");
    wrap.className = "field";
    const value = state.settings.settings?.[field.key];
    let control = "";
    if (field.type === "select") {
      control = `<select data-system-key="${field.key}">${field.options
        .map(
          (item) =>
            `<option value="${escapeHtml(item)}" ${item === value ? "selected" : ""}>${escapeHtml(
              state.settings.agents?.[item]?.name || item
            )}</option>`
        )
        .join("")}</select>`;
    } else if (field.type === "checkbox") {
      control = `<label class="checkPill"><input type="checkbox" data-system-key="${field.key}" ${
        value ? "checked" : ""
      }><span>${field.label}</span></label>`;
    } else {
      control = `<input type="${field.type}" data-system-key="${field.key}" value="${escapeHtml(value ?? "")}">`;
    }
    wrap.innerHTML = `<label>${field.label}</label>${control}`;
    els.systemForm.appendChild(wrap);
  });

  els.systemForm.querySelectorAll("[data-system-key]").forEach((node) => {
    node.addEventListener("input", onSystemFieldChange);
    node.addEventListener("change", onSystemFieldChange);
  });

  const facts = [
    ["Workspace", meta.workspace_path],
    ["Persist path", meta.persist_path],
    ["Isolation", meta.isolation_enabled ? `enabled (${meta.container_id || "docker"})` : "disabled"],
    ["Agents", Object.keys(state.settings.agents || {}).length],
    ["Tools", Object.keys(state.settings.tools || {}).length],
    ["Prompt templates", Object.keys(state.settings.prompt_templates || {}).length],
  ];
  els.runtimeFacts.innerHTML = facts
    .map(
      ([label, value]) => `
      <div class="runtimeFact">
        <div class="runtimeFact__label">${escapeHtml(label)}</div>
        <div class="runtimeFact__value">${escapeHtml(value)}</div>
      </div>
    `
    )
    .join("");
}

function onSystemFieldChange(event) {
  const key = event.target.dataset.systemKey;
  if (!key) return;
  const value = event.target.type === "checkbox" ? event.target.checked : event.target.value;
  state.settings.settings[key] = event.target.type === "number" ? Number(value) : value;
}

function renderAgentsSettings() {
  const agentsEntries = Object.entries(state.settings.agents || {});
  renderEditorList(
    els.agentsList,
    agentsEntries,
    state.selectedAgentEditorKey,
    (key) => {
      state.selectedAgentEditorKey = key;
      renderAgentsSettings();
    },
    (key, value) => ({ title: value.name || key, meta: `${key} · ${value.model || ""}` })
  );

  const agent = state.settings.agents?.[state.selectedAgentEditorKey];
  if (!agent) {
    els.agentEditor.innerHTML = '<div class="editorPanel__empty">Select or create an agent.</div>';
    return;
  }

  const promptKeys = Object.keys(state.settings.prompt_templates || {});
  const modelKeys = Object.keys(state.settings.models || {});
  const toolKeys = Object.keys(state.settings.tools || {});
  els.agentEditor.innerHTML = `
    <div class="editorPanel__header">
      <div>
        <h3>${escapeHtml(agent.name || state.selectedAgentEditorKey)}</h3>
        <div class="editorPanel__sub">Internal key: ${escapeHtml(state.selectedAgentEditorKey)}</div>
      </div>
      <button class="secondaryBtn" id="delete-agent-btn" type="button">Delete</button>
    </div>
    <div class="formGrid">
      <div class="field"><label>Name</label><input id="agent-name" value="${escapeHtml(agent.name || "")}"></div>
      <div class="field"><label>Model</label><select id="agent-model">${modelKeys
        .map(
          (key) =>
            `<option value="${escapeHtml(key)}" ${key === agent.model ? "selected" : ""}>${escapeHtml(
              state.settings.models[key]?.description || key
            )}</option>`
        )
        .join("")}</select></div>
      <div class="field"><label>Base prompt</label><select id="agent-base-prompt">${promptKeys
        .map(
          (key) => `<option value="${escapeHtml(key)}" ${key === agent.base_prompt ? "selected" : ""}>${escapeHtml(key)}</option>`
        )
        .join("")}</select></div>
      <div class="field"><label>Description</label><input id="agent-description" value="${escapeHtml(agent.description || "")}"></div>
      <div class="field"><label>MCP enabled</label><label class="checkPill"><input type="checkbox" id="agent-mcp-enabled" ${
        agent.mcp_enabled ? "checked" : ""
      }><span>Enable MCP</span></label></div>
    </div>
    <div class="field">
      <label>Custom prompt</label>
      <textarea id="agent-custom-prompt">${escapeHtml(agent.custom_prompt || "")}</textarea>
    </div>
    <div class="field">
      <label>Tools</label>
      <div class="checkGrid">
        ${toolKeys
          .map(
            (toolKey) => `
          <label class="checkPill">
            <input type="checkbox" data-agent-tool="${escapeHtml(toolKey)}" ${(agent.tools || []).includes(toolKey) ? "checked" : ""}>
            <span>${escapeHtml(state.settings.tools[toolKey]?.name || toolKey)}</span>
          </label>
        `
          )
          .join("")}
      </div>
    </div>
  `;

  els.agentEditor.querySelector("#agent-name").addEventListener("input", (e) => {
    agent.name = e.target.value;
  });
  els.agentEditor.querySelector("#agent-model").addEventListener("change", (e) => {
    agent.model = e.target.value;
  });
  els.agentEditor.querySelector("#agent-base-prompt").addEventListener("change", (e) => {
    agent.base_prompt = e.target.value;
  });
  els.agentEditor.querySelector("#agent-description").addEventListener("input", (e) => {
    agent.description = e.target.value;
  });
  els.agentEditor.querySelector("#agent-mcp-enabled").addEventListener("change", (e) => {
    agent.mcp_enabled = e.target.checked;
  });
  els.agentEditor.querySelector("#agent-custom-prompt").addEventListener("input", (e) => {
    agent.custom_prompt = e.target.value;
  });
  els.agentEditor.querySelectorAll("[data-agent-tool]").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      agent.tools = [...els.agentEditor.querySelectorAll("[data-agent-tool]")]
        .filter((node) => node.checked)
        .map((node) => node.dataset.agentTool);
    });
  });
  els.agentEditor.querySelector("#delete-agent-btn").addEventListener("click", () => {
    if (!confirm(`Delete agent ${state.selectedAgentEditorKey}?`)) return;
    delete state.settings.agents[state.selectedAgentEditorKey];
    state.selectedAgentEditorKey = Object.keys(state.settings.agents)[0] || null;
    renderAgentsSettings();
  });
}

function renderToolsSettings() {
  const toolEntries = Object.entries(state.settings.tools || {});
  renderEditorList(
    els.toolsList,
    toolEntries,
    state.selectedToolEditorKey,
    (key) => {
      state.selectedToolEditorKey = key;
      renderToolsSettings();
    },
    (key, value) => ({ title: value.name || key, meta: `${key} · ${value.type || ""}` })
  );

  const tool = state.settings.tools?.[state.selectedToolEditorKey];
  if (!tool) {
    els.toolEditor.innerHTML = '<div class="editorPanel__empty">Select or create a tool.</div>';
    return;
  }

  els.toolEditor.innerHTML = `
    <div class="editorPanel__header">
      <div>
        <h3>${escapeHtml(tool.name || state.selectedToolEditorKey)}</h3>
        <div class="editorPanel__sub">Key: ${escapeHtml(state.selectedToolEditorKey)}</div>
      </div>
      <button class="secondaryBtn" id="delete-tool-btn" type="button">Delete</button>
    </div>
    <div class="formGrid">
      <div class="field"><label>Name</label><input id="tool-name" value="${escapeHtml(tool.name || "")}"></div>
      <div class="field"><label>Type</label><select id="tool-type">${["function", "mcp", "agent"]
        .map((type) => `<option value="${type}" ${tool.type === type ? "selected" : ""}>${type}</option>`)
        .join("")}</select></div>
      <div class="field"><label>Target agent</label><input id="tool-target-agent" value="${escapeHtml(tool.target_agent || "")}"></div>
      <div class="field"><label>Context strategy</label><input id="tool-context-strategy" value="${escapeHtml(tool.context_strategy || "")}"></div>
    </div>
    <div class="field"><label>Description</label><textarea id="tool-description">${escapeHtml(tool.description || "")}</textarea></div>
    <div class="field"><label>Prompt addition</label><textarea id="tool-prompt-addition">${escapeHtml(tool.prompt_addition || "")}</textarea></div>
    <div class="field"><label>Server command (one argument per line)</label><textarea id="tool-server-command">${escapeHtml(
      (tool.server_command || []).join("\n")
    )}</textarea></div>
    <div class="field"><label>Env vars (KEY=value)</label><textarea id="tool-env-vars">${escapeHtml(
      Object.entries(tool.env_vars || {})
        .map(([key, value]) => `${key}=${value}`)
        .join("\n")
    )}</textarea></div>
  `;

  els.toolEditor.querySelector("#tool-name").addEventListener("input", (e) => {
    tool.name = e.target.value;
  });
  els.toolEditor.querySelector("#tool-type").addEventListener("change", (e) => {
    tool.type = e.target.value;
  });
  els.toolEditor.querySelector("#tool-target-agent").addEventListener("input", (e) => {
    tool.target_agent = e.target.value || null;
  });
  els.toolEditor.querySelector("#tool-context-strategy").addEventListener("input", (e) => {
    tool.context_strategy = e.target.value || null;
  });
  els.toolEditor.querySelector("#tool-description").addEventListener("input", (e) => {
    tool.description = e.target.value;
  });
  els.toolEditor.querySelector("#tool-prompt-addition").addEventListener("input", (e) => {
    tool.prompt_addition = e.target.value || null;
  });
  els.toolEditor.querySelector("#tool-server-command").addEventListener("input", (e) => {
    tool.server_command = e.target.value
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
  });
  els.toolEditor.querySelector("#tool-env-vars").addEventListener("input", (e) => {
    const env = {};
    e.target.value
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .forEach((line) => {
        const [key, ...rest] = line.split("=");
        if (key) env[key] = rest.join("=");
      });
    tool.env_vars = env;
  });
  els.toolEditor.querySelector("#delete-tool-btn").addEventListener("click", () => {
    if (!confirm(`Delete tool ${state.selectedToolEditorKey}?`)) return;
    delete state.settings.tools[state.selectedToolEditorKey];
    Object.values(state.settings.agents || {}).forEach((agent) => {
      agent.tools = (agent.tools || []).filter((toolKey) => toolKey !== state.selectedToolEditorKey);
    });
    state.selectedToolEditorKey = Object.keys(state.settings.tools)[0] || null;
    renderToolsSettings();
    renderAgentsSettings();
  });
}

function renderPromptsSettings() {
  const entries = Object.entries(state.settings.prompt_templates || {});
  renderEditorList(
    els.promptsList,
    entries,
    state.selectedPromptKey,
    (key) => {
      state.selectedPromptKey = key;
      renderPromptsSettings();
    },
    (key, value) => ({ title: key, meta: `${String(value || "").length} chars` })
  );

  const promptValue = state.settings.prompt_templates?.[state.selectedPromptKey];
  if (promptValue == null) {
    els.promptEditor.innerHTML = '<div class="editorPanel__empty">Select or create a prompt template.</div>';
    return;
  }

  els.promptEditor.innerHTML = `
    <div class="editorPanel__header">
      <div>
        <h3>${escapeHtml(state.selectedPromptKey)}</h3>
        <div class="editorPanel__sub">Prompt template</div>
      </div>
      <button class="secondaryBtn" id="delete-prompt-btn" type="button">Delete</button>
    </div>
    <div class="field">
      <label>Prompt content</label>
      <textarea id="prompt-content">${escapeHtml(promptValue)}</textarea>
    </div>
  `;

  els.promptEditor.querySelector("#prompt-content").addEventListener("input", (e) => {
    state.settings.prompt_templates[state.selectedPromptKey] = e.target.value;
    renderPromptsSettings();
  });
  els.promptEditor.querySelector("#delete-prompt-btn").addEventListener("click", () => {
    if (!confirm(`Delete prompt ${state.selectedPromptKey}?`)) return;
    delete state.settings.prompt_templates[state.selectedPromptKey];
    state.selectedPromptKey = Object.keys(state.settings.prompt_templates)[0] || null;
    renderPromptsSettings();
  });
}

async function loadSettings() {
  const payload = await fetchJson("/api/settings");
  state.settings = deepClone(payload.config);
  state.rawYaml = payload.raw_yaml;
  els.yamlEditor.value = state.rawYaml;
  if (!state.selectedAgentEditorKey) {
    state.selectedAgentEditorKey = Object.keys(state.settings.agents || {})[0] || null;
  }
  if (!state.selectedToolEditorKey) {
    state.selectedToolEditorKey = Object.keys(state.settings.tools || {})[0] || null;
  }
  if (!state.selectedPromptKey) {
    state.selectedPromptKey = Object.keys(state.settings.prompt_templates || {})[0] || null;
  }
  renderSystemSettings(payload.meta);
  renderAgentsSettings();
  renderToolsSettings();
  renderPromptsSettings();
}

async function saveStructuredSettings() {
  setSettingsStatus("Saving structured config...");
  const payload = await fetchJson("/api/settings/structured", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config: state.settings }),
  });
  state.settings = deepClone(payload.config);
  state.rawYaml = payload.raw_yaml;
  els.yamlEditor.value = state.rawYaml;
  setSettingsStatus("Changes saved.", "ok");
  await loadBootstrap();
  await loadConversations();
  renderSystemSettings(payload.meta);
  renderAgentsSettings();
  renderToolsSettings();
  renderPromptsSettings();
  renderAgentPicker();
  updateActiveAgentButton();
}

async function saveYamlSettings() {
  setSettingsStatus("Saving YAML...");
  const payload = await fetchJson("/api/settings/yaml", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ yaml_content: els.yamlEditor.value }),
  });
  state.settings = deepClone(payload.config);
  state.rawYaml = payload.raw_yaml;
  els.yamlEditor.value = state.rawYaml;
  setSettingsStatus("YAML saved and validated.", "ok");
  await loadBootstrap();
  await loadConversations();
  renderSystemSettings(payload.meta);
  renderAgentsSettings();
  renderToolsSettings();
  renderPromptsSettings();
  renderAgentPicker();
  updateActiveAgentButton();
}

async function saveSettings() {
  try {
    const activeTab = els.settingsTabs.find((node) => node.classList.contains("active"))?.dataset.tab;
    if (activeTab === "advanced") {
      await saveYamlSettings();
    } else {
      await saveStructuredSettings();
    }
  } catch (error) {
    setSettingsStatus(error.message, "error");
  }
}

function bindStaticEvents() {
  const onInput = debounce(() => {
    els.sendBtn.disabled = !els.messageInput.value.trim() || state.isStreaming;
    autoResizeTextarea();
  }, 10);
  els.messageInput.addEventListener("input", onInput);
  els.messageInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendMessage();
    }
  });

  els.newChatBtn.addEventListener("click", createConversation);
  els.topbarNewChatBtn.addEventListener("click", createConversation);
  els.sendBtn.addEventListener("click", sendMessage);
  els.stopBtn.addEventListener("click", stopStreaming);
  els.toggleSidebarBtn.addEventListener("click", openSidebar);
  els.sidebarBackdrop.addEventListener("click", closeSidebar);
  els.jumpLatestBtn.addEventListener("click", () => scrollMessagesToBottom(true));
  els.messagesContainer.addEventListener("scroll", updateJumpButton);
  els.agentButton.addEventListener("click", () => {
    if (els.agentPopover.classList.contains("hidden")) {
      openAgentPopover();
    } else {
      closeAgentPopover();
    }
  });
  els.closeAgentPopover.addEventListener("click", closeAgentPopover);
  els.openSettingsBtn.addEventListener("click", async () => {
    await loadSettings();
    openSettingsDrawer();
  });
  els.closeSettingsBtn.addEventListener("click", closeSettingsDrawer);
  els.settingsBackdrop.addEventListener("click", closeSettingsDrawer);
  els.settingsTabs.forEach((node) => node.addEventListener("click", () => switchSettingsTab(node.dataset.tab)));
  els.saveSettingsBtn.addEventListener("click", saveSettings);
  els.reloadSettingsBtn.addEventListener("click", async () => {
    await loadSettings();
    setSettingsStatus("Configuration re-read from disk.");
  });
  els.addAgentBtn.addEventListener("click", () => {
    const key = prompt("New agent key", "new_agent")?.trim();
    if (!key || state.settings.agents[key]) return;
    state.settings.agents[key] = {
      name: "New agent",
      model: Object.keys(state.settings.models || {})[0] || "",
      tools: [],
      base_prompt: Object.keys(state.settings.prompt_templates || {})[0] || "base",
      custom_prompt: "",
      description: "",
      mcp_enabled: false,
    };
    state.selectedAgentEditorKey = key;
    renderAgentsSettings();
  });
  els.addToolBtn.addEventListener("click", () => {
    const key = prompt("New tool key", "new_tool")?.trim();
    if (!key || state.settings.tools[key]) return;
    state.settings.tools[key] = { type: "function", name: "", description: "", prompt_addition: "" };
    state.selectedToolEditorKey = key;
    renderToolsSettings();
  });
  els.addPromptBtn.addEventListener("click", () => {
    const key = prompt("New prompt key", "new_prompt")?.trim();
    if (!key || state.settings.prompt_templates[key]) return;
    state.settings.prompt_templates[key] = "";
    state.selectedPromptKey = key;
    renderPromptsSettings();
  });
  els.promptChips.forEach((chip) => {
    chip.addEventListener("click", () => {
      setComposerValue(chip.dataset.prompt || "");
      els.messageInput.focus();
    });
  });

  document.addEventListener("click", (event) => {
    const target = event.target;
    if (!els.agentPopover.classList.contains("hidden") && !els.agentPopover.contains(target) && !els.agentButton.contains(target)) {
      closeAgentPopover();
    }
  });
  window.addEventListener("resize", () => {
    if (window.innerWidth > 1100) {
      closeSidebar();
    }
    updateJumpButton();
  });
}

async function init() {
  bindStaticEvents();
  await loadBootstrap();
  await loadConversations();
  if (state.conversations[0]) {
    await loadConversation(state.conversations[0].id);
  }
  await prepareAgent(state.currentAgentKey);
}

init().catch((error) => {
  console.error(error);
  els.runtimePill.textContent = "bootstrap failed";
  els.activeAgentName.textContent = "bootstrap failed";
});
