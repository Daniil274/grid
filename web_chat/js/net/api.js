/**
 * REST client. One place that knows the server's URL shape and error format.
 *
 * On a server with accounts, a 401 means the session is gone (signed out
 * elsewhere, expired, disabled): the page goes to sign-in.
 */

const SIGN_IN = "/login";

async function request(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => null);
  if (response.status === 401) {
    location.assign(SIGN_IN);
    throw new Error("Signed out");
  }
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
  renameConversation: (id, title) =>
    request(`/api/chat/conversations/${encodeURIComponent(id)}`, json("PATCH", { title })),
  deleteConversation: (id) =>
    request(`/api/chat/conversations/${encodeURIComponent(id)}`, { method: "DELETE" }),
  /** Fork before a user message; the edited text then goes into the branch. */
  createBranch: (id, messageId) =>
    request(`/api/chat/conversations/${encodeURIComponent(id)}/branches`, json("POST", { message_id: messageId })),
  compactConversation: (id) =>
    request(`/api/chat/conversations/${encodeURIComponent(id)}/compact`, { method: "POST" }),
  activateBranch: (id) =>
    request(`/api/chat/conversations/${encodeURIComponent(id)}/activate`, { method: "POST" }),
  prepareAgent: (systemKey, agentKey) =>
    request("/api/chat/prepare-agent", json("POST", { system_key: systemKey, agent_key: agentKey })),
  getSettings: () => request("/api/settings"),
  saveSettings: (config) => request("/api/settings/structured", json("PUT", { config })),
  saveYaml: (yamlContent) => request("/api/settings/yaml", json("PUT", { yaml_content: yamlContent })),
  /** Report a problem with an answer: freezes it for the admins (web_chat/review). */
  reportAnswer: (contextId, messageId, note) =>
    request(`/api/chat/conversations/${encodeURIComponent(contextId)}/reviews`, json("POST", { message_id: messageId, note })),
  myReports: () => request("/api/reviews"),
  signOut: () => request("/api/auth/logout", { method: "POST" }),
  changePassword: (current, next) => request("/api/auth/password", json("POST", { current, new: next })),
  adminUsers: () => request("/api/admin/users"),
  reviews: () => request("/api/action-policy/reviews"),
  resolveReview: (id, decision) =>
    request(`/api/action-policy/reviews/${encodeURIComponent(id)}`, json("POST", { decision })),
  updateUser: (id, patch) => request(`/api/admin/users/${encodeURIComponent(id)}`, json("PATCH", patch)),
  adminInvites: () => request("/api/admin/invites"),
  createInvite: (invite) => request("/api/admin/invites", json("POST", invite)),
  revokeInvite: (id) => request(`/api/admin/invites/${encodeURIComponent(id)}`, { method: "DELETE" }),
  /** The user's own agents and the templates they may start from. */
  agents: () => request("/api/agents"),
  createAgent: (spec) => request("/api/agents", json("POST", spec)),
  updateAgent: (key, spec) => request(`/api/agents/${encodeURIComponent(key)}`, json("PUT", spec)),
  deleteAgent: (key) => request(`/api/agents/${encodeURIComponent(key)}`, { method: "DELETE" }),
};
