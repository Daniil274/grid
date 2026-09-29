/**
 * "Accounts": the admin's view of who may use the server (web_chat/accounts/http.py).
 *
 * Pending actions - what the operator's policy sent to review, from every
 * user's agents; an approval lets that exact action through once.
 * Users - role, state, turns today - with the two changes an admin makes:
 * role and disabled. Invites - a new one is shown once as a link, since only
 * its hash is stored; open ones can be revoked. The server enforces every rule
 * (the last admin stays, nobody disables themselves); refusals are shown as-is.
 */

import { h, replace } from "../lib/dom.js";
import { api } from "../net/api.js";
import { copyText } from "../ui/toast.js";

const when = (seconds) => new Date(seconds * 1000).toLocaleString();

export class AccountsDrawer {
  /**
   * @param {object} nodes drawer, backdrop, closeButton, refreshButton, reviews, users, invites, inviteForm, created, status
   * @param {{currentUser: () => ?object}} options the signed-in admin
   */
  constructor(nodes, { currentUser }) {
    this.nodes = nodes;
    this.currentUser = currentUser;
    this._bind();
  }

  async open() {
    this.nodes.drawer.classList.add("is-open");
    this.nodes.created.hidden = true;
    this._status("");
    await this._load();
  }

  close() {
    this.nodes.drawer.classList.remove("is-open");
  }

  _bind() {
    const { drawer, backdrop, closeButton, refreshButton, inviteForm } = this.nodes;
    refreshButton.addEventListener("click", () => this._load());
    backdrop.addEventListener("click", () => this.close());
    closeButton.addEventListener("click", () => this.close());
    inviteForm.addEventListener("submit", (event) => {
      event.preventDefault();
      this._createInvite(new FormData(inviteForm));
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && drawer.classList.contains("is-open")) this.close();
    });
  }

  _status(text, tone = "") {
    this.nodes.status.textContent = text;
    this.nodes.status.dataset.tone = tone;
  }

  /** Run a change, report a refusal, and show the lists as they are now. */
  async _act(change, done) {
    try {
      await change();
      if (done) this._status(done, "ok");
    } catch (error) {
      this._status(error.message, "error");
    }
    await this._load();
  }

  async _load() {
    const [reviews, users, invites] = await Promise.all([api.reviews(), api.adminUsers(), api.adminInvites()]);
    const names = new Map(users.map((user) => [user.id, user.username]));
    this._renderReviews(reviews);
    this._renderUsers(users);
    this._renderInvites(invites, names);
  }

  _renderReviews(reviews) {
    const decide = (review, decision) =>
      this._act(() => api.resolveReview(review.approval_id, decision), decision === "approve" ? "Approved" : "Denied");
    replace(
      this.nodes.reviews,
      reviews.length
        ? h(
            "table.adminTable",
            {},
            h("thead", {}, h("tr", {}, ["User", "Tool", "Held by", "Asked", "Expires", ""].map((title) => h("th", { text: title })))),
            h(
              "tbody",
              {},
              reviews.map((review) =>
                h(
                  "tr",
                  {},
                  h("td", { text: review.username }),
                  h("td", {}, h("code", { text: review.tool })),
                  // "chain" alone: the call itself passed, the run's earlier actions did not.
                  h("td", { text: (review.objected || []).join(", ") || "—" }),
                  h("td", { text: when(review.created_at) }),
                  h("td", { text: when(review.expires_at) }),
                  h(
                    "td.adminTable__actions",
                    {},
                    h("button.btn.btn--primary.btn--xs", { type: "button", text: "Approve", on: { click: () => decide(review, "approve") } }),
                    h("button.btn.btn--danger.btn--xs", { type: "button", text: "Deny", on: { click: () => decide(review, "deny") } }),
                  ),
                ),
              ),
            ),
          )
        : h("p.pane__empty", { text: "Nothing waits for review." }),
    );
  }

  _renderUsers(users) {
    const me = this.currentUser()?.id;
    replace(
      this.nodes.users,
      h(
        "table.adminTable",
        {},
        h("thead", {}, h("tr", {}, ["User", "Role", "State", "Turns today", ""].map((title) => h("th", { text: title })))),
        h(
          "tbody",
          {},
          users.map((user) => {
            const otherRole = user.role === "admin" ? "user" : "admin";
            return h(
              "tr",
              { class: user.disabled ? "is-muted" : "" },
              h("td", {}, h("strong", { text: user.username }), user.id === me ? h("span.adminTable__you", { text: " (you)" }) : null),
              h("td", { text: user.role }),
              h("td", { text: user.disabled ? "disabled" : "active" }),
              h("td", { text: String(user.turns_today ?? 0) }),
              h(
                "td.adminTable__actions",
                {},
                h("button.btn.btn--ghost.btn--xs", {
                  type: "button",
                  text: `Make ${otherRole}`,
                  on: { click: () => this._act(() => api.updateUser(user.id, { role: otherRole }), `${user.username} is now ${otherRole === "admin" ? "an admin" : "a user"}`) },
                }),
                user.id === me
                  ? null
                  : h("button.btn.btn--ghost.btn--xs", {
                      type: "button",
                      text: user.disabled ? "Enable" : "Disable",
                      on: {
                        click: () =>
                          this._act(
                            () => api.updateUser(user.id, { disabled: !user.disabled }),
                            `${user.username} ${user.disabled ? "enabled" : "disabled"}`,
                          ),
                      },
                    }),
              ),
            );
          }),
        ),
      ),
    );
  }

  _renderInvites(invites, names) {
    const now = Date.now() / 1000;
    const state = (invite) =>
      invite.used_at ? `used by ${names.get(invite.used_by) ?? "a removed user"}` : invite.expires_at <= now ? "expired" : "open";
    replace(
      this.nodes.invites,
      invites.length
        ? h(
            "table.adminTable",
            {},
            h("thead", {}, h("tr", {}, ["Note", "Role", "Valid until", "State", ""].map((title) => h("th", { text: title })))),
            h(
              "tbody",
              {},
              invites.map((invite) =>
                h(
                  "tr",
                  { class: state(invite) === "open" ? "" : "is-muted" },
                  h("td", { text: invite.note || "-" }),
                  h("td", { text: invite.role }),
                  h("td", { text: when(invite.expires_at) }),
                  h("td", { text: state(invite) }),
                  h(
                    "td.adminTable__actions",
                    {},
                    invite.used_at
                      ? null
                      : h("button.btn.btn--ghost.btn--xs", {
                          type: "button",
                          text: "Revoke",
                          on: { click: () => this._act(() => api.revokeInvite(invite.id), "Invite revoked") },
                        }),
                  ),
                ),
              ),
            ),
          )
        : h("p.pane__empty", { text: "No invites yet." }),
    );
  }

  async _createInvite(data) {
    let invite = null;
    await this._act(async () => {
      invite = await api.createInvite({
        role: data.get("role"),
        days: Number(data.get("days")),
        note: String(data.get("note") ?? "").trim(),
      });
    });
    if (!invite) return;
    const link = `${location.origin}/login#invite=${invite.code}`;
    replace(
      this.nodes.created,
      h("p.field__hint", { text: "Send this link to the person you invite. It is shown only now." }),
      h(
        "div.inviteLink",
        {},
        h("code.inviteLink__url", { text: link }),
        h("button.btn.btn--primary.btn--xs", { type: "button", text: "Copy", on: { click: () => copyText(link, "Invite link copied") } }),
      ),
    );
    this.nodes.created.hidden = false;
    this.nodes.inviteForm.reset();
  }
}
