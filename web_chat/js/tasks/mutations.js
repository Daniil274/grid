/**
 * The tracker page's writes as *actions*: one stable key per user action, kept
 * across a lost answer and across a reload.
 *
 * The server (web_chat/tasks_api.py) hands every write to the durable tracker
 * service, which records a `request_id` in its journal. Repeating the same id
 * with the same payload answers the first write instead of making a second
 * one; the same id with another payload is refused as a conflict. So the key
 * is what makes a retry safe, and the page's job is only to keep it.
 *
 * Rules this module implements:
 *
 * - An action's identity is the operation (`create`, `update`, `close`, `dep`)
 *   plus the **exact** canonical payload - not a short digest of it. Two
 *   different payloads therefore cannot share an identity, so a retry can never
 *   be answered with another change's result. The identity is also the stored
 *   record's key, so the payload is kept with the key it belongs to.
 * - The same action with the same payload reuses the key while its outcome is
 *   unknown, so a retry button, a second submit or a submit after a reload
 *   cannot duplicate a write.
 * - A confirmed answer (or a definite refusal, `committed: false`) ends the
 *   action: its key is forgotten, so the *next* user action is a new one and
 *   two deliberate identical tasks are still two tasks.
 * - An unconfirmed answer - HTTP 202 with `committed: null`, a dropped
 *   connection, a 5xx - is **not** success. The key is kept and the caller is
 *   told the change may or may not have landed.
 * - The key is made durable *before* the request leaves. If the record cannot
 *   be written (a full or disabled store) the write is refused
 *   (`tracker_pending_undurable`) instead of sent: a key a reload would lose is
 *   a duplicate waiting to happen.
 * - A new action that would not fit the bounded record is refused the same way
 *   (`tracker_pending_capacity`). Unconfirmed actions are never silently
 *   evicted to make room; retrying one the record already holds stays possible.
 * - Pending actions live in `sessionStorage` under this module's own key, so a
 *   reload keeps them without replaying anything: nothing here retries by
 *   itself. Only keys this module wrote are ever touched, so an auth token
 *   stored in the same `sessionStorage` is never cleared or overwritten.
 *
 * The REST client (web_chat/js/net/api.js) is used for every write; this
 * wrapper only adds the key where the server reads it (the JSON body, or the
 * optional `request_id` argument of the close/dep methods).
 */

import { api } from "../net/api.js";

/** Prefix of every `sessionStorage` key this module owns. */
export const PENDING_PREFIX = "grid.tasks.pending.";
/** How many unconfirmed actions are remembered at once. */
export const PENDING_LIMIT = 40;
/** How many characters the persisted record may take, at most. */
export const PENDING_BYTES = 32000;

/** The operations this module keys. */
const ACTIONS = ["create", "update", "close", "dep"];

/** A key for one action: the server's journal id, never reused for another. */
export function randomKey() {
  const source = globalThis.crypto;
  if (source && typeof source.randomUUID === "function") {
    try {
      return source.randomUUID();
    } catch {
      // fall through to the hex key
    }
  }
  let text = "";
  for (let index = 0; index < 4; index += 1) {
    text += Math.floor(Math.random() * 0x100000000).toString(16).padStart(8, "0");
  }
  return `${text.slice(0, 8)}-${text.slice(8, 12)}-${text.slice(12, 16)}-${text.slice(16, 20)}-${text.slice(20, 32)}`;
}

/** JSON with sorted keys, so the same payload always spells the same scope. */
export function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map((item) => canonicalJson(item ?? null)).join(",")}]`;
  if (value && typeof value === "object") {
    const keys = Object.keys(value)
      .filter((key) => value[key] !== undefined)
      .sort();
    return `{${keys.map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value ?? null) ?? "null";
}

/** The user's payload with the action key added on the wire, as the server reads it. */
export function keyedBody(body, key) {
  return { ...(body ?? {}), request_id: key };
}

/**
 * The identity of one action: its kind plus the exact canonical payload.
 *
 * The payload *is* the identity - no digest is taken, so no two payloads can
 * collide onto one key, and nothing depends on WebCrypto being available.
 */
export function actionScope(action, payload) {
  const canonical = canonicalJson(payload ?? {});
  return { scope: `${action}:${canonical}`, canonical };
}

/** The payload an identity was made from (identity = `action:` + this JSON). */
export function scopePayload(scope) {
  try {
    return JSON.parse(scope.slice(scope.indexOf(":") + 1));
  } catch {
    return null;
  }
}

function typedError(code, message, cause) {
  const error = new Error(message);
  error.code = code;
  if (cause !== undefined) error.cause = cause;
  return error;
}

/** Whether an answer proves the tracker holds the change. */
export function isUnknownAnswer(answer) {
  if (!answer || typeof answer !== "object") return false;
  if (answer.committed === null) return true;
  // An error that carries no `committed` proof at all cannot be read as a
  // refusal: the write may have reached the tracker before the answer was lost.
  return answer.isError === true && answer.committed === undefined;
}

function unknownError(reason) {
  const detail = String(reason ?? "").trim();
  const error = new Error(
    detail
      ? `The tracker did not confirm this change: ${detail}`
      : "The tracker did not confirm this change; it may already be applied. Retrying the same action is safe and will not create a second one.",
  );
  error.unknown = true;
  return error;
}

/**
 * The write actions of the page, keyed per action and payload.
 *
 * `apiClient` and `storage` are injectable for tests; in the browser they are
 * the shared REST client and `sessionStorage`.
 */
export class TrackerMutations {
  constructor({ apiClient = api, storage = null, now = () => Date.now(), limit = PENDING_LIMIT, bytes = PENDING_BYTES } = {}) {
    this.api = apiClient;
    this.storage = storage;
    this.now = now;
    this.limit = limit;
    this.bytes = bytes;
    /** Whether the last pending record reached the store (`false`: memory only). */
    this.durable = false;
    this.entries = this._read();
  }

  /** The storage this module writes to, or null when the tab forbids it. */
  _store() {
    if (this.storage) return this.storage;
    try {
      return globalThis.sessionStorage ?? null;
    } catch {
      return null;
    }
  }

  /** The one storage key this module owns; nothing else is read or cleared. */
  get _indexKey() {
    return `${PENDING_PREFIX}index`;
  }

  _read() {
    const store = this._store();
    if (!store) return {};
    try {
      const raw = store.getItem(this._indexKey);
      if (!raw) return {};
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object") return {};
      const kept = {};
      for (const [scope, entry] of Object.entries(parsed)) {
        const action = ACTIONS.find((name) => scope.startsWith(`${name}:`));
        if (!action) continue;
        if (!entry || typeof entry !== "object") continue;
        if (typeof entry.key !== "string" || typeof scopePayload(scope) !== "object") continue;
        kept[scope] = { key: entry.key, action, at: Number(entry.at) || 0 };
      }
      this.durable = true;
      return kept;
    } catch {
      return {};
    }
  }

  /** The unconfirmed actions, newest first: what a reload kept, never replayed. */
  pending() {
    return Object.entries(this.entries)
      .map(([scope, entry]) => ({ scope, action: entry.action, key: entry.key, at: entry.at, payload: scopePayload(scope) }))
      .sort((left, right) => right.at - left.at);
  }

  _serialize(entries) {
    const ordered = [...entries].sort((left, right) => right[1].at - left[1].at);
    return JSON.stringify(Object.fromEntries(ordered));
  }

  /**
   * The record with `scope` added, or a refusal when it does not fit.
   *
   * Nothing is evicted: a key the page already holds is a user action that may
   * have landed, and dropping it would turn its next retry into a duplicate.
   */
  _fit(entries) {
    if (entries.length > this.limit) {
      throw typedError(
        "tracker_pending_capacity",
        `The page already holds ${entries.length - 1} tracker changes whose outcome is unknown. Retry or dismiss them before starting another; nothing was sent, so nothing was duplicated.`,
      );
    }
    const text = this._serialize(entries);
    if (text.length > this.bytes) {
      throw typedError(
        "tracker_pending_capacity",
        `The page's record of unconfirmed tracker changes would take ${text.length} of its ${this.bytes} characters. Retry or dismiss one before starting another; nothing was sent.`,
      );
    }
    return text;
  }

  /**
   * Remember one new action, durably, *before* its request is sent.
   *
   * A refusal here (`tracker_pending_capacity`, `tracker_pending_undurable`)
   * means the caller sends nothing at all.
   */
  _admit(scope, entry) {
    const candidate = [...Object.entries(this.entries), [scope, entry]];
    const text = this._fit(candidate);
    const store = this._store();
    if (store) {
      try {
        store.setItem(this._indexKey, text);
      } catch (error) {
        // A full or disabled store: the key would be lost on reload, so the
        // write is not sent. What the page already held stays as it was.
        this.durable = false;
        throw typedError(
          "tracker_pending_undurable",
          "This browser could not store the change's key, so it was not sent: after a reload a retry could create a second one. Nothing was sent.",
          error,
        );
      }
      this.durable = true;
    } else {
      // No session storage at all (a private window, an embedded view): the
      // action is still keyed and repeatable while the tab lives, but a reload
      // cannot keep it, and `durable` says so.
      this.durable = false;
    }
    this.entries = Object.fromEntries(candidate);
  }

  _forget(scope) {
    if (!(scope in this.entries)) return;
    const kept = { ...this.entries };
    delete kept[scope];
    this.entries = kept;
    const store = this._store();
    if (!store) return;
    try {
      store.setItem(this._indexKey, this._serialize(Object.entries(kept)));
      this.durable = true;
    } catch {
      // The settled key stays in the stored record: the next identical action
      // would reuse it and be answered from the journal. Reported, not hidden.
      this.durable = false;
    }
  }

  /**
   * One action through its key: remember it, send it, settle it.
   *
   * Nothing is retried here; a repeat is the caller sending the same payload
   * again, which lands on the same key while the outcome is unknown.
   */
  async _run({ action, payload, send }) {
    const { scope } = actionScope(action, payload);
    const existing = this.entries[scope];
    let key;
    if (existing) {
      key = existing.key;
    } else {
      key = randomKey();
      this._admit(scope, { key, action, at: this.now() });
    }
    let answer;
    try {
      answer = await send(key);
    } catch (error) {
      if (uncertainFailure(error)) {
        const wrapped = unknownError(error?.message);
        wrapped.status = error?.status;
        wrapped.payload = error?.payload;
        wrapped.cause = error;
        throw wrapped;
      }
      this._forget(scope);
      throw error;
    }
    if (isUnknownAnswer(answer)) {
      const error = unknownError(answer?.reason);
      error.payload = answer;
      throw error;
    }
    if (answer && answer.isError === true) {
      // A refusal with proof (`committed: false`): nothing was written, so the
      // next attempt is a new action.
      this._forget(scope);
      const error = new Error(answer.reason || "The tracker refused the change");
      error.status = 400;
      error.payload = answer;
      throw error;
    }
    this._forget(scope);
    return answer;
  }

  /** Create a task; the same payload repeats the same action, not a second task. */
  createTask(payload) {
    return this._run({
      action: "create",
      payload: payload ?? {},
      send: (key) => this.api.createTask(keyedBody(payload, key)),
    });
  }

  /** Change one task; the patch is part of the action's identity. */
  updateTask(id, patch) {
    return this._run({
      action: "update",
      payload: { id, patch: patch ?? {} },
      send: (key) => this.api.updateTask(id, keyedBody(patch, key)),
    });
  }

  /** Close a task with its reason; the reason is part of the identity. */
  closeTask(id, reason) {
    return this._run({
      action: "close",
      payload: { id, reason: reason ?? "" },
      send: (key) => this.api.closeTask(id, reason, { request_id: key }),
    });
  }

  /** Add or remove one dependency edge. */
  taskDep(id, action, parentId) {
    return this._run({
      action: "dep",
      payload: { id, action, parent_id: parentId },
      send: (key) => this.api.taskDep(id, action, parentId, { request_id: key }),
    });
  }
}

/**
 * Whether a failed request may still have reached the tracker: no HTTP status
 * (a dropped connection), a timeout, a server fault, or the explicit 202.
 */
function uncertainFailure(error) {
  if (!error) return true;
  if (error.unknown === true) return true;
  if (error.payload && error.payload.committed === false) return false;
  const status = Number(error.status);
  if (status) {
    if (status === 202 || status === 408 || status === 429) return true;
    if (status >= 500) return true;
    return !(status >= 400 && status < 500);
  }
  // No HTTP status: a transport failure (fetch rejects with a TypeError, an
  // abort with an AbortError) may have reached the server; an application
  // refusal is a plain Error and means the write did not happen.
  return error.name === "TypeError" || error.name === "AbortError";
}
