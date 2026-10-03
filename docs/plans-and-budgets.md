# Plans, keys and dollar budgets

How a multi-user Grid decides whose key pays for a call, which models a user may use, and how much they may spend. The README has the configuration; this is the design and what it does and does not protect.

## Pieces

| Piece | Where | What it does |
|---|---|---|
| Tier | `users.tier`, config `tiers` | A named plan: pool, own credentials, model globs, limits. Read at every use, so a change applies at once. |
| `Plans` / `Entitlement` | `web_chat/entitlements.py` | Resolves a user (role, tier) to what they may use now. No `tiers` in the config = one implicit plan, as before. |
| `UserCredentials` | `web_chat/entitlements.py` | A `CredentialSource`: the user's own credential if their plan allows it, else the plan's pool, else a clear refusal. |
| `ModelAccess` | `core/model_access.py` | Builds every client of a factory: signs each request through `ManagedAuth` (no secret stored in a client), filters models by plan, reports spend. |
| `MeteredTransport` | `core/metering.py` | Reads `usage` off every model response (JSON or SSE) at the HTTP layer, whatever SDK made the call. |
| Pricing | `core/pricing.py`, `web_chat/prices.py` | `reported` / `computed` / `estimated` / `subscription` cost in micro-dollars. |
| Usage record | `web_chat/usage.py` | One row per call with cost, basis, credential source and whether the operator was charged. |
| Budgets | `web_chat/limits.py`, `web_chat/session.py` | Day and month budgets are checked before a turn; the per-turn budget stops a turn at its next step. |
| Vault | `web_chat/vault.py` | Fernet-sealed user credentials; subscription logins refreshed under a per-user lock. |
| ChatGPT | `web_chat/chatgpt.py`, `web_chat/chatgpt_api.py` | Sign in with ChatGPT (PKCE, dynamic client, ID-token verification). |

## Rules the code keeps

- **Fail closed.** A user with no credential for a provider is told so. There is no fallback from "their key" to "the pool" or from a pool to the operator's key; a pool whose variable is unset serves nobody. (`tests/test_entitlements.py`)
- **A key goes only where it belongs.** Pool keys are the operator's providers' own variables, and private systems must use the server's providers unchanged (`core/system_store.py`). A user's key is matched to a provider by its base URL against a fixed preset list, and verified against that address without following redirects.
- **Nothing secret leaves the server.** The interface gets a hint. Stored secrets name their owner and kind inside the ciphertext, so a copied row does not open for another user.
- **Money is integers.** Micro-dollars; a token at $1 per million tokens is one micro-dollar.
- **The operator's budget counts what the operator paid.** Own keys and subscriptions are recorded (value of the call) but not charged.

## Known limits

- A cost is known after a call, so a turn can pass its budget by one call, and parallel turns by one call each (bounded by `running_turns`). Put a credit limit on each pool's key at the provider as the real ceiling.
- A call cut short before the provider's usage arrives (a stopped stream) is not recorded: nothing is known to bill.
- Router and policy-gate calls run on the operator's `default` keys, outside the user's plan, and are not metered per user yet.
- Compaction uses chat completions; a model on a ChatGPT plan has none, so set `compact.summary_model` to an ordinary provider.
- Sign in with ChatGPT works only where OpenAI allows it: open-source apps on the user's own machine, with a loopback callback. For a remotely hosted server OpenAI requires approval of the app; Grid does not work around that. The routes refuse a browser that did not reach Grid at `127.0.0.1` from the same machine. The flow and the plan's request limits come from OpenAI's documentation (https://developers.openai.com/siwc/token-sharing-open-source); verify them with `python -m web_chat.chatgpt check` on your own account before relying on them.
- The revocation endpoint's path is not given in OpenAI's text (`web_chat/chatgpt.py`, `REVOKE_URL`); revocation is best effort, and the stored copy is deleted regardless.

## Operating it

1. `python -m web_chat.vault new-key` and put it in `.env` as `GRID_SECRETS_KEY` (keep it apart from backups of `accounts.db`; losing it loses the stored credentials).
2. Create one provider key per pool at the provider, with a credit limit, and name them in `.env` and `pools`.
3. `python -m web_chat.prices refresh --out <file>`, set `pricing.prices_file`, and rerun when prices change. Models without a price are charged `pricing.unknown_price`.
4. Existing users have no tier and fall to `default_tier`; assign them with `grid-web-chat accounts set-tier` or the users table.
