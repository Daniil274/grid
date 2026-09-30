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

const proposalUrl = (id, proposalId, action) =>
  `/api/admin/reviews/${encodeURIComponent(id)}/proposals/${encodeURIComponent(proposalId)}/${action}`;

const json = (method, body) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const api = {
  bootstrap: () => request("/api/chat/bootstrap"),
  uploadFiles: (files) => {
    const body = new FormData();
    for (const file of files) body.append("files", file);
    return request("/api/workspace/uploads", { method: "POST", body });
  },
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
  /** A config file: `target` is the catalog's or a system's key (web_chat/deployment.py); none is the default system's. */
  getSettings: (target) => request(`/api/settings${target ? `?target=${encodeURIComponent(target)}` : ""}`),
  saveSettings: (config, target) => request("/api/settings/structured", json("PUT", { config, target })),
  saveYaml: (yamlContent, target) =>
    request("/api/settings/yaml", json("PUT", { yaml_content: yamlContent, target })),
  /** Report a problem with an answer: freezes it for the admins (web_chat/review). */
  reportAnswer: (contextId, messageId, note) =>
    request(`/api/chat/conversations/${encodeURIComponent(contextId)}/reviews`, json("POST", { message_id: messageId, note })),
  myReports: () => request("/api/reviews"),
  /** Admins: every report, one with its evidence, its status. */
  adminReports: (status) => request(`/api/admin/reviews${status ? `?status=${encodeURIComponent(status)}` : ""}`),
  adminReport: (id) => request(`/api/admin/reviews/${encodeURIComponent(id)}`),
  setReportStatus: (id, status) => request(`/api/admin/reviews/${encodeURIComponent(id)}`, json("PATCH", { status })),
  /** The review agents' analysis of a report: its state, and a new turn of it. */
  reportAnalysis: (id) => request(`/api/admin/reviews/${encodeURIComponent(id)}/analysis`),
  analyseReport: (id, message = "") =>
    request(`/api/admin/reviews/${encodeURIComponent(id)}/analysis`, json("POST", { message })),
  /** A proposal: does its patch apply here; send it to the evolution loop; what became of it. */
  checkProposal: (id, proposalId) => request(proposalUrl(id, proposalId, "check"), { method: "POST" }),
  evolveProposal: (id, proposalId) => request(proposalUrl(id, proposalId, "evolve"), { method: "POST" }),
  proposalTask: (id, proposalId) => request(proposalUrl(id, proposalId, "task")),
  /** Admins, when the server allows it: pick an answer the user did not report. */
  reviewChats: (userId) => request(`/api/admin/review-chats/${encodeURIComponent(userId)}`),
  reviewAnswers: (userId, contextId) =>
    request(`/api/admin/review-chats/${encodeURIComponent(userId)}/${encodeURIComponent(contextId)}`),
  openReport: (userId, contextId, messageId, note) =>
    request(
      `/api/admin/review-chats/${encodeURIComponent(userId)}/${encodeURIComponent(contextId)}/reviews`,
      json("POST", { message_id: messageId, note }),
    ),
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
  /** The systems page (web_chat/system_hub.py). */
  systems: () => request("/api/systems"),
  system: (kind, key) => request(`/api/systems/${encodeURIComponent(kind)}/${encodeURIComponent(key)}`),
  saveSystemAcceptance: (kind, key, yaml) => request(`/api/systems/${encodeURIComponent(kind)}/${encodeURIComponent(key)}/acceptance`, json("PUT", { yaml })),
  probeSystem: (kind, key, messages) => request("/api/systems/probe", json("POST", { kind, key, messages })),
  createSystem: (body) => request("/api/systems/created", json("POST", body)),
  describeSystem: (key, patch) => request(`/api/systems/created/${encodeURIComponent(key)}`, json("PATCH", patch)),
  saveSystemConfig: (key, yaml) => request(`/api/systems/created/${encodeURIComponent(key)}/config`, json("PUT", { yaml })),
  setSystemStatus: (key, status) =>
    request(`/api/systems/created/${encodeURIComponent(key)}/status`, json("POST", { status })),
  deleteSystem: (key) => request(`/api/systems/created/${encodeURIComponent(key)}`, { method: "DELETE" }),
  describeBuiltSystem: (key, patch) => request(`/api/systems/built/${encodeURIComponent(key)}`, json("PATCH", patch)),
  saveBuiltSystemConfig: (key, yaml) => request(`/api/systems/built/${encodeURIComponent(key)}/config`, json("PUT", { yaml })),
  activateBuiltSystem: (key, active) => request(`/api/systems/built/${encodeURIComponent(key)}/active`, json("POST", { active })),
  deleteBuiltSystem: (key) => request(`/api/systems/built/${encodeURIComponent(key)}`, { method: "DELETE" }),
  createMySystem: (spec) => request("/api/systems/mine", json("POST", spec)),
  updateMySystem: (key, spec) => request(`/api/systems/mine/${encodeURIComponent(key)}`, json("PUT", spec)),
  deleteMySystem: (key) => request(`/api/systems/mine/${encodeURIComponent(key)}`, { method: "DELETE" }),
  activateMySystem: (key, active) =>
    request(`/api/systems/mine/${encodeURIComponent(key)}/active`, json("POST", { active })),
  submitMySystem: (key, note) => request(`/api/systems/mine/${encodeURIComponent(key)}/submit`, json("POST", { note })),
  withdrawMySystem: (key) => request(`/api/systems/mine/${encodeURIComponent(key)}/submit`, { method: "DELETE" }),
  importSubmission: (id, key, name) =>
    request(`/api/systems/submissions/${encodeURIComponent(id)}/import`, json("POST", { key, name: name || null })),
  declineSubmission: (id, note) =>
    request(`/api/systems/submissions/${encodeURIComponent(id)}/decline`, json("POST", { note })),
};
