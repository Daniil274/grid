/**
 * "Plan & keys": what the user's plan is, what it has cost this day and month,
 * and the credentials they bring themselves (web_chat/credentials_api.py).
 *
 * A key is sent to the server once and never shown again - only a hint (its
 * last characters). Signing in with ChatGPT leaves for OpenAI and comes back
 * to the page with `#chatgpt=connected` or `#chatgpt=error:<why>`, which opens
 * this drawer with the result.
 */

import { h, replace } from "../lib/dom.js";
import { api } from "../net/api.js";
import { toast } from "../ui/toast.js";

const usd = (amount) => (amount >= 1 ? `$${amount.toFixed(2)}` : `$${amount.toFixed(4)}`);

export class PlanDrawer {
  /** @param {object} nodes drawer, backdrop, closeButton, body, status, openButton */
  constructor(nodes) {
    this.nodes = nodes;
    nodes.backdrop.addEventListener("click", () => this.close());
    nodes.closeButton.addEventListener("click", () => this.close());
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && nodes.drawer.classList.contains("is-open")) this.close();
    });
  }

  /** Offer the drawer when the server has a plan to show; open it for a ChatGPT sign-in's result. */
  async start() {
    let plan;
    try {
      plan = await api.plan();
    } catch {
      return; // a server without credentials support: no button
    }
    this.nodes.openButton.hidden = false;
    const result = new URLSearchParams(location.hash.slice(1)).get("chatgpt");
    if (result) {
      history.replaceState(null, "", location.pathname + location.search);
      await this.open();
      if (result === "connected") toast("Signed in with ChatGPT", { tone: "success" });
      else this._status(result.replace(/^error:/, ""), "error");
    }
    return plan;
  }

  async open() {
    this.nodes.drawer.classList.add("is-open");
    this._status("");
    await this._load();
  }

  close() {
    this.nodes.drawer.classList.remove("is-open");
  }

  _status(text, tone = "") {
    this.nodes.status.textContent = text;
    this.nodes.status.dataset.tone = tone;
  }

  async _load() {
    try {
      this._render(await api.plan());
    } catch (error) {
      this._status(error.message, "error");
    }
  }

  async _act(change, done) {
    try {
      await change();
      if (done) this._status(done, "ok");
    } catch (error) {
      this._status(error.message, "error");
    }
    await this._load();
  }

  _budget(label, spent, limit) {
    return h("li", {}, `${label}: ${usd(spent)}`, limit ? ` of ${usd(limit)}` : " (no limit)");
  }

  _render(plan) {
    const limits = plan.limits || {};
    replace(
      this.nodes.body,
      h(
        "section.card",
        {},
        h("h3", { text: plan.label }),
        h(
          "ul.planBudget",
          {},
          this._budget("Today", plan.spent.day_usd, limits.usd_per_day),
          this._budget("This month", plan.spent.month_usd, limits.usd_per_month),
        ),
        h("p.field__hint", { text: "Counts only calls billed to the server's keys; your own keys and plans cost it nothing." }),
      ),
      plan.credentials.length
        ? h(
            "section.card",
            {},
            h("h3", { text: "Your own keys" }),
            plan.vault_enabled ? null : h("p.field__hint", { text: "This server cannot store keys: the operator has not set GRID_SECRETS_KEY." }),
            ...plan.credentials.map((entry) => (entry.kind === "chatgpt" ? this._chatgpt(plan, entry) : this._key(plan, entry))),
          )
        : h("section.card", {}, h("p.pane__empty", { text: "Your plan uses the server's keys." })),
    );
  }

  _key(plan, entry) {
    const input = h("input.control", { type: "password", autocomplete: "off", placeholder: "Paste a key", maxlength: 400, disabled: !plan.vault_enabled });
    return h(
      "div.field",
      {},
      h("span.field__label", { text: entry.label }),
      entry.connected
        ? h(
            "div.inviteLink",
            {},
            h("code", { text: entry.hint || "stored" }),
            h("button.btn.btn--ghost.btn--xs", { type: "button", text: "Remove", on: { click: () => this._act(() => api.removeCredential(entry.kind), `${entry.label} key removed`) } }),
          )
        : h(
            "div.inviteLink",
            {},
            input,
            h("button.btn.btn--primary.btn--xs", {
              type: "button",
              text: "Check and save",
              disabled: !plan.vault_enabled,
              on: { click: () => input.value.trim() && this._act(() => api.storeKey(entry.kind, input.value.trim()), `${entry.label} key saved`) },
            }),
          ),
    );
  }

  _chatgpt(plan, entry) {
    const needsAgain = entry.connected && entry.status === "needs_reauth";
    const connect = async () => {
      try {
        const { url } = await api.connectChatGPT();
        location.assign(url);
      } catch (error) {
        this._status(error.message, "error");
      }
    };
    return h(
      "div.field",
      {},
      h("span.field__label", { text: entry.label }),
      entry.connected && !needsAgain
        ? h(
            "div.inviteLink",
            {},
            h("code", { text: entry.hint || "signed in" }),
            h("button.btn.btn--ghost.btn--xs", { type: "button", text: "Sign out", on: { click: () => this._act(() => api.removeCredential("chatgpt"), "Signed out of ChatGPT") } }),
          )
        : h(
            "div.inviteLink",
            {},
            needsAgain ? h("span.field__hint", { text: "The sign-in expired." }) : null,
            h("button.btn.btn--primary.btn--xs", { type: "button", text: "Sign in with ChatGPT", on: { click: connect } }),
          ),
      h("span.field__hint", { text: "Spends your ChatGPT Plus or Pro allowance. Works only when Grid runs on your own computer, opened at 127.0.0.1." }),
    );
  }
}
