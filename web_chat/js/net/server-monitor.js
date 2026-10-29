/** Observe process changes; never reload the page or discard an unsent draft. */
export function monitorServer({ read, onStatus, onRestart, onUnavailable, interval = 1500 }) {
  let instance = null;
  let stopped = false;
  let timer = null;
  const poll = async () => {
    try {
      const status = await read();
      if (stopped) return;
      const changed = instance !== null && instance !== status.instance_id;
      instance = status.instance_id;
      onStatus(status);
      if (changed) await onRestart(status);
    } catch {
      if (!stopped) onUnavailable?.();
    } finally {
      if (!stopped) timer = setTimeout(poll, interval);
    }
  };
  void poll();
  return () => { stopped = true; clearTimeout(timer); };
}

/**
 * Keep a monitor and the page's own life in step.
 *
 * Watching the server is what tells the page that an instance changed under
 * it, so it must stop while the page is away. Only a page that is really gone
 * counts as away: one put in the back/forward cache is kept as it was and
 * comes back on `pageshow` - its monitor is paused there, not stopped, and
 * starts again if it was.
 *
 * @param {() => (() => void)} start begins monitoring, returns its stop
 * @param {{addEventListener: Function, removeEventListener: Function}} [page]
 * @returns {() => void} stop watching the page's life
 */
export function monitorPageLifecycle(start, page = globalThis.window) {
  let stop = start();
  const onPageHide = (event) => {
    if (event?.persisted) return; // kept for the back button: paused, not stopped
    stop?.();
    stop = null;
  };
  const onPageShow = (event) => {
    // Only a restored page arrives this way with `persisted`; a fresh load
    // already started its monitor above.
    if (!event?.persisted || stop) return;
    stop = start();
  };
  page.addEventListener("pagehide", onPageHide);
  page.addEventListener("pageshow", onPageShow);
  return () => {
    page.removeEventListener("pagehide", onPageHide);
    page.removeEventListener("pageshow", onPageShow);
    stop?.();
    stop = null;
  };
}
