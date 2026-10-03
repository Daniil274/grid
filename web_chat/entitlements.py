"""Plans: what a user's tier lets them run, and with whose key.

A *tier* is a named plan in the server's config (``tiers``). It bundles the
three things that used to be global: which key pays for the user's calls (the
operator's pool, or the user's own), which models they may use, and how much
they may run (``limits``, in dollars as well as turns and tokens). Roles stay
about who administers; a tier is about what a user may consume.

The user's tier is stored with the account and read at every turn, so an
admin's change applies at once. A server whose config has no ``tiers`` has one
implicit plan for everybody, which is how servers behaved before tiers.
"""

from __future__ import annotations

import fnmatch
import logging
import os
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Protocol

from core.credentials import PROVIDER_PRESETS, Credential, CredentialError, EnvCredentials, preset_for
from schemas.schemas import GridConfig, UserLimitsPolicy

logger = logging.getLogger(__name__)

ADMIN_TIER = "admin"
#: The name of the one plan of a server whose config defines no tiers.
IMPLICIT_TIER = "default"
#: The pool of the providers' own keys (the ones in the operator's environment).
DEFAULT_POOL = "default"
#: Every credential a user may bring: a key per provider preset, and a ChatGPT login.
ALL_OWN_CREDENTIALS = frozenset({*PROVIDER_PRESETS, "chatgpt"})
CHATGPT = "chatgpt"


@dataclass(frozen=True)
class Entitlement:
    """What one user may use now."""

    tier: str
    label: str
    pool: Optional[str]
    own_credentials: frozenset[str]
    models: tuple[str, ...]
    limits: UserLimitsPolicy

    def permits_model(self, key: str, name: str) -> bool:
        return any(fnmatch.fnmatchcase(candidate, pattern) for pattern in self.models for candidate in (key, name))

    def signature(self) -> str:
        """Changes when the models the user may use change."""
        return f"{self.tier}|{'|'.join(self.models)}"


class Plans:
    """The entitlements of a server's config, read fresh so an edited config applies at once."""

    def __init__(self, config: Callable[[], GridConfig]) -> None:
        self._config = config

    @property
    def configured(self) -> bool:
        return bool(self._config().tiers)

    def tier_names(self) -> list[str]:
        """Tiers an admin may assign: the configured ones except the admin's own."""
        return [name for name in self._config().tiers if name != ADMIN_TIER]

    def pool(self, name: str) -> Mapping[str, str]:
        return self._config().pools.get(name, {})

    def resolve(self, *, admin: bool, tier: str = "") -> Entitlement:
        config = self._config()
        if not config.tiers:
            # The one local user (admin) may bring any credential; other users of a
            # server that defines no tiers keep the operator's keys only.
            own = ALL_OWN_CREDENTIALS if admin else frozenset()
            return Entitlement(IMPLICIT_TIER, "", DEFAULT_POOL, own, ("*",), config.user_limits)
        if admin:
            name = ADMIN_TIER
            policy = config.tiers.get(ADMIN_TIER)
            if policy is None:
                # An unconfigured admin plan: the operator's keys, every model, the root limits.
                return Entitlement(ADMIN_TIER, "Admin", DEFAULT_POOL, ALL_OWN_CREDENTIALS, ("*",), config.user_limits)
        else:
            name = tier or config.default_tier
            policy = config.tiers.get(name)
            if policy is None:
                logger.warning("User is on tier %r, which the config does not define; using %r", name, config.default_tier)
                name = config.default_tier
                policy = config.tiers[name]
        return Entitlement(
            name,
            policy.label or name,
            policy.pool,
            frozenset(policy.own_credentials),
            tuple(policy.models),
            policy.limits if policy.limits is not None else config.user_limits,
        )


class OwnCredentials(Protocol):
    """The keys and tokens users stored for themselves (web_chat.vault)."""

    def has(self, user_id: str, kind: str) -> bool: ...

    async def credential(self, user_id: str, kind: str) -> Optional[Credential]: ...


class UserCredentials:
    """The credentials of one user: their own where their plan lets them, else the plan's pool.

    The user's own credential wins: they chose to spend it. Neither falls through
    to the other when it fails - a user whose own key was revoked is told so,
    never silently moved onto the operator's bill.
    """

    def __init__(
        self,
        user_id: str,
        entitlement: Callable[[], Entitlement],
        plans: Plans,
        own: Optional[OwnCredentials] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._user_id = user_id
        self._entitlement = entitlement
        self._plans = plans
        self._own = own
        self._environ = environ if environ is not None else os.environ
        self._env = EnvCredentials(self._environ)

    # -- own ---------------------------------------------------------------------
    def _own_kind(self, provider: object, entitlement: Entitlement) -> Optional[str]:
        """The kind of own credential that would serve *provider*, if the plan allows one."""
        if self._own is None:
            return None
        if getattr(provider, "auth", "api_key") == "chatgpt":
            kind = CHATGPT
        else:
            kind = preset_for(getattr(provider, "base_url", ""))
        if kind is not None and kind in entitlement.own_credentials and self._own.has(self._user_id, kind):
            return kind
        return None

    # -- pool --------------------------------------------------------------------
    def _pool_credential(self, provider: object, entitlement: Entitlement) -> Optional[Credential]:
        pool = entitlement.pool
        if pool is None or getattr(provider, "auth", "api_key") == "chatgpt":
            return None  # a plan login is the user's alone: no pool serves it
        if pool == DEFAULT_POOL:
            secret = self._env._secret(provider)
        else:
            variable = self._plans.pool(pool).get(getattr(provider, "api_key_env", None) or "")
            secret = (self._environ.get(variable) or None) if variable else None
        return Credential(secret, f"pool:{pool}") if secret else None

    # -- the CredentialSource protocol -------------------------------------------
    def available(self, provider: object) -> bool:
        entitlement = self._entitlement()
        return self._own_kind(provider, entitlement) is not None or self._pool_credential(provider, entitlement) is not None

    async def credential(self, provider: object) -> Credential:
        entitlement = self._entitlement()
        kind = self._own_kind(provider, entitlement)
        if kind is not None:
            found = await self._own.credential(self._user_id, kind)  # type: ignore[union-attr]
            if found is None:
                raise CredentialError(
                    f"Your {kind} credential cannot be used right now. Reconnect it in the settings.",
                    details={"provider": getattr(provider, "name", ""), "kind": kind},
                )
            return found
        pooled = self._pool_credential(provider, entitlement)
        if pooled is None and getattr(provider, "auth", "api_key") == "chatgpt":
            raise CredentialError(
                "This model runs on your ChatGPT plan. Sign in with ChatGPT in the settings to use it.",
                details={"provider": getattr(provider, "name", ""), "tier": entitlement.tier},
            )
        if pooled is None:
            raise CredentialError(
                f"No credential for provider '{getattr(provider, 'name', '')}' on your plan "
                f"({entitlement.label or entitlement.tier}). Add your own key in the settings, or ask an admin.",
                details={"provider": getattr(provider, "name", ""), "tier": entitlement.tier},
            )
        return pooled
