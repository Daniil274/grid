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
  }

  get isStreaming() {
    return this._store.get().streaming;
  }

  /** Text streamed so far for the turn in flight - the voice layer reads it. */
  get currentAnswer() {
    return this._activeTurn?.message.text ?? "";
  }

  /**
   * @param {string} rawText
   * @param {{spoken?: boolean}} [options] `spoken` marks a dictated turn, which
   *   is the only kind that reads its answer aloud without being asked to.
   */
  async send(rawText, { spoken = false } = {}) {
    const text = rawText?.trim();
    if (!text || this.isStreaming) return;
    this.stopSpeaking();

    if (!this._store.get().contextId) await this.startConversation();
    const { contextId, systemKey, agentKey } = this._store.get();

    this._transcript.add({ role: "user", content: text, timestamp: Date.now() });
    const message = this._transcript.add({
      role: "assistant",
      author: this._pinnedAgentName(systemKey, agentKey) ?? "Routing…",
      timestamp: Date.now(),
    });
    message.setWaiting(true);
    message.reasoning.start();
    this._transcript.follow();
    this._store.set({ streaming: true });
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
      this._connection.send(text, { system_key: systemKey, agent_key: agentKey });
    } catch (error) {
      turn.failed = true;
      message.setFailed(error.message);
      this._settle(turn);
    }
  }

  stop() {
    this.stopSpeaking();
    this._connection?.stop();
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
    this._store.set({ contextId: conversation.id });
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
      systemKey: payload.metadata?.system_key ?? null,
      agentKey: payload.metadata?.agent_key ?? null,
    });
    this._transcript.render(payload.messages);
    // The agent kept working while the page was away: pick the turn back up.
    if (payload.active_turn) await this._resume(payload.id, payload.active_turn);
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
      this._store.set({ contextId: null });
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
  async _resume(contextId, { message: userText, elapsed_ms: elapsedMs }) {
    const last = this._transcript.last();
    if (!(last?.role === "user" && last.text === userText)) {
      this._transcript.add({ role: "user", content: userText, timestamp: Date.now() - elapsedMs });
    }
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
      .on("step", ({ step }) => {
        message.reasoning.upsert(step);
        this._transcript.follow();
      })
      .on("step_removed", ({ id }) => message.reasoning.remove(id))
      .on("reasoning", ({ id, delta }) => {
        message.reasoning.appendReasoning(id, delta);
        this._transcript.follow();
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
      .on("done", ({ duration_ms: durationMs, stopped, detached }) => {
        if (detached) {
          // The turn finished before we could attach: show what was stored.
          turn.detached = true;
          this._settle(turn);
          return;
        }
        message.reasoning.finish(durationMs, this._transcript.isFollowing());
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
    this._store.set({ streaming: false });
    turn.message.setWaiting(false);
    turn.message.reasoning.finish(undefined, this._transcript.isFollowing());
    this._transcript.follow();
    this._connection?.close();
    this._connection = null;
    this._activeTurn = null;
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
    try {
      const payload = await api.getConversation(contextId);
      if (force || payload.messages.length !== this._transcript.count()) this._transcript.render(payload.messages);
    } catch (error) {
      console.warn("Could not reconcile the conversation", error);
    }
  }

  /** Display name of a pinned agent, or null when the router decides. */
  _pinnedAgentName(systemKey, agentKey) {
    if (!agentKey) return null;
    const system = this._store.get().systems.find((item) => item.key === systemKey);
    return system?.agents.find((agent) => agent.key === agentKey)?.name ?? agentKey;
  }
}
