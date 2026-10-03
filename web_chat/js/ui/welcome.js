/**
 * The systems and their agents, on the empty chat.
 *
 * The route picker hides them behind a chip; a new chat is where a person
 * decides who should answer, so they are laid out here as well. A card pins a
 * system, a chip in it pins an agent, and Auto hands both back to the router -
 * the same selection the picker makes.
 */

import { h, replace } from "../lib/dom.js";

export function createWelcomeSystems({ container, store, onChange }) {
  const card = ({ title, description, active, onPick, agents = [], agentKey, systemKey }) =>
    h(
      "div.systemCard",
      { class: active ? "is-active" : "" },
      h(
        "button.systemCard__main",
        { type: "button", title: description || null, on: { click: onPick } },
        h("span.systemCard__title", { text: title }),
        description ? h("span.systemCard__desc", { text: description }) : null,
      ),
      agents.length
        ? h(
            "div.systemCard__agents",
            {},
            agents.map((agent) =>
              h(
                "button.agentChip",
                {
                  type: "button",
                  class: active && agentKey === agent.key ? "is-active" : "",
                  title: agent.description || agent.key,
                  on: {
                    click: () =>
                      onChange({ systemKey, agentKey: active && agentKey === agent.key ? null : agent.key }),
                  },
                },
                agent.name,
              ),
            ),
          )
        : null,
    );

  store.watch(["systems", "systemKey", "agentKey", "multiSystem"], (state) => {
    const systems = state.systems.filter((system) => !system.error);
    if (!systems.length) {
      replace(container);
      return;
    }
    const cards = [];
    if (state.multiSystem) {
      cards.push(
        card({
          title: "Auto",
          description: "The router picks the system and the agent for every message.",
          active: state.systemKey === null,
          onPick: () => onChange({ systemKey: null, agentKey: null }),
        }),
      );
    }
    for (const system of systems) {
      cards.push(
        card({
          title: system.name,
          description: system.description,
          active: state.systemKey === system.key,
          systemKey: system.key,
          agentKey: state.agentKey,
          agents: system.agents.filter((agent) => agent.routable !== false || agent.personal),
          onPick: () => onChange({ systemKey: system.key, agentKey: null }),
        }),
      );
    }
    replace(container, cards);
  });
}
