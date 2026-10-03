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
