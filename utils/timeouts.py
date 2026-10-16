"""Named time limits of Grid, in seconds.

The one place to read or change a timeout. Names say what is bounded; groups
mirror the subsystem that owns the limit. ``*_DEFAULT`` is what an operator gets
without configuring anything, ``*_MIN``/``*_MAX`` bound what configuration may
ask for. Only the standard library is imported here, so every layer (schemas,
core, web chat, entry points) can depend on this module.

``grid_control`` keeps its own ``limits`` module: it ships as a separate service
and imports nothing from Grid.
"""

from typing import Final

# -- Providers and agents ------------------------------------------------------
PROVIDER_TIMEOUT_DEFAULT: Final = 30
PROVIDER_TIMEOUT_MAX: Final = 300
DECISIONS_REQUEST_TIMEOUT_MAX: Final = 300

AGENT_TIMEOUT_DEFAULT: Final = 300
AGENT_TIMEOUT_MAX: Final = 1800

# -- Action policy gate ----------------------------------------------------------
POLICY_VALIDATOR_TIMEOUT_DEFAULT: Final = 5
POLICY_VALIDATOR_TIMEOUT_MAX: Final = 60
POLICY_CIRCUIT_COOLDOWN_DEFAULT: Final = 15
POLICY_CIRCUIT_COOLDOWN_MAX: Final = 300
POLICY_REVIEW_TTL_DEFAULT: Final = 300
POLICY_REVIEW_TTL_MIN: Final = 10
POLICY_REVIEW_TTL_MAX: Final = 86400

# -- Background workers (MCP server) ------------------------------------------------
WORKER_TIMEOUT_DEFAULT: Final = 300
WORKER_TIMEOUT_MAX: Final = 900
WORKER_WAIT_DEFAULT: Final = 180

# -- Web chat: outgoing HTTP ------------------------------------------------------------
CREDENTIAL_CHECK_HTTP: Final = 10
CHATGPT_HTTP: Final = 20
CHATGPT_PROBE_HTTP: Final = 60
CHATGPT_CALLBACK_POLL: Final = 1
PRICES_FETCH_HTTP: Final = 60
REVIEW_CONTROLLER_HTTP: Final = 30

# -- Web chat: model questions answered inside a user's turn ----------------------------
# The HTTP request is bounded tighter than the deadline around the whole call.
VOICE_TURN_REQUEST: Final = 2.5
VOICE_TURN_DEADLINE: Final = 3
DELIVERY_REQUEST: Final = 4
DELIVERY_DEADLINE: Final = 5

# -- Web chat: subprocesses ------------------------------------------------------------------
FFMPEG_CONVERT: Final = 45
GIT_QUERY: Final = 10
GIT_APPLY_CHECK: Final = 30
GIT_ARCHIVE: Final = 120

# -- Restart of the web chat ---------------------------------------------------------------
RESTART_IDLE_POLL: Final = 0.1
# Lets the HTTP 202/status response leave before the listener closes.
RESTART_RESPONSE_GRACE: Final = 0.5

# -- Evaluation harness -----------------------------------------------------------------------
EVAL_CONTROLLER_PROBE: Final = 2
EVAL_CONTROLLER_START: Final = 60
EVAL_CONTROLLER_START_POLL: Final = 1
EVAL_DRAIN_POLL: Final = 10
EVAL_CONTROLLER_STOP: Final = 30
EVAL_ADMIN_TIMEOUT_DEFAULT: Final = 3600
EVAL_DRAIN_DEFAULT: Final = 1800
