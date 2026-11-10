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

/**
 * How long a burst of "the page is back" events - hidden to visible, the page
 * restored from the back/forward cache, the network returning - waits for its
 * last member before one resync runs. They arrive together; one attach is enough.
 */
const RESUME_DEBOUNCE_MS = 200;
/** How many times a resync retries a conversation it could not read. */
const RESUME_RETRIES = 2;
/** Base delay between those retries; the second waits twice as long. */
const RESUME_RETRY_MS = 300;

/**
 * Revision frames carry a machine code (`revision.*`); the text is only for
 * people and is never what the client decides on.
 */
const REVISION_CODE_PREFIX = "revision.";
const isRevisionCode = (code) => typeof code === "string" && code.startsWith(REVISION_CODE_PREFIX);

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
    // The chat being loaded: a newer pick aborts it, and a late answer for a
    // chat no longer on screen is dropped instead of drawn over the new one.
    this._loading = null;
    this._navigationIntent = 0;
    // The resync a burst of wake-up events scheduled (see resumeLive).
    this._resume = null;
    // Which conversation read is the newest one. Reads run concurrently (a
    // reconcile of a finished turn, a resync, a post-processing pass) and the
    // older answer must never be drawn over the newer screen.
    this._syncSeq = 0;
    this._syncAbort = null;
    // The answer bubble of a turn that is no longer followed - dropped for a
    // resync, or settled before its answer was stored. The stored conversation
    // does not have it yet, so the next read must adopt it rather than erase it.
    this._liveAnswer = null;
    // Where the reader was in each chat this page has visited: a chat opens
    // where they left it, not at whatever position the last one was scrolled to.
    this._places = new Map();
    // The chat the transcript on screen really shows. A place belongs to the
    // chat whose messages are on screen, and to no other: an open that is still
    // fetching has already claimed a chat id without having drawn anything.
    this._renderedId = null;
    this._listenForLifecycle();
  }

  /**
   * The page can sleep, lose its network or be restored from the back/forward
   * cache without the turn's socket ever saying so. Every way back is one
   * resync; the resync itself is debounced, so a burst of them is one attach.
   */
  _listenForLifecycle() {
    const page = globalThis.window ?? globalThis;
    const onVisible = () => {
      if (globalThis.document?.visibilityState === "visible") void this.resumeLive();
    };
    const wake = () => void this.resumeLive();
    try {
      globalThis.document?.addEventListener?.("visibilitychange", onVisible);
      page?.addEventListener?.("pageshow", wake);
      page?.addEventListener?.("online", wake);
    } catch {
      // A host without page events (a test, a worker) simply never resumes.
    }
  }

  /**
   * The page came back: stop following the turn on screen - without finalising
   * it - and attach to the conversation again. A turn still running on the
   * server is rebuilt from its snapshot; one that finished meanwhile shows what
   * was stored. Debounced: the wake-up events arrive as a burst.
   *
   * The burst belongs to the chat the reader was in when it was scheduled, and
   * to that navigation: waking up is not a reason to read a chat they have
   * already left. A chat switched before the timer fires cancels the burst
   * (`_detach`); one that is somehow still pending is refused by its identity.
   * @returns {Promise<void>} settled once the resync (if any) has run
   */
  resumeLive(delay = RESUME_DEBOUNCE_MS) {
    const { contextId } = this._store.get();
    if (!contextId) return Promise.resolve();
    if (!this._resume) {
      const pending = { timer: null, resolve: null };
      pending.promise = new Promise((resolve) => { pending.resolve = resolve; });
      this._resume = pending;
    }
    const pending = this._resume;
    // What this resync is about: the chat on screen, the navigation that put
    // it there, and the turn it follows. Each of them is checked when the
    // burst fires - the page was asleep, the reader was not. A reused burst is
    // re-bound to what is on screen now: a wake after the turn changed is a
    // reason to reconcile the new turn, not the one the burst was first set for.
    pending.contextId = contextId;
    pending.intent = this._navigationIntent;
    pending.turn = this._activeTurn;
    clearTimeout(pending.timer);
    pending.timer = setTimeout(() => {
      const { resolve } = pending;
      this._resume = null;
      resolve(this._resyncLive(pending));
    }, delay);
    return pending.promise;
  }

  /**
   * Drop the resync a burst scheduled, if any: its chat is no longer the one on
   * screen, so its read, its attach and its drop of a live turn belong to
   * nobody. Whoever awaits the burst is released, not left hanging.
   */
  _cancelResume() {
    const pending = this._resume;
    if (!pending) return;
    this._resume = null;
    clearTimeout(pending.timer);
    pending.resolve?.();
  }

  /** One resync: drop the turn, then reconcile - retried while it cannot be read. */
  async _resyncLive({ turn = null, contextId = null, intent = null } = {}) {
    // The burst is about one chat, seen under one navigation. A chat opened
    // meanwhile has its own read - and its own turn, which must not be dropped
    // or drawn over by a resync the reader triggered in the one before it.
    if (!this._ownsResync(contextId, intent)) return;
    // A newer turn is on screen: the burst was about the one before it.
    if (this._activeTurn && this._activeTurn !== turn) return;
    this._dropLiveTurn();
    for (let attempt = 0; attempt <= RESUME_RETRIES; attempt += 1) {
      if (attempt) await new Promise((resolve) => setTimeout(resolve, RESUME_RETRY_MS * attempt));
      if (!this._ownsResync(contextId, intent)) return; // the reader moved on
      // Reconciling attaches to the server's running turn, or shows what was stored.
      if (await this._reconcile(contextId)) return;
    }
  }

  /**
   * Is *contextId* - seen under navigation `intent` - still what the screen
   * shows? A null context means the caller did not bind itself to one.
   */
  _ownsResync(contextId, intent) {
    const current = this._store.get();
    if (!current.contextId) return false;
    if (contextId !== null && current.contextId !== contextId) return false;
    return intent === null || intent === this._navigationIntent;
  }

  /**
   * Stop following the turn on screen, keeping everything it drew. Nothing is
   * finalised: the answer, its trace and its status come from the server when
   * the page attaches again.
   */
  _dropLiveTurn() {
    const turn = this._activeTurn;
    this._connection?.close();
    this._connection = null;
    this._activeTurn = null;
    // The bubble stays on screen: the read that follows keeps it (the stored
    // conversation has no answer yet) and the attach refills it from the
    // snapshot. Dropping it here would blank the answer until the round trip.
    if (turn?.message && !turn.message.messageId) {
      this._liveAnswer = { contextId: turn.contextId, message: turn.message, settled: Boolean(turn.done) };
    }
    this._store.set({ streaming: false, stopping: false });
    // Whoever waits for this turn to settle (Stop, an edit) must not hang.
    turn?.onSettled?.();
  }

  /** The turn's socket cannot carry a frame any more: resync to the server. */
  _lostTurn() {
    toast("The connection to this turn was lost. Reconnecting…", { tone: "error", timeout: 4000 });
    void this.resumeLive();
  }

  /**
   * Fetch a conversation for *contextId*; null when another load or chat took
   * over meanwhile. A pick aborts the load before it (`supersede`).
   * `signal` ties the read to its caller - a reconcile that a newer one
   * replaced aborts its own request instead of leaving it to answer late.
   */
  async _fetchConversation(contextId, { supersede = false, signal = null } = {}) {
    if (supersede) this._loading?.abort();
    const load = new AbortController();
    const forwardAbort = () => load.abort();
    if (signal) {
      if (signal.aborted) return null;
      signal.addEventListener?.("abort", forwardAbort, { once: true });
    }
    if (supersede) this._loading = load;
    try {
      const payload = await api.getConversation(contextId, load.signal);
      const current = this._store.get().contextId === contextId && (!supersede || this._loading === load);
      return current ? payload : null;
    } catch (error) {
      if (load.signal.aborted && error?.name === "AbortError") return null;
      if (supersede && (this._loading !== load || this._store.get().contextId !== contextId)) return null;
      throw error;
    } finally {
      signal?.removeEventListener?.("abort", forwardAbort);
      if (this._loading === load) this._loading = null;
    }
  }

  get isStreaming() {
    return this._store.get().streaming;
  }

  async reconnectAfterRestart() {
    const contextId = this._store.get().contextId;
    // The settle's own reconcile would race the forced one below for the same
    // conversation: one read and one attach is what a restart needs.
    if (this._activeTurn) this._settle(this._activeTurn, { reconcile: false });
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
   *   being asked to; `images` and `audio` are attachment data URLs.
   */
  async send(rawText, { spoken = false, images = [], delivery = null, audio = null } = {}) {
    if (this._store.get().compacting) return;
    const text = rawText?.trim() ?? "";
    if (!text && !images.length && !audio) return;
    if (this.isStreaming) {
      if (audio) {
        toast("Wait for the current turn before sending audio.", { tone: "error" });
        return;
      }
      // The agent is working: the server delivers the message now, at the
      // agent's next step or after the turn; `delivery` null lets it decide.
      if (!this._connection?.send(text, this._selection(), images, null, delivery, audio)) this._lostTurn();
      return;
    }
    await this._begin({ text, spoken, images, audio });
  }

  /** Send a waiting message right away. */
  async sendQueued(item) {
    if (this._store.get().compacting) return;
    if (this.isStreaming) {
      if (!this._connection?.queued("send_queued", item.id)) this._lostTurn();
      return;
    }
    await this._queuedWhileIdle("unqueue", item.id);
    await this.send(item.text, { images: item.images ?? [] });
  }

  /** Drop a waiting message. */
  async dropQueued(item) {
    if (this.isStreaming) {
      if (!this._connection?.queued("unqueue", item.id)) this._lostTurn();
    }
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
    const intent = ++this._navigationIntent;
    try {
      if (this._store.get().compacting) throw new Error("Wait for context compaction to finish before editing.");
      if (this.isStreaming) await this._stopNow();
      if (intent !== this._navigationIntent) return;
      const branch = await api.createBranch(this._store.get().contextId, id);
      if (intent !== this._navigationIntent) return;
      // The new version runs with the agent picked now, not the one the
      // branch inherited from the original - often the reason for the edit.
      await this.openConversation(branch.id, intent, { keepSelection: true });
      if (intent !== this._navigationIntent) return;
      await this._begin({ text, spoken: false, images, editOf: branch.edit_of });
    } catch (error) {
      if (intent !== this._navigationIntent) return;
      toast(error.message, { tone: "error" });
    }
  }

  /**
   * Delete a stored message from the chat and from what the agent remembers.
   * A message the user sent takes its whole turn with it - the answer too.
   * @param {{id: string, role: string}} target
   */
  async deleteMessage({ id, role }) {
    const { contextId, compacting } = this._store.get();
    if (!contextId) return;
    if (this.isStreaming || compacting) {
      toast("Stop the running turn before deleting a message.", { tone: "error" });
      return;
    }
    const question = role === "user"
      ? "Delete this message and the agent's answer to it? The agent forgets that turn too."
      : "Delete this message? The agent forgets it too.";
    if (!window.confirm(question)) return;
    try {
      const result = await api.deleteMessage(contextId, id);
      await this._reconcile(contextId, { force: true });
      void this._refreshConversations();
      toast(
        result.context ? "Deleted from the chat and the agent's context." : "Deleted from the chat; the agent's context had no exact copy of it (summarized or older).",
        { tone: result.context ? "success" : "info", timeout: 4000 },
      );
    } catch (error) {
      toast(error.message, { tone: "error" });
    }
  }

  /**
   * Bring the workspace back to before the turn of a message: every file that
   * turn or a later one changed - by any tool - as it was then.
   * @param {{turn_id: string, files: number}} checkpoint
   */
  async restoreCheckpoint({ turn_id: turnId, files }) {
    const { contextId, compacting } = this._store.get();
    if (!contextId) return;
    if (this.isStreaming || compacting) {
      toast("Stop the running turn before restoring files.", { tone: "error" });
      return;
    }
    const question = `Restore the workspace to before this message? Files changed by this turn (${files}) `
      + "and by the turns after it go back to how they were; files created since are removed. The chat stays as it is.";
    if (!window.confirm(question)) return;
    try {
      const { restored } = await api.restoreCheckpoint(contextId, turnId);
      toast(restored.length ? `Restored ${restored.length} file(s).` : "Nothing to restore: the files are as they were.", {
        tone: "success", timeout: 4000,
      });
    } catch (error) {
      toast(error.message, { tone: "error" });
    }
  }

  /** Open a new chat that is this one up to the end of message *id*'s turn. */
  async forkAt(id) {
    const { contextId, compacting } = this._store.get();
    if (!contextId) return;
    if (this.isStreaming || compacting) {
      toast("Stop the running turn before forking.", { tone: "error" });
      return;
    }
    const intent = ++this._navigationIntent;
    try {
      const fork = await api.forkConversation(contextId, id);
      if (intent !== this._navigationIntent) return;
      await this.openConversation(fork.id, intent, { keepSelection: true });
      if (intent !== this._navigationIntent) return;
      await this._refreshConversations();
      toast("Forked into a new chat.", { tone: "success" });
    } catch (error) {
      if (intent === this._navigationIntent) toast(error.message, { tone: "error" });
    }
  }

  /**
   * Carry this chat's context into a new one and open it: the server seeds the
   * new chat with the plan ("plan") or the tail ("tail") of the chat on screen.
   */
  async continueInNewChat(strategy = "plan") {
    const { contextId, compacting } = this._store.get();
    if (!contextId) return;
    if (this.isStreaming || compacting) {
      toast("Stop the running turn before continuing in a new chat.", { tone: "error" });
      return;
    }
    const intent = ++this._navigationIntent;
    try {
      const next = await api.continueConversation(contextId, strategy);
      if (intent !== this._navigationIntent) return;
      await this.openConversation(next.id, intent, { keepSelection: true });
      if (intent !== this._navigationIntent) return;
      await this._refreshConversations();
      toast("Continued in a new chat.", { tone: "success" });
    } catch (error) {
      if (intent === this._navigationIntent) toast(error.message, { tone: "error" });
    }
  }

  /**
   * Show the plan a chat's turns saved, when it opens again - so the card is
   * not blank until the next turn re-sends a live plan step. Best effort: an
   * older server without the route simply leaves the card as it was.
   */
  async _showStoredPlan(contextId) {
    try {
      const plan = await api.conversationPlan(contextId);
      if (this._store.get().contextId !== contextId) return;
      // A plan an answer on screen already shows needs no second card.
      if (this._transcript.hasPlan()) return;
      this._transcript.lastAnswer()?.reasoning?.applyStoredPlan(plan);
    } catch {
      // The chat's stored plan is a convenience; the live plan still arrives.
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
    const intent = ++this._navigationIntent;
    try {
      await api.activateBranch(contextId);
      if (intent !== this._navigationIntent) return;
      await this.openConversation(contextId, intent);
      if (intent === this._navigationIntent) void this._refreshConversations();
    } catch (error) {
      if (intent === this._navigationIntent) toast(error.message, { tone: "error" });
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
  async _begin({ text, spoken, images, audio = null, editOf = null }) {
    this.stopSpeaking();

    if (!this._store.get().contextId) await this.startConversation();
    const { contextId, systemKey, agentKey } = this._store.get();

    // Only the newest interruption can be continued, and only until now.
    this._transcript.retireContinue();
    this._store.set({ resumable: false });
    // The earlier answers' plans fold away; this turn's card opens with its plan.
    this._transcript.collapsePlans();
    if (text === null) this._transcript.add({ role: "user", kind: "continuation", timestamp: Date.now() });
    else this._transcript.add({ role: "user", content: text || (audio ? "Голосовое сообщение" : ""), images, audio, timestamp: Date.now() });
    const message = this._transcript.add({
      role: "assistant",
      author: this._pinnedAgentName(systemKey, agentKey) ?? (text === null ? "Grid" : "Routing…"),
      timestamp: Date.now(),
    });
    message.setWaiting(true);
    message.reasoning.start();
    message.reasoning.setStepSource(this._liveStepSource(contextId, message));
    this._transcript.follow();
    this._store.set({ streaming: true, stopping: false });
    void this._refreshConversations(); // the rail shows this chat as working

    // A dictated turn reads its answer back once the answer is settled. It
    // cannot read the token stream as it arrives: `run_streamed` emits every
    // assistant message of the run, including the narration before each tool
    // call, and only the last one survives into `final_output`. Speaking the
    // stream would read aloud the steps the message itself discards.
    const turn = { message, contextId, failed: false, finalText: "", spoken, done: false };
    this._activeTurn = turn;
    // A turn that starts now owns the screen: an earlier answer is neither
    // kept nor adopted by the reads this turn will cause.
    this._liveAnswer = null;
    // The chat has a turn now: its working directory stays as it is.
    this._store.set({ workspaceLocked: true });
    try {
      this._connection = await ChatConnection.open(contextId);
      this._bind(this._connection, turn);
      if (text === null) {
        // Continue has no message to carry the switch: it goes first.
        const { policyFilter } = this._store.get();
        if (policyFilter) this._connection.policyFilter(policyFilter);
        this._connection.resume();
      } else if (!this._connection.send(text, this._selection(), images, editOf, null, audio)) {
        throw new Error("Chat connection closed before the message could be sent");
      }
    } catch (error) {
      turn.failed = true;
      message.setFailed(error.message);
      this._settle(turn);
    }
  }

  /**
   * Send a button of a presented interface as the user's next turn.
   *
   * The page sends ids, the values the person chose and the revisions it is
   * looking at; the server re-checks all of them against the stored document
   * and composes the request. A refusal arrives as a typed `error` frame and
   * settles the waiting answer like any other failed turn.
   *
   * @param {{messageId: string, uiId: string, actionId: string, documentRevision: number,
   *   stateRevision: number, values: object, requestId: string}} fields
   * @returns {Promise<boolean>} false when nothing was sent
   */
  async submitUiAction(fields) {
    const { contextId, streaming } = this._store.get();
    if (!contextId || streaming || this._activeTurn) {
      toast("Wait for the current turn to finish.", { timeout: 4000 });
      return false;
    }
    this.stopSpeaking();
    this._transcript.retireContinue();
    this._transcript.collapsePlans();
    const message = this._transcript.add({
      role: "assistant",
      author: "Grid",
      timestamp: Date.now(),
    });
    message.setWaiting(true);
    message.reasoning.start();
    message.reasoning.setStepSource(this._liveStepSource(contextId, message));
    this._transcript.follow();
    const turn = { message, contextId, failed: false, finalText: "", spoken: false, done: false, submitted: false };
    this._activeTurn = turn;
    this._liveAnswer = null;
    this._store.set({ streaming: true, stopping: false, workspaceLocked: true });
    try {
      this._connection = await ChatConnection.open(contextId);
      this._bind(this._connection, turn);
      if (!this._connection.uiAction(fields)) {
        throw new Error("Chat connection closed before the choice could be sent");
      }
    } catch (error) {
      turn.failed = true;
      message.setFailed(error.message);
      this._settle(turn);
      return false;
    }
    return true;
  }

  /** Where the next message runs, and under which policy filter. */
  _selection() {
    const { systemKey, agentKey, policyFilter, executionTarget, executionWorkdir } = this._store.get();
    // A chat on the server sends nothing: that is what the server does when the
    // metadata says nothing. A chat on a device names it, with its folder, so
    // the very first call of a freshly loaded page runs where the chip says.
    const onDevice = typeof executionTarget === "string" && executionTarget !== "" && executionTarget !== "cloud";
    return {
      system_key: systemKey,
      agent_key: agentKey,
      ...(policyFilter ? { policy_filter: policyFilter } : {}),
      ...(onDevice
        ? { execution_target: executionTarget, execution_workdir: executionWorkdir ?? "" }
        : {}),
    };
  }

  /** The policy switch moved: the next turns, and the one running now, follow it. */
  setPolicyFilter(filter) {
    this._store.set({ policyFilter: filter });
    if (this._connection && !this._connection.policyFilter(filter)) this._lostTurn();
  }

  /**
   * The user pointed the chat at the server or at one of their devices, in a
   * folder (`core/devices/execution.py`). The page shows the choice at once; the
   * server is told over the turn's socket when there is one - so a running turn
   * and its tools follow it from the next call - and over REST otherwise.
   */
  setExecutionTarget({ target, workdir = "" }) {
    const contextId = this._store.get().contextId;
    if (!contextId || typeof target !== "string") return;
    this._store.set({ executionTarget: target, executionWorkdir: workdir });
    if (this._connection?.executionTarget(target, workdir)) return;
    void this._pushExecutionTarget(contextId, target, workdir);
  }

  async _pushExecutionTarget(contextId, target, workdir) {
    try {
      const state = await api.setExecutionTarget(contextId, target, workdir);
      if (this._store.get().contextId === contextId) this._applyExecutionTarget(state);
    } catch (error) {
      toast(`Could not switch: ${error.message}`, { tone: "error" });
      // The choice was refused (a device that went away): show what is true.
      await this.refreshExecutionTarget(contextId);
    }
  }

  /** Where the chat on screen works, as the server has it: target, folder, devices. */
  async refreshExecutionTarget(contextId = this._store.get().contextId) {
    if (!contextId) return;
    try {
      const state = await api.executionTarget(contextId);
      if (this._store.get().contextId === contextId) this._applyExecutionTarget(state);
    } catch (error) {
      // An older server, a chat that is not there yet: the chips stay as they are.
      console.warn("Could not read where this chat works", error);
    }
  }

  /**
   * The server's answer about the target. It is the state afterwards, never an
   * echo of what was asked: a refused switch leaves the page on the truth.
   */
  _applyExecutionTarget(state) {
    if (!state || typeof state.target !== "string") return;
    this._store.set({
      executionTarget: state.target,
      executionWorkdir: typeof state.workdir === "string" ? state.workdir : "",
      ...(Array.isArray(state.devices) ? { devices: state.devices } : {}),
    });
  }

  /** Answer a call the policy holds in this chat for the user's permission. */
  answerReview(approvalId, { approve, remember = false }) {
    if (!this._connection) {
      toast("This request is no longer waiting for an answer.", { tone: "error" });
      return;
    }
    if (!this._connection.answerReview(approvalId, { approve, remember })) this._lostTurn();
  }

  /**
   * The Stop button: the first press lets the agent finish its current step,
   * a second press - or `now` - stops it at once. Either way the turn is
   * recorded and Continue resumes it.
   */
  stop({ now = false } = {}) {
    this.stopSpeaking();
    if (this._connection && !this._connection.stop({ now })) this._lostTurn();
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
    // The chat on screen is still empty and in the project asked for: it is
    // the new chat. Another one would only pile up beside it, never written in.
    const { contextId: openId, conversations = [], activeProject: wanted, streaming, workspaceLocked } = this._store.get();
    const open = conversations.find((conversation) => conversation.id === openId);
    if (open && !open.message_count && !open.active && !streaming && !workspaceLocked && (open.project ?? null) === (wanted ?? null)) {
      return;
    }
    const intent = ++this._navigationIntent;
    // A new chat replaces the one on screen: the reader's place in it is kept
    // for the next time they come back.
    const leaving = this._store.get().contextId;
    if (leaving) this._rememberPlace(leaving);
    this._detach();
    this._voice?.cleanup();
    const { systemKey, agentKey, activeProject } = this._store.get();
    const conversation = await api.createConversation({
      system_key: systemKey,
      agent_key: agentKey,
      project_id: activeProject ?? null,
    });
    if (intent !== this._navigationIntent) return;
    this._store.set({
      contextId: conversation.id, rootId: conversation.id, resumable: false, hasAgentContext: false, compacting: false,
      chatWorkspace: conversation.workspace ?? null, workspaceLocked: false, reference: null,
      // A chat that has not run yet works here: a device is chosen per chat.
      executionTarget: "cloud", executionWorkdir: "",
    });
    this._transcript.clear();
    // An empty transcript is this chat's: nothing to remember yet, and nothing
    // of the one it replaced.
    this._renderedId = conversation.id;
    await this._refreshConversations();
  }

  /**
   * Have the chat on screen work in directory `path`. A chat that already has
   * messages keeps its directory: a new chat opens in `path` instead.
   */
  async chooseWorkspace(path) {
    const { contextId, workspaceLocked } = this._store.get();
    if (!contextId || workspaceLocked) await this.startConversation();
    const id = this._store.get().contextId;
    const result = await api.setWorkspace(id, path);
    if (this._store.get().contextId === id) this._store.set({ chatWorkspace: result.workspace });
    await this._refreshConversations();
  }

  /**
   * Load a stored conversation, including its reasoning traces.
   * `keepSelection` keeps the agent picked on screen instead of the chat's own.
   */
  async openConversation(contextId, intent = null, { keepSelection = false } = {}) {
    if (!contextId) return;
    if (intent === null) intent = ++this._navigationIntent;
    // The place the reader has in this chat, remembered when they left it, is
    // what the chat opens at - a chat is not a fresh page every time.
    const leaving = this._store.get().contextId;
    if (leaving && leaving !== contextId) this._rememberPlace(leaving);
    const place = this._places.get(contextId) ?? null;
    // Leaving a chat mid-turn only stops watching it: the turn runs on the
    // server and is replayed when the chat is opened again.
    this._detach();
    this._voice?.cleanup();
    // The pick shows at once: the row is selected and the transcript says it
    // is loading, rather than the old chat staying on screen meanwhile.
    this._store.set({ contextId, rootId: contextId });
    this._transcript.setLoading(true);
    // An opening is a read like any other: it takes the newest generation.
    // A resync of this same chat that answers first is newer, and this older
    // answer must not draw over the snapshot it put on screen.
    const read = this._claimRead();
    let payload;
    try {
      payload = await this._fetchConversation(contextId, { supersede: true });
    } catch (error) {
      if (intent === this._navigationIntent && this._store.get().contextId === contextId) {
        this._transcript.setLoading(false);
        this._transcript.clear();
        this._renderedId = null;
        this._store.set({ resumable: false });
      }
      throw error;
    }
    if (!payload || !this._ownsRead(read)) {
      // Another navigation took over, or a newer read of this chat answered
      // first: the screen is not this answer's to draw. The newer read ends
      // the loading state when it settles the screen (see `_reconcile`).
      return;
    }
    this._transcript.setLoading(false);
    // A conversation remembers what was pinned when it ran, including "nothing".
    const selection = keepSelection
      ? {}
      : { systemKey: payload.metadata?.system_key ?? null, agentKey: payload.metadata?.agent_key ?? null };
    this._store.set({
      contextId: payload.id,
      rootId: payload.root ?? payload.id,
      ...selection,
      hasAgentContext: Boolean(payload.metadata?.routed_agent),
      chatWorkspace: payload.metadata?.workspace ?? null,
      // Where this chat works (core.factory.execution): its own choice, kept
      // with the chat, not the one the previous chat was on.
      executionTarget: payload.metadata?.execution_target || "cloud",
      executionWorkdir: payload.metadata?.execution_workdir ?? "",
      // A reference-only target keeps its source link on screen (ui/reference-banner.js).
      reference: payload.metadata?.migration?.action === "context.reference_bootstrap"
        ? payload.metadata.migration
        : null,
      workspaceLocked: payload.messages.length > 0 || Boolean(payload.active_turn),
      compacting: this._compactingId === payload.id,
      queue: payload.pending ?? [],
    });
    // From here on, the places read off the screen belong to this chat.
    this._renderedId = payload.id;
    this._transcript.render(payload.messages, { loadStep: this._stepLoader(payload.id), reveal: !place, place });
    this._syncResumable();
    // The saved plan of the chat, before any live plan step of a new turn.
    void this._showStoredPlan(payload.id);
    // The agent kept working while the page was away: pick the turn back up,
    // without moving a reader who is not at the end of a resumed chat.
    if (payload.active_turn && this._ownsRead(read)) {
      await this._attachToRunningTurn(payload.id, payload.active_turn, { reveal: !place });
    }
    // The devices a chat may switch to are the ones connected now: read once
    // the chat is on screen, so the target chip names them.
    void this.refreshExecutionTarget(payload.id);
  }

  /**
   * Remember where the reader is in *contextId*, so reopening that chat puts
   * them back there. Failing to read a place (no layout: a hidden pane) forgets
   * the old one instead of restoring a guess.
   */
  _rememberPlace(contextId) {
    // Only the chat on screen has a place to read: while another chat is still
    // being fetched, the id in the store is ahead of the transcript, and reading
    // the screen for it would hand this chat the position of the one before.
    if (contextId !== this._renderedId) return;
    const place = this._transcript.place?.() ?? null;
    this._places.delete(contextId);
    if (!place) return;
    this._places.set(contextId, place);
    // Bounded: a session reads a handful of chats, not an unbounded archive.
    while (this._places.size > 40) this._places.delete(this._places.keys().next().value);
  }

  /** Stop following the turn on screen without stopping the agent. */
  _detach() {
    // Another chat is taking the screen: a read still in flight for this one
    // must not draw its answer over it, its live bubble is gone with it, and a
    // resync it scheduled is not about the chat that is coming.
    this._liveAnswer = null;
    this._cancelResume();
    this._syncAbort?.abort();
    // The generation a read is checked against: a read that started before this
    // transition - even for the very chat being opened again (A -> B -> A) -
    // is older than the screen and must not be drawn.
    this._syncSeq += 1;
    if (!this._activeTurn && !this._connection) return;
    this._connection?.close();
    this._connection = null;
    this._activeTurn = null;
    this._store.set({ streaming: false });
  }

  /** Forget a chat; if it is the one on screen, move to the newest other one. */
  async deleteConversation(contextId) {
    const intent = ++this._navigationIntent;
    await api.deleteConversation(contextId);
    this._places.delete(contextId);
    if (intent !== this._navigationIntent) return;
    if (contextId === this._store.get().contextId) {
      this._detach();
      this._store.set({ contextId: null, hasAgentContext: false, reference: null });
      this._transcript.clear();
      this._renderedId = null;
    }
    await this._refreshConversations();
    const next = this._store.get().conversations[0];
    if (intent === this._navigationIntent && !this._store.get().contextId && next) await this.openConversation(next.open_id ?? next.id);
  }

  async renameConversation(contextId, title) {
    await api.renameConversation(contextId, title);
    await this._refreshConversations();
  }

  /**
   * Attach to a turn already running on the server and replay it in place.
   *
   * `reveal` is the explicit way in (a chat was just opened, or just sent):
   * the newest message is shown. A resync attaches without moving the reader,
   * who may be reading further up - and an answer this page was already
   * streaming keeps its text until the server's snapshot replaces it.
   */
  async _attachToRunningTurn(contextId, { message: userText, resumes, elapsed_ms: elapsedMs }, { reveal = true } = {}) {
    // One turn per chat. A second attach to a chat this page already follows
    // would stack another request and answer under it, open a second socket
    // and leave the first one open - the turn on screen already is that turn.
    if (this._activeTurn?.contextId === contextId) return;
    const last = this._transcript.last();
    // The answer this page was streaming into when it went away is this same
    // turn, still on screen: the snapshot refills that bubble instead of
    // stacking a second one under the request. A stored message has an id; a
    // live one does not yet.
    const rebuild = last?.role === "assistant" && !last.messageId ? last : null;
    if (!rebuild) {
      if (resumes) {
        if (last?.role !== "marker") this._transcript.add({ role: "user", kind: "continuation", timestamp: Date.now() - elapsedMs }, { reveal });
      } else if (!(last?.role === "user" && last.text === userText)) {
        this._transcript.add({ role: "user", content: userText, timestamp: Date.now() - elapsedMs }, { reveal });
      }
    }
    this._transcript.retireContinue();
    const message = rebuild
      ?? this._transcript.add({ role: "assistant", author: "Grid", timestamp: Date.now() - elapsedMs }, { reveal });
    message.setWaiting(true);
    message.reasoning.start(elapsedMs);
    message.reasoning.setStepSource(this._liveStepSource(contextId, message));
    if (reveal) this._transcript.scrollToBottom(true);
    // A following reader lands at the end at once, without waiting a frame.
    else this._transcript.scrollToBottom();
    this._store.set({ streaming: true });

    // The snapshot's token frame carries the whole answer so far, so it
    // replaces this bubble's text instead of being appended to it - but only
    // when it arrives: clearing it here would blank a reader's screen for the
    // round trip and drop the place they were reading.
    const turn = { message, contextId, failed: false, finalText: "", spoken: false, done: false, snapshot: Boolean(rebuild) };
    this._activeTurn = turn;
    // This turn owns the bubble now: a read that follows may adopt it in place.
    this._liveAnswer = null;
    try {
      const connection = await ChatConnection.open(contextId);
      // Another turn took the screen while the socket opened: this one's
      // frames belong to nobody, and leaving it open would stream into a bubble
      // the newer turn no longer shows.
      if (this._activeTurn !== turn) {
        connection.close();
        return;
      }
      this._connection = connection;
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
    /**
     * A frame is only this turn's while this turn is the one on screen. A
     * socket that was closed for a resync, a stop or a newer turn can still
     * deliver what was already in flight; painting it would put one turn's
     * text, steps or status into another's bubble.
     */
    const live = (handler) => (payload) => {
      if (this._activeTurn !== turn || this._connection !== connection) return;
      handler(payload);
    };
    /**
     * A content frame stops being this turn's once the server has refused it.
     * What was already in flight between `error` and `done` - text, reasoning,
     * a step - would otherwise be appended to an answer that reads as failed.
     */
    const liveContent = (handler) => live((payload) => {
      if (turn.failed) return;
      handler(payload);
    });
    connection
      .on("token", liveContent(({ content }) => {
        // An attaching page is sent the answer it missed as one frame: it
        // states what the bubble shows, so it replaces rather than repeats.
        if (turn.snapshot) {
          turn.snapshot = false;
          message.setText(content);
        } else message.appendText(content);
        this._transcript.follow();
      }))
      // The streamed text was narration before an action; the trace now holds it.
      .on("answer_reset", liveContent(() => message.setText("")))
      .on("final_output", liveContent(({ content }) => {
        turn.finalText = content;
        message.setText(content);
        this._transcript.follow();
      }))
      .on("routed", live(({ agent_name: agentName }) => message.setAuthor(agentName)))
      // The server's own count of the running turn: a page that attaches
      // starts its clocks from this frame, not from the history it read first.
      .on("attached", live(({ elapsed_ms: elapsedMs }) => message.reasoning.setElapsed(elapsedMs)))
      // The turn changed files: its request now offers Restore.
      .on("checkpoint", live(({ turn_id: turnId, files }) => {
        this._transcript.requestOf(message)?.setCheckpoint({ turn_id: turnId, files });
      }))
      // A message sent while the agent works: how it is delivered, the queue,
      // and - delivered at a step - its place in the thread.
      .on("delivery", ({ delivery, decided_by: decidedBy }) => {
        const how = { now: "The agent stops and takes it now", next_step: "The agent reads it at its next step", after_turn: "It waits until the agent finishes" }[delivery];
        toast(decidedBy === "user" ? how : `${how} (decided: ${decidedBy})`, { timeout: 4000 });
      })
      .on("queue", ({ items }) => this._store.set({ queue: items }))
      .on("policy_review", ({ ok }) => {
        if (!ok) toast("This request is no longer waiting for an answer.", { tone: "error" });
      })
      // Where this chat works, as the server now has it: the answer to a
      // switch, its devices included - whether the switch was accepted or not.
      .on("execution_target", (state) => this._applyExecutionTarget(state))
      .on("steered", live(({ text, images }) => {
        this._transcript.addBefore(message, { role: "user", content: text, images: images ?? [], timestamp: Date.now() });
      }))
      // An image the model generated, shown as soon as it arrives.
      .on("image", liveContent(({ url }) => {
        message.addImage(url);
        this._transcript.follow();
      }))
      // The answer presented a checked interface (web_chat/ui_document.py):
      // it joins the answer now, and is stored with it at the end of the turn.
      .on("ui", liveContent(({ ui }) => {
        message.addUiBlock?.(ui);
        this._transcript.follow();
      }))
      // A button of an interface became the user's turn. The sentence and its
      // provenance are the server's (composed from the stored document), so
      // the request bubble is added from this frame, never from local text.
      .on("ui_submitted", live(({ text: submitted, origin, duplicate }) => {
        // The server stored the values and, for a new press, started the turn:
        // tell the widget the revision it now holds and that its button is done.
        this._transcript.acknowledgeUi?.(origin?.message_id, origin?.ui_id, {
          stateRevision: origin?.state_revision,
          sent: true,
        });
        if (duplicate) {
          // This press was already honoured (a retry after a dropped socket):
          // the turn it started is elsewhere, so show nothing new.
          this._transcript.drop(turn.message);
          toast("This choice was already sent.", { timeout: 4000 });
          turn.duplicate = true;
          turn.done = true;
          this._settle(turn);
          return;
        }
        turn.submitted = true;
        this._transcript.addBefore(turn.message, {
          role: "user",
          content: submitted,
          origin: origin ?? null,
          timestamp: Date.now(),
        });
        this._transcript.follow();
      }))
      // A button press was refused (schema, stale, busy, or the turn could not
      // start). This socket exists for that one press, so settle it here; the
      // conversation's own running turn is not touched.
      .on("ui_refused", ({ content, message_id: messageId, ui_id: uiId, values, state_revision: stateRevision }) => {
        // A stale refusal also carries the values the server holds, so the
        // widget adopts them instead of keeping a choice that lost the race.
        this._transcript.acknowledgeUi?.(messageId, uiId, { failed: true, values, stateRevision });
        if (turn !== this._activeTurn) return;
        turn.failed = true;
        turn.spoken = false;
        turn.done = true;
        message.setFailed(content ?? "The choice was not sent.");
        this._settle(turn);
      })
      // The full list is a step in the trace; the toast makes sure it is seen once.
      .on("tool_issues", ({ system, agent, agent_name: agentName, summary }) => {
        const key = `${system}/${agent}`;
        if (this._warnedAgents.has(key)) return;
        this._warnedAgents.add(key);
        toast(`${agentName}: ${summary.length} tool problem(s), calls may fail. ${summary[0]}`, { tone: "error", timeout: 12000 });
      })
      .on("step", liveContent(({ step }) => {
        message.reasoning.upsert(step);
        // Narration and tool titles are public events too: the message keeps
        // them when the trace folds, the answer replaces the stream, or the
        // page attaches again and the stored events replay them.
        message.observeStep(step);
        this._transcript.follow();
      }))
      // A typed public event of the running turn, when the server sends one.
      .on("event", liveContent(({ event }) => message.applyEvent(event)))
      .on("step_removed", liveContent(({ id }) => message.reasoning.remove(id)))
      .on("reasoning", liveContent(({ id, delta }) => {
        message.reasoning.appendReasoning(id, delta);
        this._transcript.follow();
      }))
      // The first Stop was accepted: the agent is finishing its current step.
      .on("stopping", live(() => this._store.set({ stopping: true })))
      // The turn ended early and was recorded; Continue resumes it.
      .on("interrupted", live(({ content, interruption }) => {
        turn.spoken = false;
        message.setInterrupted(interruption, { summary: content, resumable: interruption.resumable });
      }))
      // The conversation was pinned to another Grid version, system
      // configuration or runtime and has been re-pinned to this one: the turn
      // goes on, so this is a quiet line, not an error. Keyed on the typed
      // code (`revision.*`), never on the sentence.
      .on("revision_notice", live(({ code, content }) => {
        message.addRevisionNotice({ code, content });
      }))
      .on("error", live(({ content, code }) => {
        turn.failed = true;
        turn.spoken = false;
        // A real incompatibility keeps the turn refused; the client can still
        // carry the conversation into a new chat on the current configuration.
        if (isRevisionCode(code)) {
          message.setRevisionRefusal(content, {
            code,
            onContinue: () => void this.continueInNewChat("tail"),
          });
        } else message.setFailed(content);
      }))
      .on("busy", live(({ content }) => {
        turn.failed = true;
        message.setFailed(content);
      }))
      .on("done", live(({ duration_ms: durationMs, stopped, detached, tokens_in: tokensIn, tokens_out: tokensOut }) => {
        // The turn is over. Only this frame says so: a socket that goes quiet
        // leaves the turn as the server still has it, to be attached again.
        turn.done = true;
        if (detached) {
          // The turn finished before we could attach: show what was stored.
          turn.detached = true;
          this._settle(turn);
          return;
        }
        message.reasoning.finish(durationMs, this._transcript.isFollowing(), { tokensIn: tokensIn ?? 0, tokensOut: tokensOut ?? 0 });
        turn.finished = true;
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
      }))
      .onClose(() => this._settle(turn));
  }

  /** Idempotent: `done` and the socket closing both land here. */
  _settle(turn, { reconcile = true } = {}) {
    if (!this.isStreaming || turn !== this._activeTurn) return;
    this._store.set({ streaming: false, stopping: false });
    turn.message.setWaiting(false);
    // No `done` and the socket closed anyway: the turn may still be running
    // on the server, so its trace stays live until the page attaches again
    // (resumeLive) and either finds it running or finds it stored.
    if (turn.done && !turn.finished) {
      turn.finished = true;
      turn.message.reasoning.finish(undefined, this._transcript.isFollowing());
    }
    this._transcript.follow();
    this._connection?.close();
    this._connection = null;
    this._activeTurn = null;
    // The answer was streamed here, not read from the server: the stored
    // conversation has it only once the run returned. The read that follows
    // keeps this bubble instead of redrawing the thread without it.
    if (!turn.failed && !turn.message.messageId) {
      this._liveAnswer = { contextId: turn.contextId, message: turn.message, settled: Boolean(turn.done) };
    }
    turn.onSettled?.();
    // The user's own counts may have moved; the page refreshes its badge.
    document.dispatchEvent(new CustomEvent("grid:turn-settled"));
    // The newest answer now offers "Continue in new chat"; earlier ones do not.
    this._transcript.refreshContinueInNewChat?.();
    this._syncResumable();
    void this._refreshConversations();
    // A failed turn stores no answer, so reconciling would re-render the
    // transcript without it and wipe the error the reader has to see.
    if (reconcile && !turn.failed) void this._reconcile(turn.contextId, { force: turn.detached });
  }

  /**
   * Take the generation of a read that may draw on screen, aborting the older
   * one. Reads run concurrently - an opening, a resync, a reconcile after a
   * turn - and the screen belongs to the newest of them, under the navigation
   * it was started in (`_ownsRead`). `_detach` moves the generation on as well,
   * so an answer from before a transition is never drawn.
   * @param {?AbortController} abort the caller's own request, if it has one
   * @returns {{seq: number, intent: number, contextId: ?string}} the read
   */
  _claimRead(abort = null) {
    this._syncAbort?.abort();
    this._syncAbort = abort;
    return {
      seq: ++this._syncSeq,
      intent: this._navigationIntent,
      contextId: this._store.get().contextId,
    };
  }

  /** Is *read* still the newest one, for the chat and navigation on screen? */
  _ownsRead(read) {
    return read.seq === this._syncSeq
      && read.intent === this._navigationIntent
      && read.contextId === this._store.get().contextId;
  }

  /**
   * The runtime may post-process a turn (memory writes, trimming). Re-render
   * only when the stored conversation no longer matches what is on screen, so
   * an expanded reasoning panel is not collapsed for no reason.
   *
   * A read never draws over a turn this page is still following: the stored
   * conversation cannot contain its answer yet, so rendering that history
   * would erase the live one. Reads also race each other - a newer read makes
   * an older answer stale, and the older one is dropped rather than drawn.
   * A read is also older than any transition: the chat it asked for may be the
   * one on screen again (A -> B -> A) without being the same screen.
   * @returns {Promise<?object>} the conversation read, or null when this read
   *   did not settle the screen - unreadable, numbered out by a newer read, or
   *   overtaken by a transition. Null is what makes a resync retry.
   */
  async _reconcile(contextId, { force = false } = {}) {
    if (contextId !== this._store.get().contextId) return null;
    if (!this._transcript) return null;
    const abort = new AbortController();
    // This read is the newest one: it supersedes the one before it, and it is
    // bound to the navigation it started in. The chat id alone cannot tell A
    // from A-after-B: the reader may come back to the same chat while this
    // answer is still on its way.
    const read = this._claimRead(abort);
    const turnAtRead = this._activeTurn;
    try {
      // Null when the reader moved to another chat while it loaded.
      const payload = await this._fetchConversation(contextId, { signal: abort.signal });
      if (!payload) return null;
      // A newer read took over while this one was in flight; or the screen
      // went through another navigation and came back to this chat; or it is
      // another chat now. Either way this answer predates the screen it would
      // be drawn on. Not a reason to call the screen settled: if that newer
      // read fails, a resync retries.
      if (!this._ownsRead(read)) return null;
      // The newest read settles this chat: whatever an opening of it was
      // showing as loading is over, drawn or attached by this pass.
      this._transcript.setLoading(false);
      // The turn on screen already has everything; its history cannot add to
      // it, and drawing the stored thread would cut the answer off mid-word.
      if (this._activeTurn?.contextId === contextId) {
        this._store.set({ queue: payload.pending ?? [], hasAgentContext: Boolean(payload.metadata?.routed_agent) });
        return payload;
      }
      // A turn that was on screen when the read started ended meanwhile: its
      // own settle reconciles, with a read newer than this one.
      if (turnAtRead) return payload;
      // The answer this page streamed is not in the stored conversation (a
      // running turn, or a write that has not landed yet): it is adopted in
      // place, not redrawn from a history that lacks it.
      const keep = this._answerToKeep(contextId, payload);
      // Same messages on screen: only take their ids and versions, so an
      // expanded reasoning panel stays as it is.
      if (keep || force || !this._transcript.adopt(payload.messages)) {
        // A resync is not an opening: the reader keeps their place unless the
        // stream was being followed (a following reader still lands at the end).
        this._renderedId = payload.id;
        this._transcript.render(payload.messages, { loadStep: this._stepLoader(payload.id), reveal: false, keep });
      }
      // The stored conversation now shows its own answer (or never will): the
      // bubble that was held on screen is not needed any more.
      if (!keep) this._liveAnswer = null;
      this._store.set({ queue: payload.pending ?? [], hasAgentContext: Boolean(payload.metadata?.routed_agent) });
      this._transcript.refreshContinueInNewChat?.();
      this._syncResumable();
      // The queue started the next waiting message: follow that turn too.
      if (payload.active_turn && !this.isStreaming) await this._attachToRunningTurn(payload.id, payload.active_turn, { reveal: false });
      return payload;
    } catch (error) {
      if (error?.name === "AbortError") return null; // a newer read took over
      console.warn("Could not reconcile the conversation", error);
      return null;
    } finally {
      if (this._syncAbort === abort) this._syncAbort = null;
    }
  }

  /**
   * The live answer bubble a read must carry over, or null when the stored
   * conversation already has the answer.
   *
   * The server writes the user's request when a turn starts and the answer
   * when the run returns (`core/factory/turns.py`): `run_output_text` always
   * hands one back, an explicit note when the run ended on tool calls, so a
   * returned run stores an assistant message. Only a stopped or interrupted
   * run stores none - and a failed turn is not reconciled at all.
   *
   * The bubble is identified by the turn the server is still running
   * (`active_turn`), not by the role of the last stored message: a write may
   * simply not have landed yet, and then the thread still ends on the request.
   */
  _answerToKeep(contextId, payload) {
    const kept = this._liveAnswer;
    if (!kept || kept.contextId !== contextId) return null;
    const handle = kept.message;
    // Adopted into the stored thread, or no longer the trailing message.
    if (handle.messageId || this._transcript.last() !== handle) return null;
    // The turn is still running: the stored conversation cannot hold its
    // answer, and the snapshot that follows fills this bubble.
    if (payload.active_turn) return handle;
    // No turn runs: the answer is stored - the run's output, or the note it
    // ended with. Only a write that has not landed leaves the thread ending on
    // the request; then the bubble stays until it does.
    const stored = Array.isArray(payload.messages) ? payload.messages : [];
    if (stored.length && stored.at(-1)?.role !== "user") return null;
    // An empty bubble is only worth keeping while its turn may still fill it:
    // otherwise it is the "working" indicator of a turn the server has
    // nothing for, and it must not pulse forever.
    return handle.text || kept.settled ? handle : null;
  }

  /** Fetches a stored step that left its payloads on the server (``lazy``). */
  _stepLoader(contextId) {
    return (messageId, stepId) => api.conversationStep(contextId, messageId, stepId);
  }

  /**
   * Where a live answer's lazy steps come from.
   *
   * A step frame of a running turn carries only its identity, labels and
   * status: opening the card fetches the rest. Until the turn is stored the
   * recorder answers under the reserved message id ``live``; afterwards the
   * message's own id works, so a card opened late still loads.
   */
  _liveStepSource(contextId, message) {
    return () => (stepId) => {
      const stored = message.messageId;
      return stored
        ? api.conversationStep(contextId, stored, stepId)
        : api.conversationLiveStep(contextId, stepId);
    };
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
