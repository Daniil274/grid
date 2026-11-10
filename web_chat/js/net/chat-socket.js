/**
 * Chat websocket.
 *
 * One connection per turn, closed when the turn ends. The turn itself lives on
 * the server: a reloaded page attaches to it again and gets it replayed. The server's event
 * vocabulary (see `web_chat/trace.py`) is dispatched by `type` to handlers the
 * caller registers, so no component has to parse raw frames.
 */

/** @typedef {"token"|"step"|"step_removed"|"reasoning"|"routed"|"tool_issues"|"final_output"|"answer_reset"|"attached"|"stopping"|"interrupted"|"image"|"delivery"|"queue"|"steered"|"revision_notice"|"model"|"ui"|"ui_submitted"|"error"|"busy"|"done"|"policy_filter"|"policy_review"|"execution_target"} ChatEventType */

export class ChatConnection {
  /**
   * @param {string} contextId conversation to attach to
   * @returns {Promise<ChatConnection>} resolved once the socket is open
   */
  static open(contextId) {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(`${protocol}//${location.host}/api/chat/ws/${encodeURIComponent(contextId)}`);
    const connection = new ChatConnection(socket);
    return new Promise((resolve, reject) => {
      socket.addEventListener("open", () => resolve(connection), { once: true });
      socket.addEventListener("error", () => reject(new Error("Could not reach the chat runtime")), { once: true });
    });
  }

  constructor(socket) {
    this._socket = socket;
    /** @type {Map<ChatEventType, Function>} */
    this._handlers = new Map();
    this._closeHandler = null;

    socket.addEventListener("message", (event) => this._dispatch(event.data));
    socket.addEventListener("close", () => this._closeHandler?.());
  }

  /**
   * Whether this connection can still carry a frame. A phone that slept or a
   * network that moved leaves the socket closed without every close event
   * arriving, so callers ask this rather than trust the last event they saw.
   */
  get isOpen() {
    return this._socket.readyState === WebSocket.OPEN;
  }

  /** @param {ChatEventType} type */
  on(type, handler) {
    this._handlers.set(type, handler);
    return this;
  }

  /** Called when the socket drops, for any reason, exactly once per instance. */
  onClose(handler) {
    this._closeHandler = handler;
    return this;
  }

  /**
   * @param {string} message
   * @param {{system_key: ?string, agent_key: ?string, policy_filter?: string,
   *   execution_target?: string, execution_workdir?: string}} selection
   *   nulls mean `auto`; `policy_filter` is the policy switch; `execution_target`
   *   is where the tools act (`cloud` or `device:<id>`, with its folder)
   * @param {string[]} [images] data URLs attached to the message
   * @param {string|null} [audio] WAV data URL attached to the message
   * @param {?string} [editOf] the slot of the message this one is a new version of
   * @param {?string} [delivery] while the agent works: "now", "next_step",
   *   "after_turn", or null for the decision model to choose
   */
  send(message, selection, images = [], editOf = null, delivery = null, audio = null) {
    return this._post({
      message,
      ...selection,
      ...(images.length ? { images } : {}),
      ...(editOf ? { edit_of: editOf } : {}),
      ...(delivery ? { delivery } : {}),
      ...(audio ? { audio } : {}),
    });
  }

  /** Send a captured audio data URL as an audio-bearing user message. */
  sendAudio(audio, selection) {
    if (typeof audio !== "string" || !audio.startsWith("data:audio/wav;base64,")) {
      throw new TypeError("Audio messages must be WAV data URLs");
    }
    return this._post({ message: "", audio, ...selection });
  }

  /** Act on a waiting message: "send_queued" or "unqueue". */
  queued(action, id) {
    return this._post({ action, id });
  }

  /**
   * First call: stop after the current step. Second call, or `now`: stop at
   * once, wherever the agent is.
   */
  stop({ now = false } = {}) {
    return this._post(now ? { action: "stop", now: true } : { action: "stop" });
  }

  /** Resume the interrupted turn the conversation ends with. */
  resume() {
    return this._post({ action: "continue" });
  }

  /** Run this conversation under policy filter *filter*, its running turn too. */
  policyFilter(filter) {
    return this._post({ action: "policy_filter", filter });
  }

  /**
   * Point this conversation at the server or a device - a live switch: the
   * running turn and its tools follow it from the next call on. The server
   * answers with `execution_target`: the state the chat is in afterwards, its
   * devices included, so a refused switch leaves the page showing what is true.
   */
  executionTarget(target, workdir = "") {
    return this._post({ action: "execution_target", target, workdir });
  }

  /** Allow or decline a call the policy holds for the user; `remember` allows the tool for the turn. */
  answerReview(approvalId, { approve, remember = false }) {
    return this._post({ action: "policy_review", approval_id: approvalId, approve, remember });
  }

  /**
   * Send a button of a presented interface as the user's next turn.
   *
   * The browser sends ids, values and revisions only: the server composes the
   * sentence from the stored document, so no prompt, tool or HTML travels from
   * here. `requestId` makes a retry of the same press harmless.
   */
  uiAction({ messageId, uiId, actionId, documentRevision, stateRevision, values, requestId }) {
    return this._post({
      action: "ui_action",
      message_id: messageId,
      ui_id: uiId,
      action_id: actionId,
      document_revision: documentRevision,
      state_revision: stateRevision,
      values,
      request_id: requestId,
    });
  }

  /** Follow the conversation's running turn: the server replays it, then streams live. */
  attach() {
    return this._post({ action: "attach" });
  }

  close() {
    this._handlers.clear();
    this._closeHandler = null;
    // A socket still connecting must be closed too, or it opens for nobody.
    if (this._socket.readyState <= WebSocket.OPEN) this._socket.close();
  }

  /**
   * Send one frame. False when the socket cannot carry it any more: dropping
   * a frame silently would leave the page waiting for an answer nobody sends.
   * @returns {boolean}
   */
  _post(payload) {
    if (!this.isOpen) return false;
    this._socket.send(JSON.stringify(payload));
    return true;
  }

  _dispatch(raw) {
    let payload;
    try {
      payload = JSON.parse(raw);
    } catch {
      return; // a frame we cannot read is not worth tearing the turn down for
    }
    this._handlers.get(payload.type)?.(payload);
  }
}
