from __future__ import annotations

import asyncio
from typing import Protocol


DEFAULT_ONE_SHOT_SERVICE = "runtasks-scheduler.service"


class OneShotRunTriggerError(RuntimeError):
    """Raised when separate approval processing cannot be requested safely."""


class OneShotRunTrigger(Protocol):
    async def request(self) -> None: ...


class SystemdOneShotRunTrigger:
    """Wake the separately installed user-level RunTasks one-shot runner."""

    def __init__(
        self,
        *,
        service_name: str = DEFAULT_ONE_SHOT_SERVICE,
        timeout_seconds: int = 15,
    ) -> None:
        self._service_name = service_name
        self._timeout_seconds = timeout_seconds

    async def request(self) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                "systemctl",
                "--user",
                "start",
                self._service_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return_code = await asyncio.wait_for(
                process.wait(),
                timeout=self._timeout_seconds,
            )
        except Exception:
            raise OneShotRunTriggerError(
                "separate approval processing could not be requested"
            ) from None
        if return_code != 0:
            raise OneShotRunTriggerError(
                "separate approval processing could not be requested"
            )
