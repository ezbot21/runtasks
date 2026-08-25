from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Mapping, cast

from runtasks.pi_mcp_execution import (
    PiMcpExecutionAdapterError,
    PiMcpExecutionAdapters,
    PiMcpExecutionError,
    is_exact_stable_version,
)
from runtasks.pi_mcp_release_adapters import (
    PROCESS_TIMEOUT_SECONDS,
    ProcessResult,
    ProcessRunner,
    SubprocessRunner,
    resolve_pi_agent_dir,
)
from runtasks.redaction import Redactor
from runtasks.telegram_config import load_telegram_settings
from runtasks.telegram_transport import (
    PythonTelegramBotClient,
    build_telegram_notification_client,
)


INSTALL_TIMEOUT_SECONDS = 120.0
MCP_VALIDATION_TIMEOUT_SECONDS = 120.0
_MCP_VALIDATION_PROMPT = (
    "Call the mcp tool with an empty object. If successful, reply exactly "
    "MCP_ADAPTER_OK."
)


class PiPackageAdapter:
    def __init__(
        self,
        *,
        agent_dir: Path,
        process_runner: ProcessRunner,
        pi_command: tuple[str, ...] = ("pi",),
    ) -> None:
        self._package_path = (
            agent_dir
            / "npm"
            / "node_modules"
            / "pi-mcp-adapter"
            / "package.json"
        )
        self._process_runner = process_runner
        self._pi_command = pi_command

    def installed_version(self) -> str:
        try:
            value: object = json.loads(
                self._package_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PiMcpExecutionAdapterError(
                "installed adapter package metadata is unavailable"
            ) from error
        if not isinstance(value, dict):
            raise PiMcpExecutionAdapterError(
                "installed adapter package metadata is malformed"
            )
        metadata = cast(dict[object, object], value)
        if metadata.get("name") != "pi-mcp-adapter":
            raise PiMcpExecutionAdapterError(
                "installed adapter package identity is invalid"
            )
        version = metadata.get("version")
        if not isinstance(version, str) or not is_exact_stable_version(version):
            raise PiMcpExecutionAdapterError(
                "installed adapter version is invalid"
            )
        return version

    def install_exact(self, version: str) -> None:
        if not is_exact_stable_version(version):
            raise PiMcpExecutionAdapterError(
                "approved target version is not an exact stable version"
            )
        result = _run_process(
            self._process_runner,
            (*self._pi_command, "install", f"npm:pi-mcp-adapter@{version}"),
            timeout_seconds=INSTALL_TIMEOUT_SECONDS,
            failure="exact adapter installation failed",
        )
        if result.returncode != 0:
            raise PiMcpExecutionAdapterError(
                "exact adapter installation failed"
            )


class SystemdServiceAdapter:
    def __init__(self, *, process_runner: ProcessRunner) -> None:
        self._process_runner = process_runner

    def restart(self, service_name: str) -> None:
        if service_name != "pi-web.service":
            raise PiMcpExecutionAdapterError("approved service name is invalid")
        result = _run_process(
            self._process_runner,
            ("systemctl", "--user", "restart", service_name),
            timeout_seconds=PROCESS_TIMEOUT_SECONDS,
            failure="Pi Web restart failed",
        )
        if result.returncode != 0:
            raise PiMcpExecutionAdapterError("Pi Web restart failed")


class SystemdHealthAdapter:
    def __init__(self, *, process_runner: ProcessRunner) -> None:
        self._process_runner = process_runner

    def check(self, service_name: str) -> str:
        if service_name != "pi-web.service":
            raise PiMcpExecutionAdapterError("approved service name is invalid")
        result = _run_process(
            self._process_runner,
            ("systemctl", "--user", "is-active", service_name),
            timeout_seconds=PROCESS_TIMEOUT_SECONDS,
            failure="Pi Web health check failed",
        )
        if (
            result.returncode != 0
            or result.stderr != ""
            or not _is_exact_line(result.stdout, "active")
        ):
            raise PiMcpExecutionAdapterError(
                "Pi Web health check was not unambiguously healthy"
            )
        return "healthy"


class FreshPiValidationAdapter:
    def __init__(
        self,
        *,
        process_runner: ProcessRunner,
        pi_command: tuple[str, ...] = ("pi",),
        cwd: Path | None = None,
    ) -> None:
        self._process_runner = process_runner
        self._pi_command = pi_command
        self._cwd = cwd

    def validate_mcp(self, expected_result: str) -> str:
        if expected_result != "MCP_ADAPTER_OK":
            raise PiMcpExecutionAdapterError(
                "approved MCP validation result is invalid"
            )
        result = _run_process(
            self._process_runner,
            (
                *self._pi_command,
                "--no-session",
                "--tools",
                "mcp",
                "-p",
                _MCP_VALIDATION_PROMPT,
            ),
            timeout_seconds=MCP_VALIDATION_TIMEOUT_SECONDS,
            cwd=self._cwd,
            failure="fresh Pi MCP validation failed",
        )
        if (
            result.returncode != 0
            or result.stderr != ""
            or not _is_exact_line(result.stdout, expected_result)
        ):
            raise PiMcpExecutionAdapterError(
                "fresh Pi MCP validation did not return exact MCP_ADAPTER_OK"
            )
        return expected_result


class TelegramExecutionNotificationAdapter:
    def __init__(
        self,
        settings: Mapping[str, str],
    ) -> None:
        self._settings = settings

    def send(self, text: str) -> None:
        telegram_settings = load_telegram_settings(
            self._settings,
            require_destination=True,
        )
        if telegram_settings.destination is None:
            raise PiMcpExecutionAdapterError(
                "Telegram notification destination is missing"
            )
        raw_client = PythonTelegramBotClient(telegram_settings.bot_token)
        try:
            asyncio.run(raw_client.verify_destination(telegram_settings))
            client = build_telegram_notification_client(
                raw_client,
                telegram_settings.destination,
                sensitive_values=self._settings.values(),
            )
            asyncio.run(client.send(text=text))
        except Exception:
            raise PiMcpExecutionAdapterError(
                "execution outcome notification delivery failed"
            ) from None


class _FixtureRecorder:
    def __init__(self, path: Path | None, redactor: Redactor) -> None:
        self._path = path
        self._redactor = redactor

    def record(self, operation: str, **values: object) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._redactor.value({"operation": operation, **values})
        with self._path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, sort_keys=True) + "\n")


class FixturePackageAdapter:
    def __init__(
        self,
        versions: tuple[str, ...],
        install_steps: tuple[dict[object, object], ...],
        recorder: _FixtureRecorder,
        shared_version_path: Path | None = None,
    ) -> None:
        self._versions = list(versions)
        self._install_steps = list(install_steps)
        self._shared_version_path = shared_version_path
        self._recorder = recorder

    def installed_version(self) -> str:
        self._recorder.record("package.installed-version")
        if self._shared_version_path is not None:
            try:
                return self._shared_version_path.read_text(encoding="utf-8").strip()
            except OSError as error:
                raise PiMcpExecutionAdapterError(
                    "fixture installed version is unavailable"
                ) from error
        if not self._versions:
            raise PiMcpExecutionAdapterError(
                "fixture installed version is unavailable"
            )
        return self._versions.pop(0)

    def install_exact(self, version: str) -> None:
        self._recorder.record("package.install-exact", version=version)
        step = _next_fixture_step(self._install_steps, "install")
        if _fixture_text(step, "status") != "success":
            raise PiMcpExecutionAdapterError(
                _fixture_error(step, "fixture exact installation failed")
            )
        if self._shared_version_path is not None:
            self._shared_version_path.write_text(version, encoding="utf-8")


class FixtureServiceAdapter:
    def __init__(
        self,
        steps: tuple[dict[object, object], ...],
        recorder: _FixtureRecorder,
    ) -> None:
        self._steps = list(steps)
        self._recorder = recorder

    def restart(self, service_name: str) -> None:
        self._recorder.record("service.restart", service=service_name)
        step = _next_fixture_step(self._steps, "restart")
        if _fixture_text(step, "status") != "success":
            raise PiMcpExecutionAdapterError(
                _fixture_error(step, "fixture service restart failed")
            )


class FixtureHealthAdapter:
    def __init__(
        self,
        steps: tuple[dict[object, object], ...],
        recorder: _FixtureRecorder,
    ) -> None:
        self._steps = list(steps)
        self._recorder = recorder

    def check(self, service_name: str) -> str:
        self._recorder.record("health.check", service=service_name)
        step = _next_fixture_step(self._steps, "health")
        result = _fixture_text(step, "result")
        if _fixture_text(step, "status") != "success" or result != "healthy":
            raise PiMcpExecutionAdapterError(
                _fixture_error(step, "fixture health check failed")
            )
        return result


class FixturePiValidationAdapter:
    def __init__(
        self,
        steps: tuple[dict[object, object], ...],
        recorder: _FixtureRecorder,
    ) -> None:
        self._steps = list(steps)
        self._recorder = recorder

    def validate_mcp(self, expected_result: str) -> str:
        self._recorder.record(
            "pi.validate-mcp",
            expected_result=expected_result,
        )
        step = _next_fixture_step(self._steps, "pi_validation")
        result = _fixture_text(step, "result")
        if _fixture_text(step, "status") != "success" or result != expected_result:
            raise PiMcpExecutionAdapterError(
                _fixture_error(step, "fixture Pi validation failed")
            )
        return result


class FixtureExecutionNotificationAdapter:
    def __init__(self, status: str, recorder: _FixtureRecorder) -> None:
        self._status = status
        self._recorder = recorder

    def send(self, text: str) -> None:
        self._recorder.record("notification.send", text=text)
        if self._status != "success":
            raise PiMcpExecutionAdapterError("fixture notification failed")


def build_pi_mcp_execution_adapters(
    settings: Mapping[str, str],
    redactor: Redactor,
) -> PiMcpExecutionAdapters:
    adapter_name = settings.get("RUNTASKS_PI_MCP_EXECUTION_ADAPTER", "local")
    if adapter_name == "local":
        agent_dir = resolve_pi_agent_dir()
        process_runner = SubprocessRunner()
        return PiMcpExecutionAdapters(
            package=PiPackageAdapter(
                agent_dir=agent_dir,
                process_runner=process_runner,
            ),
            service=SystemdServiceAdapter(process_runner=process_runner),
            health=SystemdHealthAdapter(process_runner=process_runner),
            mcp_validation=FreshPiValidationAdapter(
                process_runner=process_runner,
                cwd=agent_dir,
            ),
            notification=TelegramExecutionNotificationAdapter(settings),
            lock_path=agent_dir / ".runtasks-pi-mcp-execution.lock",
        )
    if adapter_name != "fixture":
        raise PiMcpExecutionError(
            "Pi MCP execution adapter is not registered"
        )
    raw_fixture = settings.get("RUNTASKS_FIXTURE_PI_MCP_EXECUTION")
    if raw_fixture is None:
        raise PiMcpExecutionError("fixture Pi MCP execution settings are required")
    try:
        value: object = json.loads(raw_fixture)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise PiMcpExecutionError(
            "fixture Pi MCP execution settings are invalid"
        ) from error
    if not isinstance(value, dict):
        raise PiMcpExecutionError("fixture Pi MCP execution settings are invalid")
    fixture = cast(dict[object, object], value)
    versions = fixture.get("installed_versions")
    if not isinstance(versions, list) or not all(
        isinstance(version, str) for version in versions
    ):
        raise PiMcpExecutionError("fixture installed versions are invalid")
    install = _fixture_steps(fixture, "install")
    restart = _fixture_steps(fixture, "restart")
    health = _fixture_steps(fixture, "health")
    validation = _fixture_steps(fixture, "pi_validation")
    notification = _fixture_mapping(fixture, "notification")
    raw_log_path = settings.get("RUNTASKS_FIXTURE_PI_MCP_EXECUTION_LOG")
    log_path = None if raw_log_path is None else Path(raw_log_path)
    raw_lock_path = settings.get("RUNTASKS_FIXTURE_PI_MCP_EXECUTION_LOCK")
    lock_path = (
        Path(raw_lock_path)
        if raw_lock_path is not None
        else (
            Path.cwd() / ".runtasks-fixture-pi-mcp-execution.lock"
            if log_path is None
            else log_path.with_name(f"{log_path.name}.lock")
        )
    )
    recorder = _FixtureRecorder(log_path, redactor)
    raw_shared_version_path = settings.get(
        "RUNTASKS_FIXTURE_PI_MCP_SHARED_VERSION_PATH"
    )
    shared_version_path = (
        None
        if raw_shared_version_path is None
        else Path(raw_shared_version_path)
    )
    return PiMcpExecutionAdapters(
        package=FixturePackageAdapter(
            cast(tuple[str, ...], tuple(versions)),
            install,
            recorder,
            shared_version_path,
        ),
        service=FixtureServiceAdapter(restart, recorder),
        health=FixtureHealthAdapter(health, recorder),
        mcp_validation=FixturePiValidationAdapter(validation, recorder),
        notification=FixtureExecutionNotificationAdapter(
            _fixture_text(notification, "status"),
            recorder,
        ),
        lock_path=lock_path,
    )


def _run_process(
    process_runner: ProcessRunner,
    argv: tuple[str, ...],
    *,
    timeout_seconds: float,
    failure: str,
    cwd: Path | None = None,
) -> ProcessResult:
    try:
        return process_runner.run(
            argv,
            timeout_seconds=timeout_seconds,
            cwd=cwd,
        )
    except Exception:
        raise PiMcpExecutionAdapterError(failure) from None


def _is_exact_line(value: str, expected: str) -> bool:
    return value == expected or value == f"{expected}\n"


def _fixture_steps(
    fixture: Mapping[object, object],
    name: str,
) -> tuple[dict[object, object], ...]:
    value = fixture.get(name)
    if isinstance(value, list):
        steps: list[dict[object, object]] = []
        for item in value:
            if not isinstance(item, dict):
                raise PiMcpExecutionError(f"fixture {name} settings are invalid")
            steps.append(cast(dict[object, object], item))
        if not steps:
            raise PiMcpExecutionError(f"fixture {name} settings are invalid")
        return tuple(steps)
    return (_fixture_mapping(fixture, name),)


def _next_fixture_step(
    steps: list[dict[object, object]],
    name: str,
) -> dict[object, object]:
    if not steps:
        raise PiMcpExecutionAdapterError(
            f"fixture {name} step is unavailable"
        )
    if len(steps) == 1:
        return steps[0]
    return steps.pop(0)


def _fixture_error(value: Mapping[object, object], default: str) -> str:
    error = value.get("error")
    return error if isinstance(error, str) and error else default


def _fixture_mapping(
    fixture: Mapping[object, object],
    name: str,
) -> dict[object, object]:
    value = fixture.get(name)
    if not isinstance(value, dict):
        raise PiMcpExecutionError(f"fixture {name} settings are invalid")
    return cast(dict[object, object], value)


def _fixture_text(value: Mapping[object, object], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str):
        raise PiMcpExecutionError(f"fixture {name} setting is invalid")
    return item
