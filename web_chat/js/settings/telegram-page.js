/**
 * The /telegram page: the Telegram section of the user's settings, on its own.
 *
 * A server with accounts gives ordinary users no settings drawer, so the
 * channel's own page is where they link and unlink. Admins get the bot card
 * too; the server tells the page who is asking in `bootstrap.user`.
 */

import { TelegramPanel } from "./telegram.js";

async function main() {
  const root = document.getElementById("telegram-root");
  let isAdmin = false;
  try {
    const response = await fetch("/api/chat/bootstrap", { cache: "no-store" });
    const bootstrap = await response.json();
    isAdmin = bootstrap?.user?.role === "admin";
  } catch {
    // A page that cannot read the bootstrap is still worth drawing: the
    // routes answer for themselves.
  }
  root.replaceWith(new TelegramPanel({ isAdmin }).mount());
}

void main();
