/** REST client. One place that knows the server's URL shape and error format. */

async function request(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(payload?.detail || `${options.method || "GET"} ${url} failed (${response.status})`);
  }
  return payload;
}

const json = (method, body) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const api = {
  bootstrap: () => request("/api/chat/bootstrap"),
  listConversations: () => request("/api/chat/conversations"),
  getConversation: (id) => request(`/api/chat/conversations/${encodeURIComponent(id)}`),
  createConversation: (selection) => request("/api/chat/conversations", json("POST", selection)),
  prepareAgent: (systemKey, agentKey) =>
    request("/api/chat/prepare-agent", json("POST", { system_key: systemKey, agent_key: agentKey })),
  getSettings: () => request("/api/settings"),
  saveSettings: (config) => request("/api/settings/structured", json("PUT", { config })),
  saveYaml: (yamlContent) => request("/api/settings/yaml", json("PUT", { yaml_content: yamlContent })),
};
