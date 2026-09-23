/**
 * Chat websocket.
 *
 * One connection per turn, closed when the turn ends. The turn itself lives on
 * the server: a reloaded page attaches to it again and gets it replayed. The server's event
 * vocabulary (see `web_chat/trace.py`) is dispatched by `type` to handlers the
 * caller registers, so no component has to parse raw frames.
 */

/** @typedef {"token"|"step"|"step_removed"|"reasoning"|"routed"|"final_output"|"answer_reset"|"attached"|"error"|"busy"|"done"} ChatEventType */

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

  /** @param {{system_key: ?string, agent_key: ?string}} selection nulls mean `auto` */
  send(message, selection) {
    this._post({ message, ...selection });
  }

  stop() {
    this._post({ action: "stop" });
  }

  /** Follow the conversation's running turn: the server replays it, then streams live. */
  attach() {
    this._post({ action: "attach" });
  }

  close() {
    this._handlers.clear();
    this._closeHandler = null;
    if (this._socket.readyState <= WebSocket.OPEN) this._socket.close();
  }

  _post(payload) {
    if (this._socket.readyState !== WebSocket.OPEN) return;
    this._socket.send(JSON.stringify(payload));
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
