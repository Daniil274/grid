"""Where a conversation's work happens: here (the cloud) or on a user's device.

The user picks a target in the chat - "cloud", or one of the devices their
phone, PC or browser machine show up as - and every tool of the turn follows
it: the base tools run on the device (core.devices.execution), the action
policy reads paths by that device's rules (core.action_policy), and the
prompt says where the work is (core.managers.instructions_builder).

The choice is kept in the conversation's metadata, like the policy filter, so
it survives a restart and a switch of system; the factory holds the live value
for the runs of this process. Tools read it through the factory, never from a
model's argument: no tool can move a chat onto another machine.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("grid.agent_factory")

#: Where a conversation works: "cloud" or "device:<device id>" (ContextManager metadata).
EXECUTION_TARGET_KEY = "execution_target"
#: The folder on the device the conversation works in; "" means the device's own.
EXECUTION_WORKDIR_KEY = "execution_workdir"

CLOUD = "cloud"


def resolve_target(user_id: str, target: Any, workdir: Any = "") -> Optional[Tuple[str, str]]:
    """(target, workdir) of a choice the user made, or None when it names no device.

    A device must be one this user has connected now: a target nobody can serve
    would leave their writes nowhere to go. The device's own working folder is
    used when the choice names none.
    """
    from core.devices import execution
    from core.devices.hub import DeviceError, hub

    value = target.strip() if isinstance(target, str) else ""
    folder = workdir.strip() if isinstance(workdir, str) else ""
    if len(folder) > 1024:
        return None
    if not value or value.casefold() == CLOUD:
        # Back to the server: the folder of a device means nothing there.
        return CLOUD, ""
    device_id = execution.device_id_of(value)
    if device_id is None:
        return None
    try:
        device = hub().get(user_id, device_id)
    except DeviceError:
        return None
    if not folder:
        folder = str(getattr(device, "default_workdir", "") or "")
    return execution.target_for(device_id), folder


class ExecutionTargets:
    """The factory's side of the execution target (mixed into AgentFactory).

    Relies on ``self.context_manager`` and ``self.get_active_context_id()``.
    """

    #: Where this factory's tools act unless the conversation says otherwise.
    execution_target: str = CLOUD
    #: The folder on the device; "" is the device's own working folder.
    execution_workdir: str = ""
    #: The user whose devices the factory may use (the web chat's space sets it).
    execution_user: Optional[str] = None

    # -- reading -------------------------------------------------------------
    def execution_target_of(self, context_id: Optional[str] = None) -> Tuple[str, str]:
        """(target, workdir) conversation *context_id* works with.

        A conversation's choice is its own and nobody else's: one that made none
        works on the server, whatever another chat chose. The factory's own
        value is what a run without a conversation uses. A device this user no
        longer has connected is still reported - the chat keeps pointing at it -
        and ``core.devices.execution`` says so when a tool runs.
        """
        target, workdir = self.execution_target, self.execution_workdir
        context_id = context_id or self._execution_context_id()
        store = getattr(self, "context_manager", None)
        if context_id and store is not None:
            try:
                metadata = store.get_context_metadata(context_id) or {}
            except Exception:  # noqa: BLE001 - metadata is a convenience, never a requirement
                logger.debug("No metadata for %s", context_id, exc_info=True)
                metadata = {}
            stored = metadata.get(EXECUTION_TARGET_KEY)
            if isinstance(stored, str) and self._target_is_known(stored):
                target = stored
                stored_workdir = metadata.get(EXECUTION_WORKDIR_KEY)
                workdir = stored_workdir if isinstance(stored_workdir, str) else ""
            else:
                target, workdir = CLOUD, ""
        return (target if isinstance(target, str) else CLOUD), (workdir if isinstance(workdir, str) else "")

    def target_device_id(self, context_id: Optional[str] = None) -> Optional[str]:
        """The id of the device this conversation works on; None for the cloud."""
        from core.devices.execution import device_id_of

        return device_id_of(self.execution_target_of(context_id)[0])

    def execution_device(self, context_id: Optional[str] = None) -> Optional[dict]:
        """What the prompt needs about the device, or None when the work is here."""
        from core.devices import execution
        from core.devices.hub import DeviceError

        device_id = self.target_device_id(context_id)
        if device_id is None:
            return None
        try:
            target = execution.chosen(self)
        except DeviceError:
            return None
        if target is None:
            return None
        return {
            "device": target.device_id,
            "name": target.name,
            "platform": target.platform,
            "platform_label": execution.platform_label(target.platform),
            "workdir": target.workdir,
        }

    @staticmethod
    def _target_is_known(target: str) -> bool:
        from core.devices.execution import CLOUD as CLOUD_TARGET
        from core.devices.execution import device_id_of

        return target.casefold() == CLOUD_TARGET or device_id_of(target) is not None

    def _execution_context_id(self) -> Optional[str]:
        reader = getattr(self, "get_active_context_id", None)
        if callable(reader):
            try:
                value = reader()
            except Exception:  # noqa: BLE001 - no conversation: the factory's own value
                return None
            return value if isinstance(value, str) and value else None
        return None

    # -- writing -------------------------------------------------------------
    def set_execution_target(
        self,
        target: Any,
        workdir: Any = "",
        *,
        context_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> bool:
        """Point this factory (and *context_id*, when named) at a target.

        ``"cloud"`` sends the work back to the server; ``"device:<id>"`` needs a
        device the user has connected now. False - and nothing changed - for
        anything else. The user's choice only: no agent tool reaches this.
        """
        user = user_id or self.execution_user or "local"
        resolved = resolve_target(str(user), target, workdir)
        if resolved is None:
            return False
        value, folder = resolved
        self.execution_target, self.execution_workdir = value, folder
        if user_id:
            self.execution_user = str(user_id)
        context_id = context_id or self._execution_context_id()
        store = getattr(self, "context_manager", None)
        if context_id and store is not None:
            try:
                store.update_context_metadata(
                    context_id,
                    {EXECUTION_TARGET_KEY: value, EXECUTION_WORKDIR_KEY: folder},
                )
            except Exception:  # noqa: BLE001 - a turn in flight keeps the live value
                logger.warning("Could not keep the execution target of %s", context_id, exc_info=True)
        return True

    def execution_value(self) -> Dict[str, Any]:
        """The target as the chat reports it to its client."""
        target, workdir = self.execution_target_of()
        return {"target": target, "workdir": workdir}
