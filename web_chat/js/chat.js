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
    this._store.set({ streaming: true });

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
      message.setFailed(error.message);
      toast(error.message, { tone: "error" });
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
    if (this.isStreaming) return;
    this._voice?.cleanup();
    const { systemKey, agentKey } = this._store.get();
    const conversation = await api.createConversation({ system_key: systemKey, agent_key: agentKey });
    this._store.set({ contextId: conversation.id });
    this._transcript.clear();
    await this._refreshConversations();
  }

  /** Load a stored conversation, including its reasoning traces. */
  async openConversation(contextId) {
    if (!contextId || this.isStreaming) return;
    this._voice?.cleanup();
    const payload = await api.getConversation(contextId);
    // A conversation remembers what was pinned when it ran, including "nothing".
    this._store.set({
      contextId: payload.id,
      systemKey: payload.metadata?.system_key ?? null,
      agentKey: payload.metadata?.agent_key ?? null,
    });
    this._transcript.render(payload.messages);
  }

  // -- streaming ---------------------------------------------------------
  _bind(connection, turn) {
    const { message } = turn;
    connection
      .on("token", ({ content }) => {
        message.appendText(content);
        this._transcript.follow();
      })
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
        toast(content, { tone: "error" });
      })
      .on("busy", ({ content }) => {
        turn.failed = true;
        message.setFailed(content);
        toast(content, { tone: "error" });
      })
      .on("done", ({ duration_ms: durationMs, stopped }) => {
        message.reasoning.finish(durationMs);
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
    if (!this.isStreaming) return;
    this._store.set({ streaming: false });
    turn.message.setWaiting(false);
    turn.message.reasoning.finish();
    this._connection?.close();
    this._connection = null;
    this._activeTurn = null;
    void this._refreshConversations();
    void this._reconcile(turn.contextId);
  }

  /**
   * The runtime may post-process a turn (memory writes, trimming). Re-render
   * only when the stored conversation no longer matches what is on screen, so
   * an expanded reasoning panel is not collapsed for no reason.
   */
  async _reconcile(contextId) {
    if (contextId !== this._store.get().contextId) return;
    try {
      const payload = await api.getConversation(contextId);
      if (payload.messages.length !== this._transcript.count()) this._transcript.render(payload.messages);
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
