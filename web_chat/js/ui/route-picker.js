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
 */

import { h, icon, replace } from "../lib/dom.js";
import { ICONS } from "./icons.js";

const AUTO = null;

const agentMeta = (agent) =>
  [agent.model_description || agent.model_name || agent.model_key, `${agent.tool_count} tools`, agent.mcp_enabled && "MCP"]
    .filter(Boolean)
    .join(" · ");

function option({ title, description, meta, active, disabled, onPick }) {
  return h(
    "button.routeOption",
    {
      type: "button",
      class: active ? "is-active" : "",
      disabled,
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
  );
}

export function createRoutePicker({ button, nameNode, metaNode, popover, systemList, agentList, agentHeading, store, onChange }) {
  const close = () => popover.classList.remove("is-open");

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
          meta: system.error ? "unavailable" : system.config_path,
          active: state.systemKey === system.key,
          disabled: Boolean(system.error),
          onPick: () => pick({ systemKey: system.key, agentKey: AUTO }),
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
          active: state.agentKey === agent.key,
          onPick: () => pick({ systemKey: system.key, agentKey: agent.key }),
        }),
      ),
    ]);
  };

  function pick(selection) {
    close();
    onChange(selection);
  }

  button.addEventListener("click", (event) => {
    event.stopPropagation();
    popover.classList.toggle("is-open");
  });
  popover.addEventListener("click", (event) => event.stopPropagation());
  document.addEventListener("click", close);
  document.addEventListener("keydown", (event) => event.key === "Escape" && close());

  store.subscribe((state) => {
    renderChip(state);
    renderSystems(state);
    renderAgents(state);
  });

  return { close };
}
