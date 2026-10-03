/**
 * Minimal observable store.
 *
 * The app has one source of truth for cross-cutting state (agents,
 * conversations, which turn is streaming) and views subscribe to it. No
 * framework, no diffing: a view re-renders wholesale, but only when one of the
 * keys it reads changed (`watch`) - a list of hundreds of chats must not be
 * rebuilt because a turn started streaming.
 */
export function createStore(initial) {
  let state = { ...initial };
  const listeners = new Set();

  return {
    get: () => state,

    /** Shallow-merge a patch and notify. Re-entrant updates are safe. */
    set(patch) {
      const next = typeof patch === "function" ? patch(state) : patch;
      const changed = Object.entries(next).some(([key, value]) => state[key] !== value);
      if (!changed) return state;
      state = { ...state, ...next };
      for (const listener of [...listeners]) listener(state);
      return state;
    },

    /** @returns {() => void} unsubscribe */
    subscribe(listener) {
      listeners.add(listener);
      listener(state);
      return () => listeners.delete(listener);
    },

    /**
     * Like `subscribe`, but called only when one of *keys* changed.
     * @param {string[]} keys
     * @returns {() => void} unsubscribe
     */
    watch(keys, listener) {
      let seen = null;
      return this.subscribe((current) => {
        if (seen && keys.every((key) => seen[key] === current[key])) return;
        seen = current;
        listener(current);
      });
    },
  };
}
