/**
 * The policy switch: how strictly the action policy watches the agents.
 *
 * The filters are the operator's (`filters` in the policy file,
 * schemas/action_policy.py); the server lists them at bootstrap. The choice is
 * the user's and stays in this browser: every message carries it, and moving
 * the switch while the agent works applies to that turn at once. Hidden when
 * the server runs without a policy.
 */

import { h, replace } from "../lib/dom.js";

const STORAGE_KEY = "grid.policyFilter";

function remembered() {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function remember(filter) {
  try {
    localStorage.setItem(STORAGE_KEY, filter);
  } catch {
    // A private window: the switch still works for this page.
  }
}

/**
 * @param {object} deps
 * @param {HTMLSelectElement} deps.select
 * @param {ReturnType<import("../lib/store.js").createStore>} deps.store
 * @param {(filter: string) => void} deps.onChange
 */
export function createPolicySwitch({ select, store, onChange }) {
  const describe = (policy, key) => policy?.filters.find((item) => item.key === key)?.description ?? "";

  store.watch(["policy"], ({ policy }) => {
    select.hidden = !policy;
    if (!policy) return;
    replace(
      select,
      policy.filters.map((item) => h("option", { value: item.key, text: item.label, title: item.description })),
    );
    const saved = remembered();
    const filter = policy.filters.some((item) => item.key === saved) ? saved : policy.default;
    store.set({ policyFilter: filter });
  });

  store.watch(["policyFilter"], ({ policy, policyFilter }) => {
    if (!policyFilter) return;
    select.value = policyFilter;
    select.title = `Policy filter: ${describe(policy, policyFilter)}`;
  });

  select.addEventListener("change", () => {
    remember(select.value);
    onChange(select.value);
  });
}
