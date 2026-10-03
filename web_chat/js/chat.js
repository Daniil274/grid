/**
 * Turn orchestration: one user message in, one streamed answer out.
 *
 * Everything time-ordered about a turn lives here - creating the conversation
 * if there isn't one, opening the socket, routing each server event to the
 * message that owns it, and putting the UI back to rest afterwards, including
 * when the turn fails or the socket drops mid-answer.
 */

import { api } from "./net/api.js";
import { ChatConnection } from "./net/chat-socket.js";
import { toast } from "./ui/toast.js";

export class ChatController {
  /**
   * @param {object} deps
   * @param {import("./lib/store.js").createStore} deps.store
   * @param {ReturnType<import("./ui/transcript.js").createTranscript>} deps.transcript
   * @param {() => Promise<void>} deps.onConversationsChanged
   * @param {import("./ui/speech.js").SpeechPlayer} deps.speech
   * @param {object} [deps.voice] optional VoiceUI bridge
   */
  constructor({ store, transcript, onConversationsChanged, speech, voice }) {
    this._store = store;
    this._transcript = transcript;
    this._refreshConversations = onConversationsChanged;
    this._speech = speech;
    this._voice = voice;
    this._connection = null;
    this._activeTurn = null;
    // Agents whose tool problems were already announced on this page.
    this._warnedAgents = new Set();
  }

  get isStreaming() {
    return this._store.get().streaming;
  }

  async reconnectAfterRestart() {
    const contextId = this._store.get().contextId;
    if (this._activeTurn) this._settle(this._activeTurn);
    if (contextId) await this._reconcile(contextId, { force: true });
    await this._refreshConversations();
  }

  /** Text streamed so far for the turn in flight - the voice layer reads it. */
  get currentAnswer() {
    return this._activeTurn?.message.text ?? "";
  }

  /**
   * @param {string} rawText
   * @param {{spoken?: boolean, images?: string[]}} [options] `spoken` marks a
   *   dictated turn, which is the only kind that reads its answer aloud without
   *   being asked to; `images` are data URLs attached to the message.
   */
  async send(rawText, { spoken = false, images = [], delivery = null } = {}) {
    if (this._store.get().compacting) return;
    const text = rawText?.trim() ?? "";
    if (!text && !images.length) return;
    if (this.isStreaming) {
      // The agent is working: the server delivers the message now, at the
      // agent's next step or after the turn; `delivery` null lets it decide.
      const { systemKey, agentKey } = this._store.get();
      this._connection?.send(text, { system_key: systemKey, agent_key: agentKey }, images, null, delivery);
      return;
    }
    await this._begin({ text, spoken, images });
  }

  /** Send a waiting message right away. */
  async sendQueued(item) {
    if (this._store.get().compacting) return;
    if (this.isStreaming) {
      this._connection?.queued("send_queued", item.id);
      return;
    }
    await this._queuedWhileIdle("unqueue", item.id);
    await this.send(item.text, { images: item.images ?? [] });
  }

  /** Drop a waiting message. */
  async dropQueued(item) {
    if (this.isStreaming) this._connection?.queued("unqueue", item.id);
    else await this._queuedWhileIdle("unqueue", item.id);
  }

  /** Act on the queue when no turn runs: a short connection of its own. */
  async _queuedWhileIdle(action, id) {
    const contextId = this._store.get().contextId;
    if (!contextId) return;
    const connection = await ChatConnection.open(contextId);
    await new Promise((resolve) => {
      connection.on("queue", ({ items }) => {
        this._store.set({ queue: items });
        resolve();
      });
      connection.queued(action, id);
      setTimeout(resolve, 3000);
    });
    connection.close();
  }

  /**
   * Resume the turn the conversation's last message says was interrupted. The
   * server runs it with the agent that stopped, from everything it had done.
   */
  async continueTurn() {
    if (this.isStreaming || this._store.get().compacting || !this._store.get().contextId) return;
    await this._begin({ text: null, spoken: false, images: [] });
  }

  /** Compact the agent session; the visible transcript and draft stay intact. */
  async compactContext() {
    const { contextId, streaming, compacting, restartPending } = this._store.get();
    if (!contextId) throw new Error("Open a conversation first.");
    if (streaming) throw new Error("Stop the running turn before compacting.");
    if (compacting) throw new Error("Context compaction is already in progress.");
    if (restartPending) throw new Error("Wait for the server restart to finish.");
    this._compactingId = contextId;
    this._store.set({ compacting: true });
    try {
      const result = await api.compactConversation(contextId);
      // Compaction leaves a display-only marker in the stored thread; pull the
      // conversation back so it shows up right away rather than on the next turn.
      await this._reconcile(contextId, { force: true });
      return result;
    } finally {
      this._compactingId = null;
      // Only the chat being compacted shows it; another one may be open by now.
      if (this._store.get().contextId === contextId) this._store.set({ compacting: false });
    }
  }

  /**
   * Save an edited user message: the conversation forks before it and the new
   * text becomes the next message of that branch. The original stays one click
   * away in the message's version switcher. A running turn is stopped first.
   * @param {{id: string, text: string, images: string[]}} edited
   */
  async editMessage({ id, text, images = [] }) {
    try {
      if (this._store.get().compacting) throw new Error("Wait for context compaction to finish before editing.");
      if (this.isStreaming) await this._stopNow();
      const branch = await api.createBranch(this._store.get().contextId, id);
      await this.openConversation(branch.id);
      await this._begin({ text, spoken: false, images, editOf: branch.edit_of });
    } catch (error) {
      toast(error.message, { tone: "error" });
    }
  }

  /** What voice control needs to know about the chat (web_chat/voice_turns.py). */
  voiceState() {
    const versions = this._transcript.lastVersions();
    const { resumable, queue, conversations, rootId } = this._store.get();
    return {
      resumable: Boolean(resumable),
      has_previous_version: Boolean(versions && versions.index > 0),
      has_next_version: Boolean(versions && versions.index < versions.total - 1),
      queue_count: queue?.length ?? 0,
      reading_aloud: this._speech.activeId !== null && this._speech.activeId !== undefined,
      chat_title: conversations.find((item) => item.id === rootId)?.title ?? "",
    };
  }

  /**
   * Run a spoken command - the same functions the buttons call. Returns what
   * to show (and say) about it. `argument` comes chosen by the decision model.
   */
  async voiceCommand(action, argument = null, { chooseAgent } = {}) {
    const { contextId, rootId, queue } = this._store.get();
    switch (action) {
      case "stop":
        if (!this.isStreaming) return "Агент сейчас не работает.";
        this.stop();
        return "Останавливаю после текущего шага.";
      case "stop_now":
        if (!this.isStreaming) return "Агент сейчас не работает.";
        this.stop({ now: true });
        return "Останавливаю.";
      case "continue":
        if (!this._store.get().resumable) return "Продолжать нечего.";
        await this.continueTurn();
        return "Продолжаю.";
      case "edit_last": {
        const last = this._transcript.lastUserMessage();
        if (!last || !argument?.text) return "Не нашёл сообщение, которое можно исправить.";
        await this.editMessage({ id: last.messageId, text: argument.text, images: last.images });
        return `Исправил сообщение: ${argument.text}`;
      }
      case "previous_version":
      case "next_version": {
        const versions = this._transcript.lastVersions();
        const target = versions?.targets[versions.index + (action === "next_version" ? 1 : -1)];
        if (!target) return "Другой версии нет.";
        await this.switchVersion(target);
        return action === "next_version" ? "Следующая версия." : "Предыдущая версия.";
      }
      case "new_chat":
        await this.startConversation();
        return "Новый чат.";
      case "open_chat":
        if (!argument?.context_id) return "Не нашёл такой чат.";
        await this.openConversation(argument.context_id);
        return `Открыл чат «${argument.title}».`;
      case "choose_agent":
        if (!argument || !chooseAgent) return "Не понял, какого агента выбрать.";
        await chooseAgent({ systemKey: argument.system_key, agentKey: argument.agent_key });
        return argument.agent_key ? `Выбран ${argument.label}.` : "Агента выбирает маршрутизатор.";
      case "compact": {
        if (!contextId) return "Чат пуст.";
        try {
          const result = await this.compactContext();
          return `Контекст сжат: примерно ${result.tokens_before} → ${result.tokens_after} токенов.`;
        } catch (error) {
          return error.message;
        }
      }
      case "read_aloud": {
        const answer = this._transcript.lastAnswer();
        if (!answer) return "Нечего читать.";
        this._speech.speak(answer.id, answer.text);
        return "Читаю ответ.";
      }
      case "stop_reading":
        this.stopSpeaking();
        return "Молчу.";
      case "rename_chat":
        if (!rootId || !argument?.text) return "Не понял новое название.";
        await this.renameConversation(rootId, argument.text);
        return `Чат переименован: ${argument.text}`;
      case "delete_chat":
        if (!rootId) return "Удалять нечего.";
        await this.deleteConversation(rootId);
        return "Чат удалён.";
      case "send_queued":
        if (!queue?.length) return "Ожидающих сообщений нет.";
        await this.sendQueued(queue[0]);
        return "Отправляю ожидающее сообщение.";
      case "cancel_queued":
        if (!queue?.length) return "Ожидающих сообщений нет.";
        for (const item of queue) await this.dropQueued(item);
        return "Ожидающие сообщения отменены.";
      default:
        return "Не понял команду.";
    }
  }

  /** Open another version of an edited message: its branch becomes the active one. */
  async switchVersion(contextId) {
    if (!contextId || contextId === this._store.get().contextId) return;
    try {
      await api.activateBranch(contextId);
      await this.openConversation(contextId);
      void this._refreshConversations();
    } catch (error) {
      toast(error.message, { tone: "error" });
    }
  }

  /** Stop the running turn at once and wait until it has settled. */
  _stopNow() {
    const turn = this._activeTurn;
    if (!turn) return Promise.resolve();
    const settled = new Promise((resolve) => { turn.onSettled = resolve; });
    this.stop({ now: true });
    return settled;
  }

  /** Start a turn: `text` null is Continue. */
  async _begin({ text, spoken, images, editOf = null }) {
    this.stopSpeaking();

    if (!this._store.get().contextId) await this.startConversation();
    const { contextId, systemKey, agentKey } = this._store.get();

    // Only the newest interruption can be continued, and only until now.
    this._transcript.retireContinue();
    this._store.set({ resumable: false });
    if (text === null) this._transcript.add({ role: "user", kind: "continuation", timestamp: Date.now() });
    else this._transcript.add({ role: "user", content: text, images, timestamp: Date.now() });
    const message = this._transcript.add({
      role: "assistant",
      author: this._pinnedAgentName(systemKey, agentKey) ?? (text === null ? "Grid" : "Routing…"),
      timestamp: Date.now(),
    });
    message.setWaiting(true);
    message.reasoning.start();
    this._transcript.follow();
    this._store.set({ streaming: true, stopping: false });
    void this._refreshConversations(); // the rail shows this chat as working

    // A dictated turn reads its answer back once the answer is settled. It
    // cannot read the token stream as it arrives: `run_streamed` emits every
    // assistant message of the run, including the narration before each tool
    // call, and only the last one survives into `final_output`. Speaking the
    // stream would read aloud the steps the message itself discards.
    const turn = { message, contextId, failed: false, finalText: "", spoken };
    this._activeTurn = turn;
    try {
      this._connection = await ChatConnection.open(contextId);
      this._bind(this._connection, turn);
      if (text === null) this._connection.resume();
      else this._connection.send(text, { system_key: systemKey, agent_key: agentKey }, images, editOf);
    } catch (error) {
      turn.failed = true;
      message.setFailed(error.message);
      this._settle(turn);
    }
  }

  /**
   * The Stop button: the first press lets the agent finish its current step,
   * a second press - or `now` - stops it at once. Either way the turn is
   * recorded and Continue resumes it.
   */
  stop({ now = false } = {}) {
    this.stopSpeaking();
    this._connection?.stop({ now });
  }

  /** Silence whatever is being read aloud, wherever it was started from. */
  stopSpeaking() {
    this._speech.stop();
  }

  /** Toggle reading one message aloud - what the speaker button calls. */
  toggleSpeech(messageId) {
    if (this._speech.isActive(messageId)) {
      this._speech.stop();
      return;
    }
    const message = this._transcript.get(messageId);
    if (message?.text) this._speech.speak(messageId, message.text);
  }

  /** Start a fresh conversation and clear the transcript. */
  async startConversation() {
    this._detach();
    this._voice?.cleanup();
    const { systemKey, agentKey } = this._store.get();
    const conversation = await api.createConversation({ system_key: systemKey, agent_key: agentKey });
    this._store.set({ contextId: conversation.id, rootId: conversation.id, resumable: false, hasAgentContext: false, compacting: false });
    this._transcript.clear();
    await this._refreshConversations();
  }

  /** Load a stored conversation, including its reasoning traces. */
  async openConversation(contextId) {
    if (!contextId) return;
    // Leaving a chat mid-turn only stops watching it: the turn runs on the
    // server and is replayed when the chat is opened again.
    this._detach();
    this._voice?.cleanup();
    const payload = await api.getConversation(contextId);
    // A conversation remembers what was pinned when it ran, including "nothing".
    this._store.set({
      contextId: payload.id,
      rootId: payload.root ?? payload.id,
      systemKey: payload.metadata?.system_key ?? null,
      agentKey: payload.metadata?.agent_key ?? null,
      hasAgentContext: Boolean(payload.metadata?.routed_agent),
      compacting: this._compactingId === payload.id,
    });
    this._transcript.render(payload.messages);
    this._store.set({ queue: payload.pending ?? [] });
    this._syncResumable();
    // The agent kept working while the page was away: pick the turn back up.
    if (payload.active_turn) await this._attachToRunningTurn(payload.id, payload.active_turn);
  }

  /** Stop following the turn on screen without stopping the agent. */
  _detach() {
    if (!this._activeTurn && !this._connection) return;
    this._connection?.close();
    this._connection = null;
    this._activeTurn = null;
    this._store.set({ streaming: false });
  }

  /** Forget a chat; if it is the one on screen, move to the newest other one. */
  async deleteConversation(contextId) {
    await api.deleteConversation(contextId);
    if (contextId === this._store.get().contextId) {
      this._detach();
      this._store.set({ contextId: null, hasAgentContext: false });
      this._transcript.clear();
    }
    await this._refreshConversations();
    const next = this._store.get().conversations[0];
    if (!this._store.get().contextId && next) await this.openConversation(next.id);
  }

  async renameConversation(contextId, title) {
    await api.renameConversation(contextId, title);
    await this._refreshConversations();
  }

  /** Attach to a turn already running on the server and replay it in place. */
  async _attachToRunningTurn(contextId, { message: userText, resumes, elapsed_ms: elapsedMs }) {
    const last = this._transcript.last();
    if (resumes) {
      if (last?.role !== "marker") this._transcript.add({ role: "user", kind: "continuation", timestamp: Date.now() - elapsedMs });
    } else if (!(last?.role === "user" && last.text === userText)) {
      this._transcript.add({ role: "user", content: userText, timestamp: Date.now() - elapsedMs });
    }
    this._transcript.retireContinue();
    const message = this._transcript.add({ role: "assistant", author: "Grid", timestamp: Date.now() - elapsedMs });
    message.setWaiting(true);
    message.reasoning.start(elapsedMs);
    this._transcript.scrollToBottom(true);
    this._store.set({ streaming: true });

    const turn = { message, contextId, failed: false, finalText: "", spoken: false };
    this._activeTurn = turn;
    try {
      this._connection = await ChatConnection.open(contextId);
      this._bind(this._connection, turn);
      this._connection.attach();
    } catch (error) {
      turn.failed = true;
      message.setFailed(error.message);
      this._settle(turn);
    }
  }

  // -- streaming ---------------------------------------------------------
  _bind(connection, turn) {
    const { message } = turn;
    connection
      .on("token", ({ content }) => {
        message.appendText(content);
        this._transcript.follow();
      })
      // The streamed text was narration before an action; the trace now holds it.
      .on("answer_reset", () => message.setText(""))
      .on("final_output", ({ content }) => {
        turn.finalText = content;
        message.setText(content);
        this._transcript.follow();
      })
      .on("routed", ({ agent_name: agentName }) => message.setAuthor(agentName))
      // A message sent while the agent works: how it is delivered, the queue,
      // and - delivered at a step - its place in the thread.
      .on("delivery", ({ delivery, decided_by: decidedBy }) => {
        const how = { now: "The agent stops and takes it now", next_step: "The agent reads it at its next step", after_turn: "It waits until the agent finishes" }[delivery];
        toast(decidedBy === "user" ? how : `${how} (decided: ${decidedBy})`, { timeout: 4000 });
      })
      .on("queue", ({ items }) => this._store.set({ queue: items }))
      .on("steered", ({ text, images }) => {
        this._transcript.addBefore(message, { role: "user", content: text, images: images ?? [], timestamp: Date.now() });
      })
      // An image the model generated, shown as soon as it arrives.
      .on("image", ({ url }) => {
        message.addImage(url);
        this._transcript.follow();
      })
      // The full list is a step in the trace; the toast makes sure it is seen once.
      .on("tool_issues", ({ system, agent, agent_name: agentName, summary }) => {
        const key = `${system}/${agent}`;
        if (this._warnedAgents.has(key)) return;
        this._warnedAgents.add(key);
        toast(`${agentName}: ${summary.length} tool problem(s), calls may fail. ${summary[0]}`, { tone: "error", timeout: 12000 });
      })
      .on("step", ({ step }) => {
        message.reasoning.upsert(step);
        this._transcript.follow();
      })
      .on("step_removed", ({ id }) => message.reasoning.remove(id))
      .on("reasoning", ({ id, delta }) => {
        message.reasoning.appendReasoning(id, delta);
        this._transcript.follow();
      })
      // The first Stop was accepted: the agent is finishing its current step.
      .on("stopping", () => this._store.set({ stopping: true }))
      // The turn ended early and was recorded; Continue resumes it.
      .on("interrupted", ({ content, interruption }) => {
        turn.spoken = false;
        message.setInterrupted(interruption, { summary: content, resumable: interruption.resumable });
      })
      .on("error", ({ content }) => {
        turn.failed = true;
        turn.spoken = false;
        message.setFailed(content);
      })
      .on("busy", ({ content }) => {
        turn.failed = true;
        message.setFailed(content);
      })
      .on("done", ({ duration_ms: durationMs, stopped, detached, tokens_in: tokensIn, tokens_out: tokensOut }) => {
        if (detached) {
          // The turn finished before we could attach: show what was stored.
          turn.detached = true;
          this._settle(turn);
          return;
        }
        message.reasoning.finish(durationMs, this._transcript.isFollowing(), { tokensIn: tokensIn ?? 0, tokensOut: tokensOut ?? 0 });
        this._settle(turn);
        if (!turn.failed && !stopped && !message.text) {
          // The run ended on tool calls with no closing message. The trace above
          // shows what happened; the body must not just sit there empty.
          message.setNotice("The agent finished this turn without a written answer.");
          return;
        }
        if (turn.spoken && !turn.failed && !stopped && message.text) {
          this._speech.speak(message.id, message.text);
        }
      })
      .onClose(() => this._settle(turn));
  }

  /** Idempotent: `done` and the socket closing both land here. */
  _settle(turn) {
    if (!this.isStreaming || turn !== this._activeTurn) return;
    this._store.set({ streaming: false, stopping: false });
    turn.message.setWaiting(false);
    turn.message.reasoning.finish(undefined, this._transcript.isFollowing());
    this._transcript.follow();
    this._connection?.close();
    this._connection = null;
    this._activeTurn = null;
    turn.onSettled?.();
    // The user's own counts may have moved; the page refreshes its badge.
    document.dispatchEvent(new CustomEvent("grid:turn-settled"));
    this._syncResumable();
    void this._refreshConversations();
    // A failed turn stores no answer, so reconciling would re-render the
    // transcript without it and wipe the error the reader has to see.
    if (!turn.failed) void this._reconcile(turn.contextId, { force: turn.detached });
  }

  /**
   * The runtime may post-process a turn (memory writes, trimming). Re-render
   * only when the stored conversation no longer matches what is on screen, so
   * an expanded reasoning panel is not collapsed for no reason.
   */
  async _reconcile(contextId, { force = false } = {}) {
    if (contextId !== this._store.get().contextId) return;
    if (!this._transcript) return;
    try {
      const payload = await api.getConversation(contextId);
      // Same messages on screen: only take their ids and versions, so an
      // expanded reasoning panel stays as it is.
      if (force || !this._transcript.adopt(payload.messages)) {
        this._transcript.render(payload.messages);
      }
      this._store.set({ queue: payload.pending ?? [], hasAgentContext: Boolean(payload.metadata?.routed_agent) });
      this._syncResumable();
      // The queue started the next waiting message: follow that turn too.
      if (payload.active_turn && !this.isStreaming) await this._attachToRunningTurn(payload.id, payload.active_turn);
    } catch (error) {
      console.warn("Could not reconcile the conversation", error);
    }
  }

  /** Offer Continue in the composer while the last turn can still be resumed. */
  _syncResumable() {
    this._store.set({ resumable: !this.isStreaming && this._transcript.resumable() });
  }

  /** Display name of a pinned agent, or null when the router decides. */
  _pinnedAgentName(systemKey, agentKey) {
    if (!agentKey) return null;
    const system = this._store.get().systems.find((item) => item.key === systemKey);
    return system?.agents.find((agent) => agent.key === agentKey)?.name ?? agentKey;
  }
}
