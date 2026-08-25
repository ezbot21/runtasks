from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Literal, Mapping, Sequence

from runtasks.paths import RuntimePaths


DAILY_CALENDAR_EXPRESSION = "*-*-* 09:00:00 Asia/Singapore"
MANAGED_UNIT_NAMES = (
    "runtasks-scheduler.service",
    "runtasks-scheduler.timer",
    "runtasks-telegram.service",
)
_MANAGED_UNIT_HEADER = "# Managed by RunTasks. Re-run 'runtasks install' to update."
_MANAGED_SERVICE_MARKER_NAME = ".runtasks-services-managed.json"
_MANAGED_SKILL_MARKER = ".runtasks-managed.json"
_MANAGED_SKILL_SIDECAR_NAME = ".runtasks-discovery-managed.json"
AgentName = Literal["pi", "codex", "opencode", "claude"]
DiscoveryMode = Literal["symlink", "copy"]
SkillDestinationKind = Literal[
    "missing",
    "symlink",
    "copy",
    "matching-unmanaged-symlink",
    "unmanaged",
]

_AGENT_NAMES: tuple[AgentName, ...] = ("pi", "codex", "opencode", "claude")
_SHARED_SKILL_AGENTS: frozenset[AgentName] = frozenset(
    ("pi", "codex", "opencode")
)
_AUTH_ENVIRONMENT_NAMES = frozenset(
    (
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "OPENCODE_API_KEY",
    )
)


class InstallationError(RuntimeError):
    """Raised when user-level installation cannot be managed safely."""


@dataclass(frozen=True)
class AgentDiscovery:
    name: str
    discovered: bool
    fallback: str | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "discovered": self.discovered,
            "name": self.name,
        }
        if self.fallback is not None:
            payload["fallback"] = self.fallback
        return payload


@dataclass(frozen=True)
class InstallationOutcome:
    agents: tuple[AgentDiscovery, ...]
    services: tuple[str, ...]
    skill_locations: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "agents": [agent.as_dict() for agent in self.agents],
            "services": list(self.services),
            "skill_locations": list(self.skill_locations),
            "status": "installed",
        }


@dataclass(frozen=True)
class SkillInstallationPlan:
    agents: tuple[AgentDiscovery, ...]
    common_mode: DiscoveryMode
    claude_mode: DiscoveryMode


@dataclass(frozen=True)
class UninstallationOutcome:
    data_removed: bool
    removed_services: tuple[str, ...]
    removed_skill_locations: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "data_removed": self.data_removed,
            "removed_services": list(self.removed_services),
            "removed_skill_locations": list(self.removed_skill_locations),
            "status": "uninstalled",
        }


@dataclass(frozen=True)
class UserInstallationPaths:
    runtime_home: Path
    application_root: Path
    user_home: Path
    systemd_user_directory: Path
    canonical_skill: Path
    common_skill: Path
    claude_skill: Path

    @classmethod
    def from_environment(
        cls,
        runtime_paths: RuntimePaths,
        environment: Mapping[str, str],
    ) -> UserInstallationPaths:
        home_value = environment.get("HOME")
        if not home_value:
            raise InstallationError("user home is unavailable for installation")
        user_home = Path(home_value).expanduser().resolve()
        application_value = environment.get("RUNTASKS_APPLICATION_ROOT")
        application_root = (
            Path(application_value).expanduser()
            if application_value
            else Path(__file__).resolve().parents[2]
        ).resolve()
        runtime_home = runtime_paths.home.expanduser().resolve()
        config_home_value = environment.get("XDG_CONFIG_HOME")
        config_home = (
            Path(config_home_value).expanduser().resolve()
            if config_home_value
            else user_home / ".config"
        )
        return cls(
            runtime_home=runtime_home,
            application_root=application_root,
            user_home=user_home,
            systemd_user_directory=config_home / "systemd" / "user",
            canonical_skill=application_root / "skills" / "runtasks",
            common_skill=user_home / ".agents" / "skills" / "runtasks",
            claude_skill=user_home / ".claude" / "skills" / "runtasks",
        )

    @property
    def executable(self) -> Path:
        return self.application_root / "bin" / "runtasks"


def install_user_environment(
    runtime_paths: RuntimePaths,
    *,
    initialize_runtime: Callable[[], object],
    environment: Mapping[str, str] | None = None,
) -> InstallationOutcome:
    process_environment = os.environ if environment is None else environment
    paths = UserInstallationPaths.from_environment(
        runtime_paths,
        process_environment,
    )
    _validate_supported_platform()
    systemd_analyze = _required_executable(
        "systemd-analyze",
        process_environment,
    )
    systemctl = _required_executable("systemctl", process_environment)
    _validate_installation_sources(paths)
    unit_definitions = _unit_definitions(paths, process_environment)
    rollback_activation_on_failure = not _service_marker_is_managed(paths)
    _validate_unit_destinations(paths, unit_definitions)
    _run_required(
        (systemctl, "--user", "show-environment"),
        process_environment,
        "systemd user manager is unavailable",
    )
    validate_systemd_calendar(
        systemd_analyze,
        environment=process_environment,
    )

    agents = _installed_agents(process_environment)
    skill_plan = _prepare_skill_installation(
        paths,
        agents,
        process_environment,
    )
    _validate_skill_installation(paths, skill_plan)
    initialize_runtime()
    _apply_skill_installation(paths, skill_plan)
    paths.systemd_user_directory.mkdir(parents=True, exist_ok=True)
    for name, content in unit_definitions.items():
        _write_managed_file(paths.systemd_user_directory / name, content)

    _run_required(
        (systemctl, "--user", "daemon-reload"),
        process_environment,
        "systemd user manager could not reload RunTasks services",
    )
    _enable_user_services(
        systemctl,
        process_environment,
        rollback_on_failure=rollback_activation_on_failure,
    )
    _write_service_marker(paths)
    return InstallationOutcome(
        agents=skill_plan.agents,
        services=MANAGED_UNIT_NAMES,
        skill_locations=(str(paths.common_skill), str(paths.claude_skill)),
    )


def uninstall_user_environment(
    runtime_paths: RuntimePaths,
    *,
    remove_data: bool,
    environment: Mapping[str, str] | None = None,
) -> UninstallationOutcome:
    process_environment = os.environ if environment is None else environment
    paths = UserInstallationPaths.from_environment(
        runtime_paths,
        process_environment,
    )
    systemctl = _optional_executable("systemctl", process_environment)
    managed_services = frozenset(
        name
        for name in MANAGED_UNIT_NAMES
        if _is_managed_unit(paths.systemd_user_directory / name)
    )
    if managed_services and systemctl is None:
        raise InstallationError(
            "managed RunTasks services cannot be removed without systemctl"
        )
    if systemctl is not None:
        if "runtasks-scheduler.timer" in managed_services:
            _run_required(
                (
                    systemctl,
                    "--user",
                    "disable",
                    "--now",
                    "runtasks-scheduler.timer",
                ),
                process_environment,
                "RunTasks daily timer could not be disabled",
            )
        if "runtasks-telegram.service" in managed_services:
            _run_required(
                (
                    systemctl,
                    "--user",
                    "disable",
                    "--now",
                    "runtasks-telegram.service",
                ),
                process_environment,
                "RunTasks Telegram listener could not be disabled",
            )
        if "runtasks-scheduler.service" in managed_services:
            _run_required(
                (systemctl, "--user", "stop", "runtasks-scheduler.service"),
                process_environment,
                "RunTasks one-shot scheduler could not be stopped",
            )

    removed_services = tuple(
        name
        for name in MANAGED_UNIT_NAMES
        if _remove_managed_unit(paths.systemd_user_directory / name)
    )
    removed_skills = tuple(
        str(destination)
        for destination in (paths.common_skill, paths.claude_skill)
        if _remove_managed_skill(destination, paths.canonical_skill)
    )
    if systemctl is not None and removed_services:
        _run_required(
            (systemctl, "--user", "daemon-reload"),
            process_environment,
            "systemd user manager could not reload after RunTasks uninstallation",
        )
    _remove_service_marker(paths)
    if remove_data:
        _remove_runtime_data(runtime_paths)
    return UninstallationOutcome(
        data_removed=remove_data,
        removed_services=removed_services,
        removed_skill_locations=removed_skills,
    )


def validate_systemd_calendar(
    systemd_analyze: str,
    *,
    environment: Mapping[str, str] | None = None,
) -> None:
    process_environment = os.environ if environment is None else environment
    try:
        result = subprocess.run(
            (systemd_analyze, "calendar", DAILY_CALENDAR_EXPRESSION),
            env=dict(process_environment),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallationError(
            "systemd calendar expression could not be validated"
        ) from error
    if result.returncode != 0:
        raise InstallationError(
            "systemd rejected the RunTasks calendar expression "
            f"'{DAILY_CALENDAR_EXPRESSION}'"
        )


def _validate_supported_platform() -> None:
    if not sys.platform.startswith("linux"):
        raise InstallationError(
            "RunTasks user services are supported only on Linux with systemd"
        )


def _validate_installation_sources(paths: UserInstallationPaths) -> None:
    skill_file = paths.canonical_skill / "SKILL.md"
    if not skill_file.is_file():
        raise InstallationError("canonical RunTasks skill source is unavailable")
    if not paths.executable.is_file():
        raise InstallationError("RunTasks CLI entry point is unavailable")
    _home_specifier(paths.runtime_home, paths.user_home)
    _home_specifier(paths.application_root, paths.user_home)


def _unit_definitions(
    paths: UserInstallationPaths,
    environment: Mapping[str, str],
) -> dict[str, str]:
    runtime_home = _home_specifier(paths.runtime_home, paths.user_home)
    executable = _home_specifier(paths.executable, paths.user_home)
    working_directory = _systemd_quote(runtime_home)
    executable_command = _systemd_quote(executable)
    environment_assignment = _systemd_quote(f"RUNTASKS_HOME={runtime_home}")
    path_assignment = _systemd_quote(
        f"PATH={_systemd_path(environment, paths.user_home)}"
    )
    return {
        "runtasks-scheduler.service": f"""{_MANAGED_UNIT_HEADER}
[Unit]
Description=RunTasks one-shot scheduler runner
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory={working_directory}
Environment={environment_assignment}
Environment={path_assignment}
ExecStart={executable_command} run-due
UMask=0077
""",
        "runtasks-scheduler.timer": f"""{_MANAGED_UNIT_HEADER}
[Unit]
Description=RunTasks daily scheduler timer

[Timer]
OnCalendar={DAILY_CALENDAR_EXPRESSION}
Persistent=true
Unit=runtasks-scheduler.service

[Install]
WantedBy=timers.target
""",
        "runtasks-telegram.service": f"""{_MANAGED_UNIT_HEADER}
[Unit]
Description=RunTasks Telegram decision listener
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={working_directory}
Environment={environment_assignment}
Environment={path_assignment}
ExecStart={executable_command} telegram listen
Restart=on-failure
RestartSec=5s
UMask=0077

[Install]
WantedBy=default.target
""",
    }


def _home_specifier(path: Path, user_home: Path) -> str:
    try:
        relative = path.resolve().relative_to(user_home.resolve())
    except ValueError as error:
        raise InstallationError(
            "RunTasks application and runtime homes must be located under the user home"
        ) from error
    if not relative.parts:
        return "%h"
    return "%h/" + relative.as_posix()


def _systemd_path(
    environment: Mapping[str, str], user_home: Path
) -> str:
    entries: list[str] = []
    for raw_entry in environment.get("PATH", "").split(os.pathsep):
        if not raw_entry:
            continue
        entry = Path(raw_entry).expanduser()
        if not entry.is_absolute():
            continue
        try:
            relative = entry.resolve().relative_to(user_home.resolve())
        except ValueError:
            if _looks_like_another_user_home(entry):
                continue
            rendered = str(entry)
        else:
            rendered = "%h" if not relative.parts else f"%h/{relative.as_posix()}"
        if rendered not in entries:
            entries.append(rendered)
    if not entries:
        raise InstallationError("RunTasks service PATH is unavailable")
    return os.pathsep.join(entries)


def _looks_like_another_user_home(path: Path) -> bool:
    parts = path.resolve().parts
    return (
        len(parts) >= 3
        and parts[0] == os.sep
        and parts[1] in {"home", "Users"}
    )


def _systemd_quote(value: str) -> str:
    if any(character in value for character in (" ", "\t", '"', "\\")):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _service_marker(paths: UserInstallationPaths) -> Path:
    return paths.systemd_user_directory / _MANAGED_SERVICE_MARKER_NAME


def _service_marker_payload_is_managed(paths: UserInstallationPaths) -> bool:
    marker = _service_marker(paths)
    if not marker.is_file() or marker.is_symlink():
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return bool(
        payload
        == {
            "managed_by": "RunTasks",
            "services": list(MANAGED_UNIT_NAMES),
        }
    )


def _service_marker_is_managed(paths: UserInstallationPaths) -> bool:
    return _service_marker_payload_is_managed(paths) and all(
        _is_managed_unit(paths.systemd_user_directory / name)
        for name in MANAGED_UNIT_NAMES
    )


def _write_service_marker(paths: UserInstallationPaths) -> None:
    _write_managed_file(
        _service_marker(paths),
        json.dumps(
            {
                "managed_by": "RunTasks",
                "services": list(MANAGED_UNIT_NAMES),
            },
            sort_keys=True,
        )
        + "\n",
    )


def _remove_service_marker(paths: UserInstallationPaths) -> None:
    if _service_marker_payload_is_managed(paths):
        _service_marker(paths).unlink()


def _validate_unit_destinations(
    paths: UserInstallationPaths,
    definitions: Mapping[str, str],
) -> None:
    for name in definitions:
        destination = paths.systemd_user_directory / name
        if destination.exists() or destination.is_symlink():
            if not _is_managed_unit(destination):
                raise InstallationError(
                    f"{name} exists and is not managed by RunTasks"
                )


def _write_managed_file(destination: Path, content: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
        temporary.chmod(0o644)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _prepare_skill_installation(
    paths: UserInstallationPaths,
    agents: Sequence[AgentName],
    environment: Mapping[str, str],
) -> SkillInstallationPlan:
    shared_agents = tuple(agent for agent in agents if agent in _SHARED_SKILL_AGENTS)
    shared_results = {
        agent: _probe_agent(agent, paths.canonical_skill, "symlink", environment)
        for agent in shared_agents
    }
    shared_fallback = not all(shared_results.values())
    if shared_fallback:
        shared_results = {
            agent: _probe_agent(agent, paths.canonical_skill, "copy", environment)
            for agent in shared_agents
        }

    claude_installed = "claude" in agents
    claude_discovered = True
    claude_fallback = False
    if claude_installed:
        claude_discovered = _probe_agent(
            "claude", paths.canonical_skill, "symlink", environment
        )
        if not claude_discovered:
            claude_fallback = True
            claude_discovered = _probe_agent(
                "claude", paths.canonical_skill, "copy", environment
            )

    results: list[AgentDiscovery] = []
    for agent in agents:
        if agent in _SHARED_SKILL_AGENTS:
            discovered = shared_results[agent]
            fallback = "managed-copy" if shared_fallback else None
        else:
            discovered = claude_discovered
            fallback = "managed-copy" if claude_fallback else None
        if not discovered:
            raise InstallationError(
                f"installed {agent} executable did not discover the RunTasks skill"
            )
        results.append(
            AgentDiscovery(name=agent, discovered=True, fallback=fallback)
        )
    return SkillInstallationPlan(
        agents=tuple(results),
        common_mode="copy" if shared_fallback else "symlink",
        claude_mode="copy" if claude_fallback else "symlink",
    )


def _validate_skill_installation(
    paths: UserInstallationPaths, plan: SkillInstallationPlan
) -> None:
    _validate_skill_destination(
        paths.common_skill,
        paths.canonical_skill,
        plan.common_mode,
    )
    _validate_skill_destination(
        paths.claude_skill,
        paths.canonical_skill,
        plan.claude_mode,
    )


def _apply_skill_installation(
    paths: UserInstallationPaths, plan: SkillInstallationPlan
) -> None:
    _manage_skill_destination(
        paths.common_skill,
        paths.canonical_skill,
        plan.common_mode,
    )
    _manage_skill_destination(
        paths.claude_skill,
        paths.canonical_skill,
        plan.claude_mode,
    )


def _validate_skill_destination(
    destination: Path,
    source: Path,
    mode: DiscoveryMode,
) -> None:
    kind = _managed_skill_kind(destination, source)
    if kind == "unmanaged":
        raise InstallationError(
            f"skill destination {destination} exists and is not managed by RunTasks"
        )
    if kind == "matching-unmanaged-symlink" and mode != "symlink":
        raise InstallationError(
            f"skill destination {destination} exists and is not managed by RunTasks"
        )


def _manage_skill_destination(
    destination: Path,
    source: Path,
    mode: DiscoveryMode,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    _validate_skill_destination(destination, source, mode)
    kind = _managed_skill_kind(destination, source)
    if kind == "matching-unmanaged-symlink":
        return
    if kind == mode:
        if mode == "copy":
            _replace_with_managed_copy(destination, source)
            return
        if _symlink_targets_source(destination, source):
            _write_skill_sidecar(destination, source)
            return
    if kind == "copy":
        shutil.rmtree(destination)
    if mode == "symlink":
        temporary = destination.parent / f".{destination.name}.runtasks-link"
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(source, target_is_directory=True)
        os.replace(temporary, destination)
        _write_skill_sidecar(destination, source)
        return
    _replace_with_managed_copy(destination, source)


def _symlink_targets_source(destination: Path, source: Path) -> bool:
    if not destination.is_symlink():
        return False
    try:
        return destination.resolve() == source.resolve()
    except OSError:
        return False


def _managed_skill_kind(
    destination: Path, source: Path
) -> SkillDestinationKind:
    sidecar_managed = _skill_sidecar_is_managed(destination)
    if destination.is_symlink():
        if sidecar_managed:
            return "symlink"
        if _symlink_targets_source(destination, source):
            return "matching-unmanaged-symlink"
        return "unmanaged"
    if not destination.exists():
        return "missing"
    if not destination.is_dir():
        return "unmanaged"
    if sidecar_managed:
        return "copy"
    marker = destination / _MANAGED_SKILL_MARKER
    if not marker.is_file():
        return "unmanaged"
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "unmanaged"
    return "copy" if payload == {"source": str(source.resolve())} else "unmanaged"


def _replace_with_managed_copy(destination: Path, source: Path) -> None:
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    try:
        shutil.rmtree(temporary)
        shutil.copytree(source, temporary)
        temporary.joinpath(_MANAGED_SKILL_MARKER).write_text(
            json.dumps({"source": str(source.resolve())}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        backup: Path | None = None
        if destination.exists() or destination.is_symlink():
            backup = Path(
                tempfile.mkdtemp(
                    prefix=f".{destination.name}.previous.",
                    dir=destination.parent,
                )
            )
            backup.rmdir()
            os.replace(destination, backup)
        try:
            os.replace(temporary, destination)
            _write_skill_sidecar(destination, source)
        except OSError:
            _remove_path(destination)
            if backup is not None:
                os.replace(backup, destination)
            raise
        if backup is not None:
            _remove_path(backup)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _skill_sidecar(destination: Path) -> Path:
    return destination.parent / _MANAGED_SKILL_SIDECAR_NAME


def _write_skill_sidecar(destination: Path, source: Path) -> None:
    _write_managed_file(
        _skill_sidecar(destination),
        json.dumps(
            {
                "destination": str(destination),
                "managed_by": "RunTasks",
                "source": str(source.resolve()),
            },
            sort_keys=True,
        )
        + "\n",
    )


def _skill_sidecar_is_managed(destination: Path) -> bool:
    sidecar = _skill_sidecar(destination)
    if not sidecar.is_file() or sidecar.is_symlink():
        return False
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not (
        isinstance(payload, dict)
        and payload.get("destination") == str(destination)
        and payload.get("managed_by") == "RunTasks"
        and isinstance(payload.get("source"), str)
    ):
        return False
    recorded_source = Path(payload["source"])
    if destination.is_symlink():
        try:
            target = destination.readlink()
        except OSError:
            return False
        absolute_target = target if target.is_absolute() else destination.parent / target
        return absolute_target.resolve() == recorded_source.resolve()
    if not destination.is_dir():
        return False
    marker = destination / _MANAGED_SKILL_MARKER
    if not marker.is_file() or marker.is_symlink():
        return False
    try:
        copy_payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return bool(copy_payload == {"source": str(recorded_source.resolve())})


def _installed_agents(environment: Mapping[str, str]) -> tuple[AgentName, ...]:
    return tuple(
        name for name in _AGENT_NAMES if _optional_executable(name, environment)
    )


def _probe_agent(
    agent: AgentName,
    source: Path,
    discovery_mode: DiscoveryMode,
    environment: Mapping[str, str],
) -> bool:
    executable = _optional_executable(agent, environment)
    if executable is None:
        return False
    with tempfile.TemporaryDirectory(prefix="runtasks-agent-discovery-") as directory:
        home = Path(directory)
        if agent == "claude":
            destination = home / ".claude" / "skills" / "runtasks"
        else:
            destination = home / ".agents" / "skills" / "runtasks"
        destination.parent.mkdir(parents=True)
        if discovery_mode == "symlink":
            destination.symlink_to(source, target_is_directory=True)
        else:
            shutil.copytree(source, destination)
        probe_environment = _probe_environment(environment, home)
        if agent == "pi":
            result = _run_agent_probe(
                (
                    executable,
                    "--mode",
                    "rpc",
                    "--no-session",
                    "--no-extensions",
                    "--no-prompt-templates",
                    "--no-context-files",
                ),
                probe_environment,
                cwd=home,
                input_text='{"type":"get_commands"}\n',
            )
            return result is not None and _pi_discovered(result.stdout)
        if agent == "codex":
            (home / ".codex").mkdir()
            probe_environment["CODEX_HOME"] = str(home / ".codex")
            result = _run_agent_probe(
                (executable, "debug", "prompt-input", "discovery validation only"),
                probe_environment,
                cwd=home,
            )
            return result is not None and _codex_discovered(result.stdout)
        if agent == "opencode":
            result = _run_agent_probe(
                (executable, "debug", "skill"),
                probe_environment,
                cwd=home,
            )
            return result is not None and _opencode_discovered(result.stdout)
        debug_file = home / "claude-debug.log"
        _run_agent_probe(
            (
                executable,
                "-p",
                "--max-budget-usd",
                "0.0001",
                "--tools",
                "",
                "--debug",
                "skills",
                "--debug-file",
                str(debug_file),
                "discovery validation only",
            ),
            probe_environment,
            cwd=home,
        )
        return _claude_discovered(debug_file)


def _run_agent_probe(
    command: Sequence[str],
    environment: Mapping[str, str],
    *,
    cwd: Path,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            cwd=cwd,
            env=dict(environment),
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _probe_environment(
    environment: Mapping[str, str], home: Path
) -> dict[str, str]:
    values = {
        key: value
        for key, value in environment.items()
        if key not in _AUTH_ENVIRONMENT_NAMES
    }
    values.update(
        {
            "HOME": str(home),
            "PI_CODING_AGENT_DIR": str(home / ".pi" / "agent"),
            "PI_OFFLINE": "1",
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
        }
    )
    return values


def _pi_discovered(output: str) -> bool:
    for line in output.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        commands = payload.get("data", {}).get("commands", []) if isinstance(payload, dict) else []
        if any(
            isinstance(command, dict)
            and command.get("name") == "skill:runtasks"
            and command.get("source") == "skill"
            for command in commands
        ):
            return True
    return False


def _codex_discovered(output: str) -> bool:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return False
    return any("runtasks" in text.lower() and "skill" in text.lower() for text in _json_strings(payload))


def _json_strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(text for item in value for text in _json_strings(item))
    if isinstance(value, dict):
        return tuple(text for item in value.values() for text in _json_strings(item))
    return ()


def _opencode_discovered(output: str) -> bool:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, list) and any(
        isinstance(skill, dict) and skill.get("name") == "runtasks"
        for skill in payload
    )


def _claude_discovered(debug_file: Path) -> bool:
    try:
        output = debug_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return bool(
        re.search(
            r"Loaded 1 unique skills .*\buser: 1\b",
            output,
        )
    )


def _required_executable(name: str, environment: Mapping[str, str]) -> str:
    executable = _optional_executable(name, environment)
    if executable is None:
        raise InstallationError(
            f"systemd user services are unsupported: {name} was not found"
        )
    return executable


def _optional_executable(
    name: str, environment: Mapping[str, str]
) -> str | None:
    return shutil.which(name, path=environment.get("PATH"))


def _enable_user_services(
    systemctl: str,
    environment: Mapping[str, str],
    *,
    rollback_on_failure: bool,
) -> None:
    _run_required(
        (systemctl, "--user", "enable", "--now", "runtasks-scheduler.timer"),
        environment,
        "RunTasks daily timer could not be enabled",
    )
    try:
        _run_required(
            (systemctl, "--user", "enable", "--now", "runtasks-telegram.service"),
            environment,
            "RunTasks Telegram listener could not be enabled",
        )
    except InstallationError:
        if rollback_on_failure:
            _run_best_effort(
                (
                    systemctl,
                    "--user",
                    "disable",
                    "--now",
                    "runtasks-scheduler.timer",
                ),
                environment,
            )
            _run_best_effort(
                (
                    systemctl,
                    "--user",
                    "disable",
                    "--now",
                    "runtasks-telegram.service",
                ),
                environment,
            )
        raise


def _run_required(
    command: Sequence[str],
    environment: Mapping[str, str],
    message: str,
) -> None:
    try:
        result = subprocess.run(
            command,
            env=dict(environment),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallationError(message) from error
    if result.returncode != 0:
        raise InstallationError(message)


def _run_best_effort(
    command: Sequence[str], environment: Mapping[str, str]
) -> None:
    try:
        subprocess.run(
            command,
            env=dict(environment),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def _is_managed_unit(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink() or not path.is_file():
        return False
    try:
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, UnicodeError, IndexError):
        return False
    return first_line == _MANAGED_UNIT_HEADER


def _remove_managed_unit(path: Path) -> bool:
    if not _is_managed_unit(path):
        return False
    path.unlink()
    return True


def _remove_managed_skill(destination: Path, source: Path) -> bool:
    sidecar_managed = _skill_sidecar_is_managed(destination)
    kind = _managed_skill_kind(destination, source)
    removed = False
    if kind == "symlink":
        destination.unlink()
        removed = True
    elif kind == "copy":
        shutil.rmtree(destination)
        removed = True
    if sidecar_managed:
        _skill_sidecar(destination).unlink()
        removed = True
    return removed


def _remove_runtime_data(paths: RuntimePaths) -> None:
    for path in (
        paths.secret_environment_file,
        paths.config_directory,
        paths.log_directory,
        paths.backup_directory,
    ):
        _remove_path(path)

    if paths.data_directory.resolve() == paths.global_lock_directory.resolve():
        _remove_data_except_global_locks(paths.data_directory)
    else:
        _remove_path(paths.data_directory)
    _remove_empty_directory(paths.home / "var")


def _remove_data_except_global_locks(data_directory: Path) -> None:
    if not data_directory.is_dir() or data_directory.is_symlink():
        _remove_path(data_directory)
        return
    for child in data_directory.iterdir():
        if child.name.startswith("telegram-poller-") and child.name.endswith(".lock"):
            continue
        _remove_path(child)
    _remove_empty_directory(data_directory)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _remove_empty_directory(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        return
