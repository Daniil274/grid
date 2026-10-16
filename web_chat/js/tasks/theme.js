/** Tracker-only palettes: never overwrite the chat's grid.theme preference. */
export const TRACKER_THEMES = ["light", "dark", "vivid"];
const KEY = "grid.tasks.theme";

export function createTrackerTheme(select, root = document.documentElement, storage) {
  let theme = "light";
  try {
    storage ??= globalThis.localStorage;
    const stored = storage?.getItem(KEY);
    if (TRACKER_THEMES.includes(stored)) theme = stored;
  } catch { /* Storage can be unavailable in private/embedded contexts. */ }
  const apply = () => {
    root.dataset.trackerTheme = theme;
    select.value = theme;
  };
  apply();
  select.addEventListener("change", () => {
    theme = TRACKER_THEMES.includes(select.value) ? select.value : "light";
    apply();
    try { storage?.setItem(KEY, theme); } catch { /* Still usable without persistence. */ }
  });
}
