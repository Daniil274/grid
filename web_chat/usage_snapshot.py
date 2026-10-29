"""One picture of every subscription and key a user has configured.

The Usage page and the lazy agents ask the same question - how much of each
allowance is used, and when it frees up - and must not answer it differently.
This module is that one answer:

- **Claude Code subscription.** Its windows arrive with every Claude answer
  (``core.quota_telemetry``); the latest reading is shown. There is no
  documented non-interactive command that reads them without a model call, so
  an explicit refresh runs one tiny prompt through the same locked-down runner
  (``ClaudeQuotaProbe``) and says that it spends a little of the plan.
- **ChatGPT plan.** Read from OpenAI's plan usage endpoint on request
  (``web_chat.chatgpt_quota``); reading costs nothing, so opening the Usage
  page, the lazy page or planning a due ChatGPT step reads it again once its
  TTL has passed. Lazy agents plan on that reading instead of their own token
  estimate as soon as one is available.
- **Own API keys.** OpenRouter describes a key at ``GET /api/v1/key``; the
  other presets say nothing about a key's remaining quota, and that is stated
  as *unavailable* instead of invented. Every offered credential is listed,
  connected or not.

A source that cannot be read carries no numbers: ``status`` says ``unavailable``
or ``unknown`` and ``note`` says why. Secrets never leave this module - a source
carries only whether it is connected and the hint the Plan page already shows.

The machine's Claude Code login is the operator's own: it is listed, and can be
read, only on the explicit local, single-user server (``local_cli_login``). Its
probe is one model call, so it never runs on a page view, needs confirmation,
and is locked and cooled down for the credential as a whole, not per account.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional

import httpx

from core.credentials import PROVIDER_PRESETS, preset_for
from core.lazy.situation import FRESH
from core.lazy.windows import WINDOWS
from core.quota_telemetry import CLAUDE_CODE, QuotaReading, QuotaTelemetry, claude_reading, telemetry
from web_chat.chatgpt_quota import QuotaUnavailable, fetch_usage as fetch_chatgpt_usage
from web_chat.credentials_api import LABELS as KEY_LABELS
from web_chat.lazy.accounts import CHATGPT, estimated_reading
from web_chat.limits import utc_day_start, utc_month_start

logger = logging.getLogger("grid.usage_snapshot")

OPENROUTER_URL = "https://openrouter.ai/api/v1/key"
#: A reading younger than this is shown as live, matching the lazy agents' own mode.
LIVE = FRESH
#: Two explicit refreshes of one source are at least this far apart.
COOLDOWN = 15.0
#: A free source is read again by a page view or a lazy plan at most this often.
AUTOFETCH = 60.0
#: What the Claude probe asks for: a model call is the only way the CLI reports its windows.
PROBE_PROMPT = "Reply with the single word OK."


class SnapshotError(ValueError):
    """A refresh request the sources cannot take, in words for the user."""


class RefreshUnavailable(Exception):
    """The source cannot be read now; the message is safe to show the user."""


class ClaudeQuotaProbe:
    """One explicit, tiny Claude Code run, only to hear the CLI report its windows.

    It goes through :class:`core.claude_cli.ClaudeCodeRunner`, so the
    subscription login is required, every API-key/cloud credential is stripped
    from the child environment, and built-in tools, MCP servers and host
    settings are off. Nothing but the quota reading is kept.
    """

    def __init__(
        self,
        *,
        binary: Optional[str] = None,
        working_directory: Optional[str] = None,
        quota: Optional[QuotaTelemetry] = None,
        clock: Callable[[], float] = time.time,
        timeout: float = 60.0,
        runner: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.binary = binary
        self.working_directory = working_directory
        self.quota = quota or telemetry()
        self.clock = clock
        self.timeout = timeout
        #: How a runner is built; core.claude_cli.ClaudeCodeRunner unless a test supplies one.
        self._runner = runner
        #: The machine's login is one credential: two probes must never overlap.
        self._lock = asyncio.Lock()

    async def __call__(self) -> QuotaReading:
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=self.timeout)
        except asyncio.TimeoutError:
            raise RefreshUnavailable("Another read of the Claude Code plan is still running.") from None
        try:
            return await self._read()
        finally:
            self._lock.release()

    async def _read(self) -> QuotaReading:
        runner = self._runner
        if runner is None:
            from core.claude_cli import ClaudeCodeRunner

            runner = ClaudeCodeRunner(binary=self.binary, working_directory=self.working_directory)
        elif isinstance(runner, type):
            runner = runner()
        started = self.clock()
        reported: Optional[QuotaReading] = None

        async def run() -> None:
            nonlocal reported
            stream = runner.stream(PROBE_PROMPT)
            try:
                async for event in stream:
                    if event.get("type") == "rate_limit_event":
                        # Read the report here as well as publishing it: the event is
                        # the answer, whether or not the bridge also stores it for the
                        # process. A malformed one leaves the search to the next event.
                        reported = claude_reading(event.get("rate_limit_info"), now=self.clock())
                        if reported is not None:
                            self.quota.publish(reported)
                        return
            finally:
                # The first report is the answer: close the CLI stream here, so its
                # process is terminated now instead of at garbage collection.
                close = getattr(stream, "aclose", None)
                if close is not None:
                    with suppress(asyncio.CancelledError, Exception):
                        await close()

        try:
            await asyncio.wait_for(run(), timeout=self.timeout)
        except asyncio.TimeoutError:
            raise RefreshUnavailable("Claude Code did not report its windows in time.") from None
        except Exception as exc:  # noqa: BLE001 - the CLI's words are the answer
            text = str(exc).strip() or type(exc).__name__
            raise RefreshUnavailable(f"Claude Code could not report its windows: {text[:300]}") from None
        reading = reported or self.quota.latest(CLAUDE_CODE)
        if reading is None or reading.observed_at < started:
            raise RefreshUnavailable("Claude Code answered without reporting its quota windows.")
        return reading


async def fetch_openrouter_key(
    vault: Any,
    user_id: str,
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    now: Optional[float] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """The user's OpenRouter key as ``GET /api/v1/key`` describes it (no secret included)."""
    if vault is None or not getattr(vault, "enabled", False):
        raise RefreshUnavailable("This server cannot store keys (GRID_SECRETS_KEY is not set).")
    credential = await vault.credential(user_id, "openrouter")
    if credential is None:
        raise RefreshUnavailable("No OpenRouter key is connected. Add one on the Plan page.")
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, trust_env=False, transport=transport
        ) as http:
            response = await http.get(OPENROUTER_URL, headers={"Authorization": f"Bearer {credential.secret}"})
    except httpx.HTTPError as exc:
        raise RefreshUnavailable(f"Could not reach OpenRouter: {type(exc).__name__}.") from None
    if response.status_code in (401, 403):
        raise RefreshUnavailable("OpenRouter did not accept this key. Replace it on the Plan page.")
    if not response.is_success:
        raise RefreshUnavailable(f"OpenRouter answered {response.status_code} when asked about the key.")
    try:
        data = (response.json() or {}).get("data") or {}
    except ValueError:
        raise RefreshUnavailable("OpenRouter's answer was not readable.") from None
    if not isinstance(data, Mapping):
        raise RefreshUnavailable("OpenRouter's answer was not readable.")

    def amount(name: str) -> Optional[float]:
        value = data.get(name)
        return float(value) if isinstance(value, (int, float)) else None

    limits: Dict[str, Any] = {
        "limit_usd": amount("limit"),
        "remaining_usd": amount("limit_remaining"),
        "usage_usd": amount("usage"),
        "usage_daily_usd": amount("usage_daily"),
        "usage_weekly_usd": amount("usage_weekly"),
        "usage_monthly_usd": amount("usage_monthly"),
        "limit_reset": data.get("limit_reset"),
        "is_free_tier": data.get("is_free_tier"),
        "observed_at": time.time() if now is None else now,
    }
    return limits


def _window_view(reading: QuotaReading) -> List[dict]:
    order = {name: index for index, name in enumerate(WINDOWS)}
    windows = sorted(reading.windows.values(), key=lambda window: (order.get(window.name, 99), window.name))
    return [
        {
            "name": window.name,
            "label": WINDOWS[window.name].label if window.name in WINDOWS else window.name.replace("_", " ").title(),
            "used": round(window.used, 1),
            "remaining": round(max(0.0, 100.0 - window.used), 1),
            "resets_at": window.resets_at,
        }
        for window in windows
    ]


def _mode(reading: Optional[QuotaReading], now: float) -> str:
    if reading is None:
        return "unknown"
    return "live" if now - reading.observed_at <= LIVE else "stale"


class UsageSnapshot:
    """Every allowance of one user: what is configured, what is known, how to refresh it."""

    def __init__(
        self,
        plans: Any,
        config: Callable[[], Any],
        *,
        vault: Any = None,
        quota: Optional[QuotaTelemetry] = None,
        clock: Callable[[], float] = time.time,
        chatgpt_transport: Optional[httpx.AsyncBaseTransport] = None,
        key_transport: Optional[httpx.AsyncBaseTransport] = None,
        probe: Optional[Callable[[], Awaitable[QuotaReading]]] = None,
        spent_micro: Optional[Callable[[str, float], int]] = None,
        local_cli_login: Callable[[], bool] = lambda: False,
    ) -> None:
        self._plans = plans
        self._config = config
        self._vault = vault
        self._quota = quota or telemetry()
        self.clock = clock
        self._chatgpt_transport = chatgpt_transport
        self._key_transport = key_transport
        self._probe = probe
        #: What the user's calls have cost the operator so far, for the plan's own budget.
        self._spent_micro = spent_micro
        #: Whether this server is the local, single-user one whose CLI login is the user's own.
        self._local_cli_login = local_cli_login
        #: Active readings and key limits, newest per user and source.
        self._chatgpt: Dict[str, QuotaReading] = {}
        self._keys: Dict[tuple, Dict[str, Any]] = {}
        self._errors: Dict[tuple, str] = {}
        self._fetched: Dict[tuple, float] = {}
        self._locks: Dict[tuple, asyncio.Lock] = {}

    def _machine_login(self) -> bool:
        try:
            return bool(self._local_cli_login())
        except Exception:  # noqa: BLE001 - a broken check refuses the machine login
            return False

    # -- readings other modules plan on ---------------------------------------------
    def reading_for(self, user_id: str, settings: Any, usage: Any, now: float) -> tuple[QuotaReading, bool]:
        """(reading, estimated) for lazy agents: the active one when known, else the token estimate."""
        # Windows are a pool's own: only a ChatGPT pool may be planned on this reading.
        reading = self._chatgpt.get(user_id) if settings.account == CHATGPT else None
        if reading is not None:
            return reading, False
        return estimated_reading(usage, user_id, settings, now), True

    # -- what the page shows ---------------------------------------------------------
    def snapshot(self, user: Any) -> dict:
        now = self.clock()
        plans = self._plans() if callable(self._plans) else self._plans
        entitlement = plans.resolve(admin=user.is_admin, tier=user.tier)
        config = self._config()
        providers = self._used_providers(config, entitlement)
        auths = {
            name: getattr(getattr(config, "providers", {}).get(name), "auth", "api_key") for name in providers
        }
        sources: List[dict] = []
        # The CLI login of this machine is the operator's own: only an admin on the
        # explicit local server may see (and spend) it, until the provider is
        # opened to every user.
        if user.is_admin and self._machine_login() and any(auth == "claude" for auth in auths.values()):
            sources.append(self._claude_source(now))
        if (
            CHATGPT in entitlement.own_credentials
            or any(auth == CHATGPT for auth in auths.values())
            or self._stored(user, CHATGPT) is not None
        ):
            sources.append(self._chatgpt_source(user, now))
        # Every key the plan allows and every key already stored stays visible:
        # a key does not disappear because a plan stopped naming it.
        for kind in PROVIDER_PRESETS:
            if kind in entitlement.own_credentials or self._stored(user, kind) is not None:
                sources.append(self._key_source(user, kind, now))
        connected_keys = {source["id"] for source in sources if source["kind"] == "key" and source["connected"]}
        for name in providers:
            if auths.get(name) in ("claude", CHATGPT):
                continue
            if f"key:{name}" in connected_keys:
                continue
            provider = getattr(config, "providers", {}).get(name)
            preset = preset_for(getattr(provider, "base_url", "") or "")
            if preset is not None and f"key:{preset}" in connected_keys:
                continue  # the user's own key already pays for this provider
            sources.append(self._server_source(name, config, now))
        return {"now": now, "plan": self._plan_view(user, entitlement, now), "sources": sources}

    def _plan_view(self, user: Any, entitlement: Any, now: float) -> dict:
        """The plan's own policy and what has been spent against it (web_chat.credentials_api)."""
        limits = entitlement.limits
        view: Dict[str, Any] = {
            "tier": entitlement.tier,
            "label": entitlement.label or entitlement.tier,
            "limits": limits.model_dump() if hasattr(limits, "model_dump") else dict(limits or {}),
        }
        if self._spent_micro is not None:
            view["spent"] = {
                "day_usd": self._spent_micro(user.id, utc_day_start(now)) / 1_000_000,
                "month_usd": self._spent_micro(user.id, utc_month_start(now)) / 1_000_000,
            }
        return view

    def _used_providers(self, config: Any, entitlement: Any) -> List[str]:
        used: List[str] = []
        for key, model in getattr(config, "models", {}).items():
            try:
                permitted = entitlement.permits_model(key, getattr(model, "name", key))
            except Exception:  # noqa: BLE001 - a broken plan hides nothing else
                permitted = False
            if permitted and getattr(model, "provider", None) not in used:
                used.append(model.provider)
        return used

    def _stored(self, user: Any, kind: str) -> Optional[str]:
        """The status of the user's stored credential of *kind*, or None when there is none."""
        if self._vault is None or not getattr(self._vault, "enabled", False):
            return None
        return self._vault.status(user.id, kind)

    def _claude_source(self, now: float) -> dict:
        reading = self._quota.latest(CLAUDE_CODE)
        mode = _mode(reading, now)
        notes = []
        if reading is None:
            notes.append(
                "No report yet. Claude Code reports its windows with every answer; "
                "“Read now” asks the CLI for one tiny answer to hear them."
            )
        else:
            notes.append("Reported by Claude Code with its answers.")
            if reading.status == "rejected":
                notes.append("The plan refused its last request: a limit is reached.")
            if reading.overage_allowed:
                notes.append("Extra usage is on for this account: going past the plan would be billed.")
        return {
            "id": "claude-code",
            "kind": "subscription",
            "label": "Claude Code subscription",
            "detail": "The Claude Code login of this machine",
            "status": mode,
            "connected": True,
            "observed_at": reading.observed_at if reading is not None else None,
            "windows": _window_view(reading) if reading is not None else [],
            "limits": None,
            "note": " ".join(notes),
            "error": self._errors.get(("claude-code",)),
            "action": {
                "source": "claude-code",
                "kind": "probe",
                "label": "Read now",
                "costs_quota": True,
                "confirm": (
                    "Reading the Claude Code plan sends one very small prompt through the CLI and "
                    "spends a little of the plan's quota. Continue?"
                ),
            },
        }

    def _chatgpt_source(self, user: Any, now: float) -> dict:
        connected = self._vault is not None and getattr(self._vault, "enabled", False) and self._vault.status(
            user.id, CHATGPT
        ) == "ok"
        reading = self._chatgpt.get(user.id)
        if self._vault is None or not getattr(self._vault, "enabled", False):
            status, note = "unavailable", "This server cannot store the ChatGPT sign-in (GRID_SECRETS_KEY is not set)."
        elif not connected:
            status, note = "unavailable", "Not connected: sign in with ChatGPT on the Plan page."
        elif reading is None:
            status, note = "unknown", "Not read yet: refresh to ask OpenAI for the plan's windows."
        else:
            status = _mode(reading, now)
            note = "Read from OpenAI's plan usage endpoint. Reading it spends no tokens."
            if reading.status == "rejected":
                note += " The plan says its limit is reached."
        # A sign-in that is gone leaves no numbers behind: only a connected plan is read.
        readable = reading if connected else None
        return {
            "id": "chatgpt",
            "kind": "subscription",
            "label": "ChatGPT plan",
            "detail": "The ChatGPT plan of this account",
            "status": status,
            "connected": connected,
            "observed_at": readable.observed_at if readable is not None else None,
            "windows": _window_view(readable) if readable is not None else [],
            "limits": None,
            "note": note,
            "error": self._errors.get((user.id, "chatgpt")),
            "action": {
                "source": "chatgpt",
                "kind": "fetch",
                "label": "Refresh",
                "costs_quota": False,
            }
            if connected
            else None,
        }

    def _key_source(self, user: Any, kind: str, now: float) -> dict:
        label = KEY_LABELS.get(kind, kind)
        stored = None
        if self._vault is not None and getattr(self._vault, "enabled", False):
            stored = next((row for row in self._vault.listing(user.id) if row["kind"] == kind), None)
        connected = stored is not None and stored.get("status") == "ok"
        # A key that is gone leaves no limits behind either.
        limits = self._keys.get((user.id, kind)) if connected else None
        action = None
        if not connected:
            status = "unavailable"
            note = "Not connected: add a key on the Plan page."
        elif kind == "openrouter":
            status = _mode_kind(limits, now)
            note = (
                "Read from OpenRouter's key endpoint."
                if limits is not None
                else "Not read yet: refresh to ask OpenRouter about this key."
            )
            action = {"source": f"key:{kind}", "kind": "fetch", "label": "Refresh", "costs_quota": False}
        else:
            status = "unavailable"
            if kind == "openai":
                note = (
                    "OpenAI does not report a key's remaining quota to this server. "
                    "Spend on this key is counted in the tables below."
                )
            else:
                note = f"{label} does not report a key's remaining quota to this server."
        return {
            "id": f"key:{kind}",
            "kind": "key",
            "label": label,
            "detail": f"Your own {label} key",
            "status": status,
            "connected": connected,
            "observed_at": limits.get("observed_at") if limits else None,
            "windows": [],
            "limits": limits,
            "hint": stored.get("hint") if stored else None,
            "note": note,
            "error": self._errors.get((user.id, f"key:{kind}")),
            "action": action,
        }

    def _server_source(self, name: str, config: Any, now: float) -> dict:
        provider = getattr(config, "providers", {}).get(name)
        return {
            "id": f"server:{name}",
            "kind": "server",
            "label": getattr(provider, "name", name),
            "detail": "The server's own key",
            "status": "unavailable",
            "connected": True,
            "observed_at": None,
            "windows": [],
            "limits": None,
            "note": "Paid by the server's key. Its remaining quota belongs to the operator and is not read here.",
            "error": None,
            "action": None,
        }

    # -- active reading --------------------------------------------------------------
    def _key(self, user_id: str, source: str) -> tuple:
        """Where a source's cooldown lives: the machine's own login is one for everybody."""
        return ("*", source) if source == "claude-code" else (user_id, source)

    def _free_sources(self, state: Mapping[str, Any]) -> List[str]:
        """The sources a view may read by itself: they cost nothing."""
        return [
            source["action"]["source"]
            for source in state.get("sources", ())
            if source.get("action") and source["action"].get("kind") == "fetch" and not source["action"].get("costs_quota")
        ]

    async def snapshot_live(self, user: Any) -> dict:
        """The whole picture, reading the free sources that are older than their TTL first."""
        await self._read_due(user.id, self._free_sources(self.snapshot(user)))
        return self.snapshot(user)

    async def refresh_free(self, user: Any) -> None:
        """A page or a lazy API asks for current numbers: read the free, visible sources when due."""
        await self._read_due(user.id, self._free_sources(self.snapshot(user)))

    async def ensure_reading(self, user_id: str, source: str) -> None:
        """A lazy step's reading: the caller vouches that *source* is the user's own."""
        await self._read_due(user_id, [source])

    async def _read_due(self, user_id: str, sources: List[str]) -> None:
        now = self.clock()
        due = [
            source
            for source in dict.fromkeys(sources)
            if now - self._fetched.get(self._key(user_id, source), 0.0) >= AUTOFETCH
        ]
        if due:
            # One slow provider must not hold the others back or fail the page.
            await asyncio.gather(
                *(self._read_serial(user_id, source, AUTOFETCH) for source in due), return_exceptions=True
            )

    async def _read_serial(self, user_id: str, source: str, ttl: float, *, confirm: bool = False) -> None:
        """Read *source* unless it was read within *ttl*, one reader at a time."""
        key = self._key(user_id, source)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if self.clock() - self._fetched.get(key, 0.0) < ttl:
                return
            await self._read(user_id, source, confirm=confirm)
            # Even a provider refusal is not retried at once: it said its word. A
            # request that was never made (no confirmation) leaves the cooldown alone.
            self._fetched[key] = self.clock()

    async def _read(self, user_id: str, source: str, *, confirm: bool = False) -> None:
        if source == "claude-code":
            if not confirm:
                raise SnapshotError(
                    "Reading the Claude Code plan sends one small prompt and spends a little quota: confirm it."
                )
            try:
                await self._claude_probe()()
                self._errors.pop(("claude-code",), None)
            except RefreshUnavailable as exc:
                self._errors[("claude-code",)] = str(exc)
        elif source == "chatgpt":
            try:
                self._chatgpt[user_id] = await fetch_chatgpt_usage(
                    self._vault, user_id, transport=self._chatgpt_transport, now=self.clock()
                )
                self._errors.pop((user_id, "chatgpt"), None)
            except QuotaUnavailable as exc:
                self._errors[(user_id, "chatgpt")] = str(exc)
        elif source == "key:openrouter":
            try:
                self._keys[(user_id, "openrouter")] = await fetch_openrouter_key(
                    self._vault, user_id, transport=self._key_transport, now=self.clock()
                )
                self._errors.pop((user_id, "key:openrouter"), None)
            except RefreshUnavailable as exc:
                self._errors[(user_id, "key:openrouter")] = str(exc)
        else:
            raise SnapshotError(f"No source named {source!r} can be refreshed.")

    # -- explicit refresh ------------------------------------------------------------
    async def refresh(self, user: Any, source: str, *, confirm: bool = False) -> dict:
        """Read one source the user may see now, and return the whole picture again."""
        state = self.snapshot(user)
        # A listed source stays refreshable even when it is not connected: the read
        # then says in words why (no key, no sign-in) instead of pretending it is
        # not there. Sources absent from this user's picture are refused outright.
        readable = {"claude-code", "chatgpt", "key:openrouter"}
        allowed = {item["id"] for item in state["sources"]} & readable
        if source not in allowed:
            raise SnapshotError(f"No source named {source!r} can be refreshed for this account.")
        await self._read_serial(user.id, source, COOLDOWN, confirm=confirm)
        return self.snapshot(user)

    def _claude_probe(self) -> Callable[[], Awaitable[QuotaReading]]:
        if self._probe is None:
            binary = None
            for provider in getattr(self._config(), "providers", {}).values():
                if getattr(provider, "auth", "api_key") == "claude":
                    binary = getattr(provider, "claude_binary", None)
                    break
            self._probe = ClaudeQuotaProbe(binary=binary, quota=self._quota, clock=self.clock)
        return self._probe


def _mode_kind(limits: Optional[Mapping[str, Any]], now: float) -> str:
    if limits is None:
        return "unknown"
    observed = limits.get("observed_at")
    if not isinstance(observed, (int, float)):
        return "unknown"
    return "live" if now - observed <= LIVE else "stale"
