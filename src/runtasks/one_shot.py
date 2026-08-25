from __future__ import annotations

import asyncio
from typing import Protocol


DEFAULT_ONE_SHOT_SERVICE = "runtasks-scheduler.service"


class OneShotRunTriggerError(RuntimeError):
    """Raised when separate approval processing cannot be requested safely."""


class OneShotRunTrigger(Protocol):
    async def request(self) -> None: ...


class SystemdOneShotRunTrigger:
    """Queue a fresh user-level RunTasks one-shot runner invocation."""

    def __init__(
        self,
        *,
        service_name: str = DEFAULT_ONE_SHOT_SERVICE,
        timeout_seconds: int = 600,
    ) -> None:
        self._service_name = service_name
        self._timeout_seconds = timeout_seconds

    async def request(self) -> None:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                while await self._service_is_active():
                    await asyncio.sleep(0.25)
                return_code = await self._run_systemctl(
                    "start",
                    "--no-block",
                    self._service_name,
                )
        except Exception:
            raise OneShotRunTriggerError(
                "separate approval processing could not be requested"
            ) from None
        if return_code != 0:
            raise OneShotRunTriggerError(
                "separate approval processing could not be requested"
            )

    async def _service_is_active(self) -> bool:
        return_code = await self._run_systemctl(
            "is-active",
            "--quiet",
            self._service_name,
        )
        if return_code == 0:
            return True
        if return_code in {3, 4}:
            return False
        raise OneShotRunTriggerError(
            "separate approval processing state could not be inspected"
        )

    async def _run_systemctl(self, *arguments: str) -> int:
        process = await asyncio.create_subprocess_exec(
            "systemctl",
            "--user",
            *arguments,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await process.wait()
