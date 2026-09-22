/**
 * Theme control: system, light, dark.
 *
 * "System" is the default and stamps nothing on the root, so the stylesheet's
 * `prefers-color-scheme` block decides. An explicit choice stamps
 * `data-theme` and wins in both directions.
 */

const STORAGE_KEY = "grid.theme";
const CYCLE = ["system", "light", "dark"];
const LABEL = { system: "Theme: system", light: "Theme: light", dark: "Theme: dark" };

const read = () => {
  const stored = localStorage.getItem(STORAGE_KEY);
  return CYCLE.includes(stored) ? stored : "system";
};

function apply(theme) {
  if (theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
}

export function createThemeToggle(button) {
  let theme = read();
  apply(theme);

  const sync = () => {
    button.title = LABEL[theme];
    button.setAttribute("aria-label", LABEL[theme]);
    button.dataset.theme = theme;
  };

  button.addEventListener("click", () => {
    theme = CYCLE[(CYCLE.indexOf(theme) + 1) % CYCLE.length];
    localStorage.setItem(STORAGE_KEY, theme);
    apply(theme);
    sync();
  });

  sync();
}
