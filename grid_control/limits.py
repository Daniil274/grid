"""Named time limits of the grid_control service, in seconds.

Standard library only: this package is deployed apart from Grid and must not
import it. ``*_DEFAULT`` is used when a policy says nothing, ``*_MIN``/``*_MAX``
bound what a policy may ask for.
"""

from typing import Final

# -- Policy -----------------------------------------------------------------------------
TIMEOUT_MIN: Final = 1
# Verifier startup and HTTP checks, before the scenarios begin.
POLICY_TIMEOUT_DEFAULT: Final = 180
BUILD_TIMEOUT_DEFAULT: Final = 300
POLICY_TIMEOUT_MAX: Final = 3600

# One scenario: a chat turn answered by the candidate.
SCENARIO_TIMEOUT_DEFAULT: Final = 180
SCENARIO_TIMEOUT_MAX: Final = 1800

# -- Subprocesses and storage ---------------------------------------------------------------
DOCKER_COMMAND_TIMEOUT: Final = 30
GIT_COMMAND_TIMEOUT: Final = 60
GIT_BUNDLE_TIMEOUT: Final = 300
STORE_BUSY_TIMEOUT: Final = 10
