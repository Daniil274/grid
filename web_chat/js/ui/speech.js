/**
 * Spoken answers, played in the browser, one message at a time.
 *
 * An answer is fed in as text and comes out as a stream of audio: it is split
 * into sentences, each synthesized a little ahead of the one playing, so sound
 * starts after the first sentence instead of after the whole text. Audio is
 * decoded and played by the page itself - nothing is played server-side.
 *
 * What gets spoken is the answer the message displays, never the token stream
 * behind it: a run emits an assistant message before each tool call, and the
 * transcript replaces all of them with the final one. Reading the stream would
 * read those discarded steps aloud.
 *
 * Exactly one message speaks at a time and it always belongs to a message, so
 * every spoken answer has a visible owner with a button to silence it. Speaking
 * is opt-in: a turn dictated by voice speaks itself, anything else speaks only
 * when the reader presses that button.
 */

import { speakableText } from "../lib/speakable.js";

/** Sentence-ish boundaries. A clause is enough to sound natural and start early. */
const BOUNDARY = /[^.!?…\n]*[.!?…]+["')\]]*\s+|[^\n]*\n+/y;
/** Below this, a "sentence" is punctuation noise; keep buffering. */
const MIN_CHUNK = 24;
/** Sentences synthesized ahead of the one playing. */
const LOOKAHEAD = 2;
/** Backoff between retries while the single synthesis slot is taken. */
const BUSY_RETRY_MS = 250;
const BUSY_RETRIES = 12;

/**
 * Split off every complete sentence at the head of `text`.
 * @returns {{chunks: string[], rest: string}}
 */
export function takeSentences(text) {
  const chunks = [];
  let index = 0;
  while (index < text.length) {
    BOUNDARY.lastIndex = index;
    const match = BOUNDARY.exec(text);
    if (!match) break;
    const chunk = match[0].trim();
    index = BOUNDARY.lastIndex;
    if (!chunk) continue;
    // Merge runts ("Fig. 2.") into the sentence they belong to.
    if (chunk.length < MIN_CHUNK && chunks.length) chunks[chunks.length - 1] += ` ${chunk}`;
    else chunks.push(chunk);
  }
  return { chunks, rest: text.slice(index) };
}

/** One message being spoken: an ordered pipeline of sentences. */
class Utterance {
  constructor(id, synthesize, onState) {
    this.id = id;
    this._synthesize = synthesize;
    // The runtime synthesizes one sentence at a time (a single engine slot), so
    // requests are chained rather than fired together: running ahead of
    // playback is useful, running in parallel only collides with itself.
    this._chain = Promise.resolve();
    this._onState = onState;
    this._texts = [];
    this._clips = [];
    this._buffer = "";
    this._next = 0;
    this._closed = false;
    this._stopped = false;
    this._audio = null;
    this._wake = null;
  }

  /** Add speakable text; complete sentences enter the pipeline immediately.
   *
   * The caller filters: markup spans lines, so it has to be stripped from the
   * whole answer before it is cut into sentences, never sentence by sentence.
   * `SpeechPlayer.speak` does that. */
  push(text) {
    if (this._stopped) return;
    const { chunks, rest } = takeSentences(this._buffer + text);
    this._buffer = rest;
    this._enqueue(chunks);
  }

  /** No more text is coming: speak whatever is left and finish. */
  close() {
    if (this._closed) return;
    this._closed = true;
    const tail = this._buffer;
    this._buffer = "";
    this._enqueue([tail]);
  }

  _enqueue(chunks) {
    for (const chunk of chunks) {
      const text = chunk.trim();
      if (text) this._texts.push(text);
    }
    this._resume();
  }

  stop() {
    this._stopped = true;
    this._closed = true;
    if (this._audio) {
      this._audio.pause();
      this._audio.removeAttribute("src");
      this._audio.load();
      this._audio = null;
    }
    this._resume();
  }

  /** Barge-in: hold the current clip without losing the queue. */
  pause() {
    this._audio?.pause();
  }

  resumePlayback() {
    this._audio?.play().catch(() => {});
  }

  get isPlaying() {
    return Boolean(this._audio) && !this._audio.paused;
  }

  async run() {
    this._state("loading");
    try {
      while (!this._stopped) {
        if (this._next >= this._texts.length) {
          if (this._closed) break;
          await this._sleepUntilFed();
          continue;
        }
        this._prefetch();
        const clip = await this._clips[this._next];
        if (this._stopped) break;
        this._state("speaking");
        if (clip) await this._playClip(clip);
        this._next += 1;
      }
      this._state("idle");
    } catch (error) {
      if (!this._stopped) this._state("error", error);
    } finally {
      this.stop();
    }
  }

  /** Queue a few sentences ahead so playback rarely waits on synthesis. */
  _prefetch() {
    const until = Math.min(this._texts.length, this._next + 1 + LOOKAHEAD);
    for (let index = this._next; index < until; index += 1) {
      if (this._clips[index]) continue;
      const text = this._texts[index];
      this._chain = this._chain.then(() => (this._stopped ? null : this._synthesize(text)));
      this._clips[index] = this._chain;
    }
  }

  async _playClip(blob) {
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    this._audio = audio;
    try {
      await new Promise((resolve, reject) => {
        audio.onended = resolve;
        audio.onerror = () => reject(new Error("The browser could not play the synthesized audio"));
        audio.play().catch(reject);
      });
    } finally {
      audio.onended = audio.onerror = null;
      URL.revokeObjectURL(url);
      if (this._audio === audio) this._audio = null;
    }
  }

  _sleepUntilFed() {
    return new Promise((resolve) => {
      this._wake = resolve;
    });
  }

  _resume() {
    const wake = this._wake;
    this._wake = null;
    wake?.();
  }

  _state(name, error) {
    this._onState(this.id, name, error);
  }
}

export class SpeechPlayer {
  /**
   * @param {object} deps
   * @param {(text: string) => Promise<Blob>} deps.synthesize
   * @param {(id: string, state: "idle"|"loading"|"speaking"|"error", error?: Error) => void} [deps.onState]
   */
  constructor({ synthesize, onState = () => {} }) {
    this._synthesize = synthesize;
    this._onState = onState;
    this._current = null;
  }

  get activeId() {
    return this._current?.id ?? null;
  }

  isActive(id) {
    return this._current !== null && this._current.id === id;
  }

  /** True while a clip is actually sounding - what barge-in detection asks. */
  get isPlaying() {
    return Boolean(this._current?.isPlaying);
  }

  /**
   * Start speaking a message. Any other message stops.
   * @returns {Utterance} feed it with `push`, end it with `close`; its
   *   `finished` promise settles when playback is over or stopped.
   */
  start(id) {
    this.stop();
    const utterance = new Utterance(id, this._synthesize, (...args) => this._onState(...args));
    this._current = utterance;
    utterance.finished = utterance.run().finally(() => {
      if (this._current === utterance) this._current = null;
    });
    return utterance;
  }

  /**
   * Speak a finished message: filtered for the ear, then split into sentences.
   * @returns {Promise<void>} resolves when it stops sounding, at once when the
   *   message has nothing sayable in it (a bare code block, say)
   */
  speak(id, text) {
    const spoken = speakableText(text);
    if (!spoken) return Promise.resolve();
    const utterance = this.start(id);
    utterance.push(spoken);
    utterance.close();
    return utterance.finished;
  }

  stop() {
    const current = this._current;
    this._current = null;
    current?.stop();
  }

  pause() {
    this._current?.pause();
  }

  resume() {
    this._current?.resumePlayback();
  }
}

/**
 * Ask the runtime for one sentence of audio.
 *
 * The engine has a single slot and answers 429 while it is taken - by the voice
 * loop, a warm-up, or the previous sentence. That is a wait, not a failure, so
 * it is retried; anything else is reported.
 */
export async function synthesizeSentence(text, { retries = BUSY_RETRIES } = {}) {
  for (let attempt = 0; ; attempt += 1) {
    const response = await fetch("/api/voice/synthesize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (response.ok) return response.blob();
    if (response.status === 429 && attempt < retries) {
      await new Promise((resolve) => setTimeout(resolve, BUSY_RETRY_MS));
      continue;
    }
    const detail = await response.json().catch(() => null);
    throw new Error(detail?.detail || `Speech synthesis failed (${response.status})`);
  }
}
