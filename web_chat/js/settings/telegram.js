/**
 * Telegram, in two parts that share one view.
 *
 * The bot: an admin pastes the token BotFather gave them. It is sent once and
 * never comes back - the page learns a hint (`…5kfWw`) and where the token
 * comes from. The server seals it in its vault and starts the channel at once,
 * no restart; "Remove" stops it again.
 *
 * The link: the user asks for a single-use code and follows the t.me link that
 * spends it, or unlinks the chat. A server whose channel is off says so
 * instead of offering a code that would not work.
 *
 * The admin token card is drawn only for an admin (`isAdmin`); everybody sees
 * the link card, on the /telegram page as well as in the settings drawer.
 */

import { h, replace } from "../lib/dom.js";

/** The panel's own stylesheet, once per page: the drawer and the page share it. */
function ensureStyles() {
  const href = "/static/js/settings/telegram.css";
  if (document.querySelector(`link[href="${href}"]`)) return;
  document.head.append(h("link", { rel: "stylesheet", href }));
}

/** The server's JSON, with its refusal reason - `detail` - as the error. */
async function call(url, options = {}) {
  const response = await fetch(url, { ...options, headers: { "Content-Type": "application/json", ...(options.headers ?? {}) } });
  const payload = await response.json().catch(() => null);
  if (response.status === 401) {
    location.assign("/login");
    throw new Error("Signed out");
  }
  if (!response.ok) throw new Error(payload?.detail || `Request failed (${response.status})`);
  return payload;
}

const json = (method, body) => ({ method, body: JSON.stringify(body) });

/** "Linked as @alice" / "Not linked" / the channel is off. */
function linkLine(state) {
  if (!state.enabled) return state.available ? "The channel is off: an administrator turns it on." : "This server keeps no Telegram channel.";
  if (!state.link) return "Not linked yet.";
  const who = state.link.username ? `@${state.link.username}` : `chat ${state.link.chat_id}`;
  return `Linked as ${who}.`;
}

export class TelegramPanel {
  /** @param {{isAdmin?: boolean}} options */
  constructor({ isAdmin = false } = {}) {
    this.isAdmin = isAdmin;
    this.root = h("div.telegramPanel");
  }

  /** The element to put on the page; it loads itself. */
  mount() {
    ensureStyles();
    void this.reload();
    return this.root;
  }

  async reload() {
    let state;
    let admin = null;
    try {
      state = await call("/api/telegram/state", { cache: "no-store" });
      if (this.isAdmin) admin = await call("/api/admin/telegram", { cache: "no-store" });
    } catch (error) {
      replace(this.root, h("p.telegramNote", { text: error.message, dataset: { tone: "error" } }));
      return;
    }
    this.state = state;
    this.admin = admin;
    replace(this.root, ...(admin ? [this._botCard(admin)] : []), this._linkCard(state));
  }

  // -- the bot token (admins) ----------------------------------------------
  _botCard(admin) {
    const field = h("input.control", {
      type: "password",
      name: "grid-telegram-token",
      placeholder: "123456789:AA…",
      autocomplete: "off",
      spellcheck: "false",
      "data-1p-ignore": true,
      "data-lpignore": "true",
    });
    if (!admin.can_store) field.disabled = true;
    const status = h("p.telegramNote");
    const paint = (value) => {
      const state = value.enabled ? "on" : "off";
      const from = value.source === "env" ? "the environment (it wins over the stored token)" : value.source === "vault" ? "the stored token" : "no token";
      status.textContent = `Channel ${state} · ${from}${value.hint ? ` · ${value.hint}` : ""}${value.bot_username ? ` · @${value.bot_username}` : ""}${value.error ? ` · ${value.error}` : ""}`;
      status.dataset.tone = value.enabled ? "ok" : "";
    };
    paint(admin);

    const save = h("button.btn.btn--primary", { type: "button", text: "Save token" });
    const remove = h("button.btn.btn--ghost", { type: "button", text: "Remove", disabled: !admin.hint });
    if (!admin.can_store) save.disabled = true;
    const note = h("p.telegramNote");
    if (!admin.can_store) {
      note.textContent = "This server cannot store a token: GRID_SECRETS_KEY is not set. Only GRID_TELEGRAM_BOT_TOKEN in the environment turns the channel on here.";
    }
    const act = async (change) => {
      note.textContent = "";
      try {
        const value = await change();
        this.admin = value;
        paint(value);
        field.value = "";
        remove.disabled = !value.hint;
        // The user's link card below says the channel is on now, and offers a code.
        void this.reload();
      } catch (error) {
        note.textContent = error.message;
        note.dataset.tone = "error";
      }
    };
    save.addEventListener("click", () => {
      const token = field.value.trim();
      if (!token) {
        note.textContent = "Paste the token BotFather gave you.";
        return;
      }
      void act(async () => {
        return call("/api/admin/telegram", json("PUT", { token }));
      });
    });
    remove.addEventListener("click", () =>
      void act(async () => call("/api/admin/telegram", { method: "DELETE" })),
    );

    return h(
      "section.card.telegramCard",
      {},
      h("h3", { text: "Bot" }),
      h("p.card__hint", {
        text: "The token from @BotFather. It is stored sealed in the server's vault and never shown again - only its tail. The channel starts and stops as you save or remove it; GRID_TELEGRAM_BOT_TOKEN in the environment overrides it.",
      }),
      status,
      h("div.telegramRow", {}, field, save, remove),
      note,
    );
  }

  // -- the user's own link --------------------------------------------------
  _linkCard(state) {
    const body = h("div.telegramBody");
    const status = h("p.telegramNote", { text: linkLine(state) });
    const linked = Boolean(state.link);
    const code = h("div.telegramCode", { hidden: true });
    const actions = h("div.telegramRow");

    const link = h("button.btn.btn--primary", { type: "button", text: "Link Telegram", disabled: !state.enabled || linked });
    const unlink = h("button.btn.btn--ghost", { type: "button", text: "Unlink", disabled: !linked });
    link.addEventListener("click", async () => {
      status.dataset.tone = "";
      try {
        const made = await call("/api/telegram/link-code", json("POST", {}));
        replace(
          code,
          h("p.card__hint", { text: "Send this code to the bot, or open the link:" }),
          h("code.telegramCode__value", { text: made.code }),
          made.url
            ? h("a.btn.btn--primary", { href: made.url, target: "_blank", rel: "noreferrer", text: "Open Telegram" })
            : null,
        );
        code.hidden = false;
      } catch (error) {
        status.textContent = error.message;
        status.dataset.tone = "error";
      }
    });
    unlink.addEventListener("click", async () => {
      status.dataset.tone = "";
      try {
        const after = await call("/api/telegram/link", { method: "DELETE" });
        code.hidden = true;
        this.state = after;
        await this.reload();
      } catch (error) {
        status.textContent = error.message;
        status.dataset.tone = "error";
      }
    });
    actions.append(link, unlink);
    body.append(status, actions, code);

    return h(
      "section.card.telegramCard",
      {},
      h("h3", { text: "Your Telegram link" }),
      h("p.card__hint", {
        text: "Write to GRID from Telegram: a message there runs as an ordinary turn of your conversation, voice notes included. Linking spends a single-use code, good for ten minutes.",
      }),
      body,
    );
  }
}
