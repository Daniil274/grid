/**
 * The sign-in page: sign in, or join with an invite link.
 *
 * An invite link is `/login#invite=<code>`. The code travels in the fragment,
 * which browsers never send to a server, so it stays out of access logs and
 * Referer headers; the page reads it, then removes it from the address bar
 * and the history. The server sets the session cookie on success and the page
 * moves on to the chat.
 */

import { applyStoredTheme } from "./ui/theme.js";

applyStoredTheme();

const signIn = document.getElementById("sign-in");
const signUp = document.getElementById("sign-up");

/** The invite code from the address, removed from it at once. */
function takeInvite() {
  const code = new URLSearchParams(location.hash.slice(1)).get("invite");
  if (code) history.replaceState(null, "", location.pathname);
  return code;
}

async function post(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (response.ok) return;
  const payload = await response.json().catch(() => null);
  const detail = typeof payload?.detail === "string" ? payload.detail : null;
  if (response.status === 429) throw new Error(detail || "Too many attempts. Try again later.");
  throw new Error(detail || `Something went wrong (${response.status}). Try again.`);
}

/** Run *submit* for *form*, showing progress and any error in the form. */
function handle(form, submit) {
  const error = form.querySelector(".authError");
  const button = form.querySelector("button[type=submit]");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    button.disabled = true;
    try {
      await submit(new FormData(form));
      location.replace("/");
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
      button.disabled = false;
    }
  });
}

handle(signIn, (data) =>
  post("/api/auth/login", { username: data.get("username").trim(), password: data.get("password") }),
);

const invite = takeInvite();
if (invite) {
  signIn.hidden = true;
  signUp.hidden = false;
  document.getElementById("auth-lead").textContent = "You are invited. Choose a username and a password.";
  document.getElementById("foot-sign-in").hidden = true;
  document.getElementById("foot-sign-up").hidden = false;
  handle(signUp, (data) => {
    if (data.get("password") !== data.get("repeat")) throw new Error("The passwords differ.");
    return post("/api/auth/register", {
      invite,
      username: data.get("username").trim(),
      password: data.get("password"),
    });
  });
}

document.querySelector("form:not([hidden]) input")?.focus();
