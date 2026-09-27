/**
 * Minimal observable store.
 *
 * The app has one source of truth for cross-cutting state (agents,
 * conversations, which turn is streaming) and views subscribe to it. No
 * framework, no diffing - views are cheap enough to re-render wholesale, and
 * keeping the update path explicit makes the data flow readable.
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
  };
}
