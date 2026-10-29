# Telegram channel

GRID in a Telegram chat: messages run as ordinary turns of your own
conversation, voice notes are transcribed, calls waiting for approval arrive
as inline buttons, and reminders come back on their own. The channel is
**off by default** and only ever links through a one-time code.

## What it does

- **Text** - a message becomes a turn in a conversation of the user's space
  (`web_chat/telegram/runner.py`), so it is traced, checkpointed, counted
  against the user's limits and visible in the web chat afterwards.
- **Voice** - a voice note is downloaded, converted to WAV PCM16 mono 16 kHz
  (`ffmpeg`) and passed to the same speech service the web chat uses. Voice
  replies (WAV → Ogg/Opus) are off by default.
- **Approvals** - a call held for the user's answer arrives as one message
  with *Approve* / *Decline*. The answer goes through
  `space.answer_action_review`, the same door the web chat uses.
- **Reminders** - a tick loop (`web_chat/telegram/reminders.py`) sends due
  reminders to the linked chat; one-off and repeating ones are supported.
- **Linking** - a signed-in user asks the web page for a code (good for ten
  minutes, single use) and sends it to the bot **in a private chat**. No other
  way in exists, and a group never gets one.

The token comes from the environment, or - without root, and without a
restart - from the web interface: an admin pastes it into **Configuration →
Telegram** (or the `/telegram` page) and the server seals it in its vault
(`web_chat/vault.py`, Fernet under `GRID_SECRETS_KEY`) before it reaches the
database. A token in the environment wins over a stored one. Either way it is
never written to a config file, never sent back to a browser (the page gets a
hint, `…5kfWw`, and where the token comes from) and never reaches a log: the
client holds the `httpx`/`httpcore` request loggers above `CRITICAL` and
redacts the token, so the request URL that carries it cannot be printed at any
level.

## Turn it on

1. **Make a bot.** In Telegram, talk to [@BotFather](https://t.me/BotFather),
   `/newbot`, choose a name and a username. BotFather answers with a token
   like `123456789:AA...`.

2. **Give the server the token** - one of two ways.

   **From the web interface (no root, no restart).** Sign in as an admin, open
   **Configuration → Telegram** (the same section is at `/telegram`, which any
   user may open) and paste the token into the *Bot* card. The server checks it
   with `getMe`, seals it in the vault and starts the channel at once; *Remove*
   stops it again. The vault needs `GRID_SECRETS_KEY` to be set (a single-user
   server makes its own key beside its records); without one the card says so
   and only the environment can turn the channel on.

   **From the environment** of the server process (not a config file):

   ```bash
   export GRID_TELEGRAM_ENABLED=1
   export GRID_TELEGRAM_BOT_TOKEN='123456789:AA...'
   # optional: voice replies need speech synthesis and ffmpeg
   export GRID_TELEGRAM_VOICE_REPLIES=1
   ```

   A token in the environment is the operator's word and wins over the stored
   one: with `GRID_TELEGRAM_ENABLED=1` it starts the channel, and with no
   `GRID_TELEGRAM_ENABLED` the channel stays off even if a token is present.
   `GRID_TELEGRAM_ENABLED=1` with an empty `GRID_TELEGRAM_BOT_TOKEN` is an
   error at startup unless a token is stored in the vault. A token set in the
   web interface needs no flag: setting it is the act of turning the channel
   on, removing it turns it off.

3. **Install `ffmpeg`** if you want voice notes in or out:

   ```bash
   sudo apt install ffmpeg        # Debian/Ubuntu
   brew install ffmpeg            # macOS
   ```

   Without it the channel still works; voice notes are refused with a message
   that says so. Voice replies also need `voice.enabled: true` in the system
   config and a working speech backend (see `docs/` for the web chat voice
   setup - it is the same service).

4. **Start the server** as usual (`python -m web_chat ...`). On a server with
   accounts every user links their own chat; the channel refuses accounts that
   are disabled, exactly as a planned restart refuses their turns.

5. **Link a chat.** In the web chat open **Configuration → Telegram** (admins)
   or `/telegram` (everyone) and press **Link Telegram**: the page shows the
   code and a `https://t.me/<bot>?start=<code>` link that spends it. The same
   by hand, with the API:

   ```bash
   curl -X POST http://127.0.0.1:8000/api/telegram/link-code \
        -H 'Cookie: <your session cookie>' -H 'Origin: http://127.0.0.1:8000'
   # {"code": "A1B2C3D4", "expires_at": 1761...,
   #  "bot_username": "my_grid_bot", "url": "https://t.me/my_grid_bot?start=A1B2C3D4"}
   ```

   Send that code to your bot in Telegram (or `/start A1B2C3D4`). The bot
   answers "Linked". Write to the bot from a **private chat**: a chat id is
   only ever linked when it is the sender's own id, so a code sent to a group
   or a channel links nobody (and nothing there is acted on). The same
   endpoints are used by the web page:

   | Method | Path | What it does |
   | --- | --- | --- |
   | `POST` | `/api/telegram/link-code` | a new single-use code, the bot's username and the t.me link |
   | `GET` | `/api/telegram/state` | whether the channel is on, the link, the chats, the reminders |
   | `DELETE` | `/api/telegram/link` | unlink and forget the chat's mapping |
   | `GET` | `/api/admin/telegram` | admins: on or off, where the token comes from, its hint |
   | `PUT` | `/api/admin/telegram` | admins: seal `{"token": "..."}` and start the channel |
   | `DELETE` | `/api/admin/telegram` | admins: forget the stored token |

   The admin routes are registered like every other admin API: behind
   `admins_only` of the identified user, so the role check and the origin
   check (POST/PUT/DELETE must come from the site's own pages) apply unchanged.
   These routes exist whether the channel runs or not - with it off they answer
   `enabled: false`, and a code is refused with `503` rather than a `404`.

   Asking for a second code invalidates the first; a code is spent as it is
   used, so a replay links nobody. A chat may try five codes per quarter of an
   hour; after that it is told to ask for a new one later, and the count
   survives a restart.

## Use it

Write a message, or send a voice note, and wait for the answer. While a turn
runs, a second message is answered with "I am still working on your previous
message" rather than queued.

Approvals:

- `Approval needed: <tool>` with **Approve** / **Decline** buttons. The same
  review is visible and answerable in the web chat; answering in one place
  settles it everywhere. A button is only good in the private chat it was sent
  to, for the person that chat belongs to, on the message that carried it:
  forwarding the message (Telegram copies inline keyboards with it) answers
  "This button does not belong to this chat" and resolves nothing.

Reminders (times are UTC):

```
/remind 10m stretch             in ten minutes
/remind 2h call Anna            in two hours
/remind 2h30m review            in two and a half hours
/remind 18:30 stand-up          the next 18:30
/remind daily 09:00 plan        every day at 09:00
/remind every 90m water         every ninety minutes
/reminders                      what is waiting, with ids
/cancel <id>                    drop one
/cancel all                     drop everything
/help                           the same list in the chat
/unlink                         disconnect this chat from GRID
```

## How it is built

| File | Responsibility |
| --- | --- |
| `web_chat/telegram/config.py` | settings: the environment first, the vault after it, off by default |
| `web_chat/telegram/secrets.py` | the bot token sealed in the vault, hinted, never printed |
| `web_chat/telegram/manager.py` | owns the channel: starts, stops and rebuilds it at runtime |
| `web_chat/telegram/store.py` | SQLite `telegram.db`: links, code hashes, chats, approvals, reminders, poll cursor |
| `web_chat/telegram/bot.py` | Bot API over `httpx` (long poll, messages, buttons, voice, files) |
| `web_chat/telegram/runner.py` | one message = one turn, with `HeadlessSocket` + `ChatSession._start` |
| `web_chat/telegram/confirm.py` | `space.awaiting_approval` → buttons → `space.answer_action_review` |
| `web_chat/telegram/audio.py` | Ogg/Opus ↔ WAV PCM16 mono through `ffmpeg` pipes |
| `web_chat/telegram/reminders.py` | tick loop, parsing, one-off and repeating reminders |
| `web_chat/telegram/service.py` | updates, commands, linking, turns, callbacks, lifecycle |
| `web_chat/telegram/http.py` | `/api/telegram/*`, `/api/admin/telegram`, the `/telegram` page |
| `web_chat/js/settings/telegram.js` | the Telegram section: the bot card (admins) and the user's link |

The server holds a `TelegramManager` (`web_chat/telegram/manager.py`), not a
channel: it reads the token, starts the channel on it, and on a change stops
the old poller, reminders and bot client and builds a fresh one - so the
channel comes and goes without the server being restarted. `_lifespan` starts
it and closes it with everything else; `web_chat/__main__.py` gives it
`telegram.db` in the server's data directory (`records/telegram.db` for the
single-user server, `<data-dir>/telegram.db` with accounts).

## Privacy and limits

- Only a hash of a link code is stored; the code itself is never kept.
- Only a private chat can be linked, and only the person it belongs to is
  heard: `links.tg_user_id` records that identity, and a message, a code or a
  button from anyone else - or from a group - is refused before it reaches a
  space.
- A chat is linked to exactly one user. Linking it again, or
  `DELETE /api/telegram/link`, drops the old chat's conversation mapping,
  waiting approvals and reminders in the same transaction, so a reminder is
  never delivered to a chat the user has left.
- The bot token is sealed with the server's vault key; the database holds the
  sealed bytes, never the token, and the web interface only ever sees a hint.
- The bot token is in every request URL, so `httpx` and `httpcore` are held
  above `CRITICAL` and their records are redacted: no level prints the URL. A
  transport failure is reported by its class name only.
- A chat gets five link code attempts per quarter of an hour; the count is in
  `telegram.db`, not in memory.
- Telegram messages are capped at ~4 000 characters; long answers are split on
  paragraph breaks.
- A turn that runs longer than 30 minutes is asked to stop at a safe point,
  and its conversation keeps everything already done.
- There is no webhook and no new dependency: `httpx` long polling only.

## Turning it off

Press **Remove** in **Configuration → Telegram** (or `/telegram`): the channel
stops at once and the stored token is gone. Or unset `GRID_TELEGRAM_ENABLED`
(or the token) and restart. With the channel off the routes report
`enabled: false`, and `telegram.db` is left untouched; deleting that file
forgets every link.

## Troubleshooting

- **`GRID_TELEGRAM_ENABLED is set but GRID_TELEGRAM_BOT_TOKEN is empty`** -
  export the token BotFather gave you, store one in the web interface, or unset
  the flag.
- **The token card is disabled** - `GRID_SECRETS_KEY` is not set, so this server
  cannot store credentials; only the environment can turn the channel on.
- **The bot does not answer** - check the server log for
  `Telegram long poll failed`; a wrong token makes Telegram answer `401`, which
  is logged as a refusal without the token.
- **"This chat is not linked"** - ask the web page for a new code; codes live
  ten minutes and work once.
- **"Voice notes need ffmpeg"** - install `ffmpeg`, or send text.
- **"This server is closed for your account"** - the account is disabled; the
  channel refuses it just as the web chat refuses its sign-in.
