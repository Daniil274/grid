/**
 * The refresh rhythm of the map: every ~20 seconds, but only while somebody is
 * looking at the page.
 *
 * A hidden tab does not ask anything and holds no timer; becoming visible again
 * refreshes at once and restarts the rhythm. The document and the timer
 * functions are parameters so tests can drive the whole cycle without a
 * browser, and so a page kept in the back/forward cache does not come back with
 * a timer the browser already forgot about.
 *
 * The map page never admits a second request while one is in flight: `load`
 * is the caller's, and it aborts what it had before asking again.
 */

export const REFRESH_MS = 20000;

/**
 * @param {object} options
 * @param {() => unknown} options.load - refresh now; may return a promise
 * @param {number} [options.intervalMs]
 * @param {Document} [options.doc]
 * @param {{setInterval: Function, clearInterval: Function}} [options.timers]
 * @returns {{start: Function, stop: Function, running: Boolean, visible: Function}}
 */
export function createPoller({ load, intervalMs = REFRESH_MS, doc = globalThis.document, timers = globalThis } = {}) {
  let timer = null;
  let listening = false;

  const visible = () => {
    if (!doc) return true;
    if (typeof doc.visibilityState === "string") return doc.visibilityState === "visible";
    return doc.hidden !== true;
  };

  const clear = () => {
    if (timer != null) timers.clearInterval(timer);
    timer = null;
  };

  const tick = () => {
    if (!visible()) {
      clear();
      return;
    }
    load();
  };

  const arm = () => {
    clear();
    timer = timers.setInterval(tick, intervalMs);
  };

  const onVisibility = () => {
    if (visible()) {
      load();
      arm();
    } else {
      clear();
    }
  };

  const poller = {
    get running() {
      return timer != null;
    },
    visible,
    /** Start the rhythm; a hidden page waits for its turn to be seen. */
    start() {
      if (!listening && doc?.addEventListener) {
        doc.addEventListener("visibilitychange", onVisibility);
        listening = true;
      }
      if (visible()) arm();
      return poller;
    },
    /** Stop it and forget the listener (a page going away). */
    stop() {
      clear();
      if (listening && doc?.removeEventListener) {
        doc.removeEventListener("visibilitychange", onVisibility);
        listening = false;
      }
      return poller;
    },
    /** Refresh on demand, never while hidden. */
    tick,
  };
  return poller;
}
