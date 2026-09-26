/**
 * Changing one's own password. The server checks the current one and the new
 * one's strength, and ends the user's other sessions; this one stays signed in.
 */

import { api } from "../net/api.js";

export class PasswordDrawer {
  /** @param {object} nodes drawer, backdrop, closeButton, form, status */
  constructor(nodes) {
    this.nodes = nodes;
    const { drawer, backdrop, closeButton, form } = nodes;
    backdrop.addEventListener("click", () => this.close());
    closeButton.addEventListener("click", () => this.close());
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      this._submit(new FormData(form));
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && drawer.classList.contains("is-open")) this.close();
    });
  }

  open() {
    this.nodes.form.reset();
    this._status("");
    this.nodes.drawer.classList.add("is-open");
    this.nodes.form.querySelector("input")?.focus();
  }

  close() {
    this.nodes.drawer.classList.remove("is-open");
  }

  _status(text, tone = "") {
    this.nodes.status.textContent = text;
    this.nodes.status.dataset.tone = tone;
  }

  async _submit(data) {
    if (data.get("new") !== data.get("repeat")) {
      this._status("The new passwords differ.", "error");
      return;
    }
    const button = this.nodes.form.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      await api.changePassword(data.get("current"), data.get("new"));
      this.nodes.form.reset();
      this._status("Password changed. Your other sessions were signed out.", "ok");
    } catch (error) {
      this._status(error.message, "error");
    } finally {
      button.disabled = false;
    }
  }
}
