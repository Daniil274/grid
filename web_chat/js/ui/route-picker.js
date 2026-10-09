/**
 * Where a message runs: the system, then the agent inside it.
 *
 * Both levels default to `Auto`, which is not a placeholder - it is the
 * behaviour the CLI has when started without `--agent`/`--config`: the router
 * picks per message. Pinning a level takes it away from the router and nothing
 * else, so "this system, any agent" is a first-class choice.
 *
 * An agent can only be pinned inside a pinned system: with the system on Auto
 * there is no list to pin from, so choosing Auto for the system also releases
 * the agent.
 *
 * Picking a system leaves the popover open, its agents now listed beside it:
 * the next choice is usually one of them. Picking an agent, or Auto for the
 * system, is a complete choice and closes it.
 */

import { h, icon, replace } from "../lib/dom.js";
import { ICONS } from "./icons.js";

const AUTO = null;

const agentMeta = (agent) =>
  [
    agent.personal && "yours",
    agent.model_description || agent.model_name || agent.model_key,
    `${agent.tool_count} tools`,
    agent.mcp_enabled && "MCP",
  ]
    .filter(Boolean)
    .join(" · ");

/** The space's own systems (web_chat/system_hub.py): an admin's drafts, the user's own. */
const BADGES = { draft: "draft · admins only", mine: "yours" };

const systemMeta = (system) => {
  const count = system.agents?.length ?? 0;
  return [BADGES[system.badge], `${count} agent${count === 1 ? "" : "s"}`].filter(Boolean).join(" · ");
};

/** Tools that will fail and why, as the server's health check reported them. */
function issuesNote(issues) {
  const lines = issues?.summary ?? [];
  if (!lines.length) return null;
  return h(
    "span.routeOption__issues",
    { title: lines.join("\n") },
    icon(ICONS.error, { size: 12 }),
    h("span", { text: `${lines.length} tool problem(s): ${lines[0]}${lines.length > 1 ? " …" : ""}` }),
  );
}

function option({ title, description, meta, issues, active, disabled, onPick }) {
  return h(
    "button.routeOption",
    {
      type: "button",
      class: active ? "is-active" : "",
      disabled,
      // The description is clamped to a few lines; the whole of it is here.
      title: description || null,
      on: disabled ? {} : { click: onPick },
    },
    h(
      "span.routeOption__top",
      {},
      h("span.routeOption__title", { text: title }),
      active ? icon(ICONS.check, { size: 14, className: "routeOption__check" }) : null,
    ),
    description ? h("span.routeOption__desc", { text: description }) : null,
    meta ? h("span.routeOption__meta", { text: meta }) : null,
    issuesNote(issues),
  );
}

export function createRoutePicker({ button, nameNode, metaNode, popover, systemList, agentList, agentHeading, store, onChange }) {
  // The chip drives the popover: it names it (aria-controls) and reports its
  // state (aria-expanded), the popover itself is focusable and holds the menu.
  button.setAttribute("aria-controls", popover.id);
  button.setAttribute("aria-expanded", "false");
  popover.setAttribute("tabindex", "-1");

  const options = () => [...popover.querySelectorAll(".routeOption:not([disabled])")];

  const focusFirstOption = () => (options()[0] ?? popover).focus();

  const open = () => {
    popover.classList.add("is-open");
    button.setAttribute("aria-expanded", "true");
    focusFirstOption();
  };

  /** Close the popover; return focus to the chip when it had it (Escape, pick). */
  const close = ({ restoreFocus = popover.contains(document.activeElement) } = {}) => {
    if (!popover.classList.contains("is-open")) return;
    popover.classList.remove("is-open");
    button.setAttribute("aria-expanded", "false");
    if (restoreFocus) button.focus();
  };

  const systemOf = (state) => state.systems.find((system) => system.key === state.systemKey) ?? null;

  const renderChip = (state) => {
    const system = systemOf(state);
    const agent = system?.agents.find((item) => item.key === state.agentKey) ?? null;
    if (agent) {
      nameNode.textContent = agent.name;
      metaNode.textContent = `${system.name} · ${agentMeta(agent)}`;
    } else if (system) {
      nameNode.textContent = system.name;
      metaNode.textContent = "Auto agent · routed per message";
    } else {
      nameNode.textContent = "Auto";
      metaNode.textContent = state.multiSystem ? "System and agent routed per message" : "Agent routed per message";
    }
    button.dataset.mode = agent ? "agent" : system ? "system" : "auto";
  };

  const renderSystems = (state) => {
    const options = [];
    if (state.multiSystem) {
      options.push(
        option({
          title: "Auto",
          description: "Route every message to the best-fitting system.",
          active: state.systemKey === AUTO,
          onPick: () => pick({ systemKey: AUTO, agentKey: AUTO }),
        }),
      );
    }
    for (const system of state.systems) {
      options.push(
        option({
          title: system.name,
          description: system.error || system.description,
          meta: system.error ? "unavailable" : systemMeta(system),
          issues: system.issues,
          active: state.systemKey === system.key,
          disabled: Boolean(system.error),
          // Keep the pinned agent when the same system is picked again; a
          // system of one agent is that agent, with nothing left to choose.
          onPick: () =>
            system.agents.length === 1
              ? pick({ systemKey: system.key, agentKey: system.agents[0].key })
              : pick(
                  { systemKey: system.key, agentKey: state.systemKey === system.key ? state.agentKey : AUTO },
                  { keepOpen: true },
                ),
        }),
      );
    }
    // The runtime always reports at least one system, so an empty list means
    // the page is talking to a server that predates system selection.
    replace(
      systemList,
      options.length
        ? options
        : h("p.routeHint", { text: "No systems reported. Restart the chat server to pick one up." }),
    );
  };

  const renderAgents = (state) => {
    const system = systemOf(state);
    agentHeading.textContent = system ? `Agents · ${system.name}` : "Agents";
    if (!system) {
      replace(
        agentList,
        h("p.routeHint", { text: "Pick a system above to choose one of its agents." }),
      );
      return;
    }
    replace(agentList, [
      option({
        title: "Auto",
        description: "Route every message to the best-fitting agent of this system.",
        active: state.agentKey === AUTO,
        onPick: () => pick({ systemKey: system.key, agentKey: AUTO }),
      }),
      ...system.agents.map((agent) =>
        option({
          title: agent.name,
          description: agent.description || agent.key,
          meta: agentMeta(agent),
          issues: agent.issues,
          active: state.agentKey === agent.key,
          onPick: () => pick({ systemKey: system.key, agentKey: agent.key }),
        }),
      ),
    ]);
  };

  function pick(selection, { keepOpen = false } = {}) {
    if (!keepOpen) close({ restoreFocus: true });
    onChange(selection);
  }

  button.addEventListener("click", (event) => {
    event.stopPropagation();
    if (popover.classList.contains("is-open")) close({ restoreFocus: true });
    else open();
  });
  popover.addEventListener("click", (event) => event.stopPropagation());
  // Arrow keys move between the option buttons; Left/Right hop to the other
  // column (systems / agents), which is how the two lists read on screen.
  popover.addEventListener("keydown", (event) => {
    if (!["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const items = options();
    if (!items.length) return;
    event.preventDefault();
    if (event.key === "Home") return items[0].focus();
    if (event.key === "End") return items[items.length - 1].focus();
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      const column = event.key === "ArrowLeft" ? systemList : agentList;
      const [first] = [...column.querySelectorAll(".routeOption:not([disabled])")];
      if (first) first.focus();
      return;
    }
    const current = items.indexOf(document.activeElement);
    const step = event.key === "ArrowDown" ? 1 : -1;
    items[(current + step + items.length) % items.length].focus();
  });
  document.addEventListener("click", () => close());
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && popover.classList.contains("is-open")) close({ restoreFocus: true });
  });

  store.watch(["systems", "systemKey", "agentKey", "multiSystem"], (state) => {
    renderChip(state);
    renderSystems(state);
    renderAgents(state);
    // Re-rendering replaces the option buttons; keep the keyboard inside the
    // open popover (picking a system reveals its agents beside it).
    if (popover.classList.contains("is-open") && !popover.contains(document.activeElement)) focusFirstOption();
  });

  return { close };
}
