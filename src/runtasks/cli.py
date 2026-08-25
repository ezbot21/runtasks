from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping, NoReturn, Sequence, cast

from runtasks.adapters import ExternalAdapterError, build_external_adapter
from runtasks.backups import (
    BackupError,
    create_backup,
    create_backup_from_locked_database,
    restore_backup,
)
from runtasks.cli_output import (
    configure_cli_redactor,
    print_json,
    print_text,
)
from runtasks.config import (
    AppSettings,
    ConfigurationError,
    default_config_text,
    load_app_settings,
)
from runtasks.database import (
    LATEST_SCHEMA_VERSION,
    DatabaseError,
    DatabaseHealth,
    initialize_database,
    inspect_database,
)
from runtasks.decisions import (
    Decision,
    DecisionError,
    get_decision,
    list_decisions,
    respond_to_decision,
    search_decisions,
)
from runtasks.handlers import build_handler_registry
from runtasks.installation import (
    InstallationError,
    install_user_environment,
    uninstall_user_environment,
)
from runtasks.notifications import (
    NotificationDeliveryError,
    NotificationDestinationError,
)
from runtasks.one_shot import OneShotRunTriggerError
from runtasks.paths import RuntimePaths
from runtasks.redaction import DEFAULT_REDACTOR, Redactor
from runtasks.runs import Run, RunError, execute_manual_run, list_runs, search_runs
from runtasks.scheduler import (
    FixedClock,
    SchedulerValidationError,
    SystemClock,
    parse_scheduler_time,
    run_due_tasks,
)
from runtasks.secrets import (
    SecretConfigurationError,
    environment_redaction_values,
    load_secret_settings,
)
from runtasks.tasks import (
    Task,
    TaskConflictError,
    TaskError,
    create_task,
    get_task,
    list_tasks,
    parse_task_add_json,
    parse_task_update_json,
    remove_task,
    search_tasks,
    set_task_enabled,
    update_task,
)
from runtasks.telegram import (
    PollerAlreadyRunningError,
    TelegramConfigurationError,
    TelegramDeliveryError,
)
from runtasks.telegram_decisions import TelegramDecisionError
from runtasks.telegram_cli import add_telegram_parser, run_telegram_command


EXIT_EXECUTION_ERROR = 1
EXIT_VALIDATION_ERROR = 2
_JSON_ARGUMENT_ERRORS = False
_ACTIVE_REDACTOR = DEFAULT_REDACTOR


class CliArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        if _JSON_ARGUMENT_ERRORS:
            _print_json(
                {
                    "error": (
                        "RunTasks validation failed: command-line arguments are "
                        "invalid; run 'runtasks --help' for usage"
                    ),
                    "status": "error",
                }
            )
            raise SystemExit(EXIT_VALIDATION_ERROR)
        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    parser = CliArgumentParser(prog="runtasks")
    parser.add_argument("--json", action="store_true", dest="as_json")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="report runtime health")
    status_parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        default=argparse.SUPPRESS,
    )

    subparsers.add_parser("init", help="initialize the runtime home")

    install_parser = subparsers.add_parser(
        "install", help="install RunTasks user services and skill discovery"
    )
    _add_output_json_flag(install_parser)

    uninstall_parser = subparsers.add_parser(
        "uninstall", help="remove managed RunTasks user installation files"
    )
    uninstall_parser.add_argument(
        "--remove-data",
        action="store_true",
        help="also remove RunTasks configuration, secrets, database, logs, and backups",
    )
    _add_output_json_flag(uninstall_parser)

    backup_parser = subparsers.add_parser(
        "backup", help="create a verified SQLite backup"
    )
    _add_output_json_flag(backup_parser)

    restore_parser = subparsers.add_parser(
        "restore", help="restore a verified SQLite backup"
    )
    restore_parser.add_argument("backup_path")
    restore_parser.add_argument(
        "--replace-live",
        action="store_true",
        help="explicitly replace the live registry after staged validation",
    )
    _add_output_json_flag(restore_parser)

    task_parser = subparsers.add_parser("task", help="manage scheduled tasks")
    task_actions = task_parser.add_subparsers(dest="task_command", required=True)

    task_list_parser = task_actions.add_parser("list", help="list tasks")
    _add_output_json_flag(task_list_parser)

    task_show_parser = task_actions.add_parser("show", help="show a task")
    task_show_parser.add_argument("task_id")
    _add_output_json_flag(task_show_parser)

    task_add_parser = task_actions.add_parser("add", help="add a task")
    task_add_parser.add_argument(
        "--json",
        required=True,
        dest="payload_json",
        help="structured task JSON",
    )
    task_add_parser.add_argument(
        "--output-json",
        action="store_true",
        dest="as_json",
        default=argparse.SUPPRESS,
    )

    task_update_parser = task_actions.add_parser("update", help="update a task")
    task_update_parser.add_argument("task_id")
    task_update_parser.add_argument(
        "--json",
        required=True,
        dest="payload_json",
        help="structured task update JSON",
    )
    task_update_parser.add_argument(
        "--output-json",
        action="store_true",
        dest="as_json",
        default=argparse.SUPPRESS,
    )

    for action in ("enable", "disable", "remove"):
        lifecycle_parser = task_actions.add_parser(action, help=f"{action} a task")
        lifecycle_parser.add_argument("task_id")
        _add_output_json_flag(lifecycle_parser)

    run_parser = subparsers.add_parser("run", help="run a Task manually")
    run_parser.add_argument("task_id")
    _add_output_json_flag(run_parser)

    run_due_parser = subparsers.add_parser(
        "run-due", help="claim and run all due Tasks"
    )
    run_due_parser.add_argument(
        "--now",
        help="deterministic offset-aware RFC 3339 scheduler time",
    )
    _add_output_json_flag(run_due_parser)

    history_parser = subparsers.add_parser("history", help="list Run history")
    history_parser.add_argument("task_id", nargs="?")
    _add_output_json_flag(history_parser)

    decisions_parser = subparsers.add_parser(
        "decisions", help="list human approval Decisions"
    )
    _add_output_json_flag(decisions_parser)

    decision_parser = subparsers.add_parser(
        "decision", help="inspect or respond to a Decision"
    )
    decision_actions = decision_parser.add_subparsers(
        dest="decision_command", required=True
    )
    for action in ("show", "approve", "reject"):
        action_parser = decision_actions.add_parser(
            action, help=f"{action} a Decision"
        )
        action_parser.add_argument("decision_id")
        _add_output_json_flag(action_parser)

    search_parser = subparsers.add_parser(
        "search", help="search registered Tasks, Runs, and Decisions"
    )
    search_parser.add_argument("query")
    _add_output_json_flag(search_parser)

    add_telegram_parser(subparsers)

    return parser


def _add_output_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        default=argparse.SUPPRESS,
    )


def main(arguments: Sequence[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if arguments is None else arguments)
    global _JSON_ARGUMENT_ERRORS
    _JSON_ARGUMENT_ERRORS = _json_output_requested(raw_arguments)
    parser = build_parser()
    options = parser.parse_args(raw_arguments)

    try:
        paths = RuntimePaths.from_environment()
        if options.command == "install":
            return _install(paths, options.as_json)
        if options.command == "uninstall":
            return _uninstall(paths, options.remove_data, options.as_json)

        settings = load_app_settings(paths)
        secret_settings = load_secret_settings(paths)
        process_redaction_values = environment_redaction_values()
        global _ACTIVE_REDACTOR
        _ACTIVE_REDACTOR = Redactor.from_secret_settings(secret_settings)
        configure_cli_redactor(_ACTIVE_REDACTOR)
        if options.command == "status":
            return _status(paths, settings, options.as_json)
        if options.command == "init":
            if options.as_json:
                raise ConfigurationError("--json is not supported by init")
            return _initialize(paths)
        if options.command == "backup":
            return _backup(paths, options.as_json)
        if options.command == "restore":
            return _restore(
                paths,
                Path(options.backup_path),
                options.replace_live,
                options.as_json,
            )
        if options.command == "task":
            return _task_command(paths, options)
        if options.command == "run":
            return _run_task(
                paths,
                options.task_id,
                options.as_json,
                secret_settings,
            )
        if options.command == "run-due":
            return _run_due(
                paths,
                options.now,
                options.as_json,
                secret_settings,
            )
        if options.command == "history":
            return _history(paths, options.task_id, options.as_json)
        if options.command == "decisions":
            return _decisions(paths, options.as_json)
        if options.command == "decision":
            return _decision_command(paths, options)
        if options.command == "search":
            return _search(paths, options.query, options.as_json)
        if options.command == "telegram":
            configure_cli_redactor(
                Redactor.from_secret_values(
                    (*secret_settings.values(), *process_redaction_values)
                )
            )
            return run_telegram_command(
                paths,
                secret_settings,
                process_redaction_values,
                options,
            )
    except TaskConflictError as error:
        return _report_task_conflict(error, getattr(options, "as_json", False))
    except (
        NotificationDeliveryError,
        OneShotRunTriggerError,
        TelegramDeliveryError,
        PollerAlreadyRunningError,
    ):
        return _report_execution_error(
            "Telegram operation failed",
            getattr(options, "as_json", False),
        )
    except (
        BackupError,
        ConfigurationError,
        InstallationError,
        DatabaseError,
        SecretConfigurationError,
        ExternalAdapterError,
        DecisionError,
        RunError,
        SchedulerValidationError,
        TaskError,
        NotificationDestinationError,
        TelegramConfigurationError,
        TelegramDecisionError,
    ) as error:
        return _report_error(str(error), getattr(options, "as_json", False))
    except (OSError, RuntimeError, ValueError):
        return _report_error(
            "runtime home could not be accessed",
            getattr(options, "as_json", False),
        )

    parser.error("unknown command")
    return EXIT_VALIDATION_ERROR


def _json_output_requested(arguments: Sequence[str]) -> bool:
    if "--output-json" in arguments:
        return True
    if not arguments:
        return False
    if arguments[0] == "--json":
        return True
    command = arguments[0]
    if command in {
        "backup",
        "decision",
        "decisions",
        "history",
        "install",
        "run",
        "restore",
        "run-due",
        "search",
        "status",
        "telegram",
        "uninstall",
    }:
        return "--json" in arguments[1:]
    if command != "task" or len(arguments) < 2:
        return False
    task_command = arguments[1]
    return task_command not in {"add", "update"} and "--json" in arguments[2:]


def _initialize(paths: RuntimePaths) -> int:
    changed = _initialize_runtime(paths)
    if changed:
        _safe_print(f"Initialized RunTasks at {paths.home}")
    else:
        _safe_print(f"RunTasks is already initialized at {paths.home}")
    return 0


def _initialize_runtime(paths: RuntimePaths) -> bool:
    changed = _ensure_runtime_layout(paths)

    def backup_existing_database() -> None:
        create_backup_from_locked_database(
            paths.database_file,
            paths.backup_directory,
        )

    database_changed = initialize_database(
        paths.database_file,
        before_existing_change=backup_existing_database,
    )
    return changed or database_changed


def _install(paths: RuntimePaths, as_json: bool) -> int:
    outcome = install_user_environment(
        paths,
        initialize_runtime=lambda: _initialize_runtime(paths),
    )
    if as_json:
        _print_json(outcome.as_dict())
    else:
        _safe_print("Installed RunTasks user services and skill discovery.")
        for service in outcome.services:
            _safe_print(f"Service: {service}")
        for agent in outcome.agents:
            fallback = (
                f" ({agent.fallback} fallback)" if agent.fallback is not None else ""
            )
            _safe_print(f"Skill discovery: {agent.name} verified{fallback}")
    return 0


def _uninstall(paths: RuntimePaths, remove_data: bool, as_json: bool) -> int:
    outcome = uninstall_user_environment(paths, remove_data=remove_data)
    if as_json:
        _print_json(outcome.as_dict())
    else:
        _safe_print("Removed managed RunTasks user services and skill discovery.")
        if remove_data:
            _safe_print(
                "Removed RunTasks configuration, secrets, database, logs, and backups."
            )
        else:
            _safe_print(
                "Preserved RunTasks configuration, secrets, database, logs, and backups."
            )
    return 0


def _ensure_runtime_layout(paths: RuntimePaths) -> bool:
    changed = False
    for directory in paths.required_directories:
        if not directory.exists():
            directory.mkdir(parents=True, mode=0o700)
            changed = True
        elif not directory.is_dir():
            raise OSError("runtime directory path is not a directory")
    if not paths.config_file.exists():
        paths.config_file.write_text(default_config_text(), encoding="utf-8")
        changed = True
    return changed


def _backup(paths: RuntimePaths, as_json: bool) -> int:
    artifact = create_backup(paths.database_file, paths.backup_directory)
    if as_json:
        _print_json({"backup": artifact.as_dict(), "status": "created"})
    else:
        _safe_print(
            f"Created backup {artifact.path} "
            f"(schema {artifact.schema_version}, {artifact.created_at})."
        )
    return 0


def _restore(
    paths: RuntimePaths,
    backup_path: Path,
    replace_live: bool,
    as_json: bool,
) -> int:
    if not replace_live:
        raise BackupError("restore requires explicit --replace-live confirmation")
    _ensure_runtime_layout(paths)
    outcome = restore_backup(
        backup_path,
        paths.database_file,
        paths.backup_directory,
        replace_live=True,
    )
    if as_json:
        _print_json({"restore": outcome.as_dict(), "status": "restored"})
    else:
        _safe_print(
            f"Restored schema {outcome.schema_version} backup to "
            f"{outcome.destination}."
        )
    return 0


def _status(paths: RuntimePaths, settings: AppSettings, as_json: bool) -> int:
    health = inspect_database(paths.database_file) if paths.database_file.is_file() else None
    payload = _status_payload(paths, settings, health)
    initialized = bool(payload["initialized"])
    if as_json:
        _print_json(payload)
    elif initialized:
        _safe_print(f"RunTasks is initialized at {paths.home}")
    else:
        _safe_print(f"RunTasks is not initialized at {paths.home}")
    return 0


def _status_payload(
    paths: RuntimePaths,
    settings: AppSettings,
    health: DatabaseHealth | None,
) -> dict[str, Any]:
    database: dict[str, Any]
    if health is None:
        database = {"exists": False, "path": str(paths.database_file)}
    else:
        database = health.as_dict()

    initialized = (
        health is not None
        and health.schema_version == LATEST_SCHEMA_VERSION
        and health.foreign_keys
        and health.fts5
        and paths.config_file.is_file()
        and all(directory.is_dir() for directory in paths.required_directories)
    )
    return {
        "configuration": {
            "daily_run_time": settings.daily_run_time_text,
            "source": settings.source.display_name,
            "timezone": settings.timezone_name,
        },
        "database": database,
        "home": str(paths.home),
        "initialized": initialized,
        "status": "initialized" if initialized else "uninitialized",
    }


def _task_command(paths: RuntimePaths, options: argparse.Namespace) -> int:
    as_json = bool(options.as_json)
    if options.task_command == "list":
        tasks = list_tasks(paths.database_file)
        if as_json:
            _print_json({"status": "ok", "tasks": [task.as_dict() for task in tasks]})
        elif not tasks:
            _safe_print("No Tasks registered.")
        else:
            for task in tasks:
                _safe_print(
                    f"{task.id}  {task.name}  [{task.human_availability}]  "
                    f"next due {task.next_run_local}"
                )
        return 0

    if options.task_command == "show":
        task = get_task(
            paths.database_file,
            options.task_id,
            include_removed=True,
        )
        return _show_task(task, as_json)

    if options.task_command == "add":
        task_input = parse_task_add_json(options.payload_json)
        task = create_task(paths.database_file, task_input)
        if as_json:
            _print_json({"status": "created", "task": task.as_dict()})
        else:
            _safe_print(f"Created Task {task.id}: {task.name}")
        return 0

    if options.task_command == "update":
        current = get_task(paths.database_file, options.task_id)
        task_input = parse_task_update_json(options.payload_json, current)
        task = update_task(paths.database_file, options.task_id, task_input)
        if as_json:
            _print_json({"status": "updated", "task": task.as_dict()})
        else:
            _safe_print(f"Updated Task {task.id}: {task.name}")
        return 0

    if options.task_command in {"enable", "disable"}:
        enabled = options.task_command == "enable"
        task = set_task_enabled(paths.database_file, options.task_id, enabled)
        if as_json:
            _print_json(
                {
                    "status": "enabled" if enabled else "disabled",
                    "task": task.as_dict(),
                }
            )
        else:
            lifecycle = "Enabled" if enabled else "Disabled"
            _safe_print(f"{lifecycle} Task {task.id}: {task.name}")
        return 0

    if options.task_command == "remove":
        task = get_task(paths.database_file, options.task_id)
        remove_task(paths.database_file, options.task_id)
        if as_json:
            _print_json({"status": "removed", "task_id": task.id})
        else:
            _safe_print(f"Removed Task {task.id}: {task.name}")
        return 0

    raise TaskError("unknown task command")


def _show_task(task: Task, as_json: bool) -> int:
    if as_json:
        _print_json({"status": "ok", "task": task.as_dict()})
        return 0
    source = task.source_type
    if task.source_ref is not None:
        source = f"{source} ({task.source_ref})"
    _safe_print(f"Task {task.id}: {task.name}")
    _safe_print(f"Status: {task.human_availability}")
    _safe_print(f"Schedule: {task.schedule.human_description()} {task.timezone_name}")
    _safe_print(f"Next due: {task.next_run_local} ({task.next_run_at})")
    _safe_print(f"Action: {task.action_mode} via {task.handler}")
    _safe_print(f"Description: {task.description}")
    _safe_print(f"Source: {source}")
    _safe_print(f"Source summary: {task.source_summary}")
    _safe_print("Policy:")
    for line in json.dumps(task.policy, indent=2, sort_keys=True).splitlines():
        _safe_print(f"  {line}")
    _safe_print(f"Created: {task.created_at}")
    _safe_print(f"Updated: {task.updated_at}")
    if task.removed_at is not None:
        _safe_print(f"Removed: {task.removed_at}")
    return 0


def _run_task(
    paths: RuntimePaths,
    task_id: str,
    as_json: bool,
    secret_settings: Mapping[str, str],
) -> int:
    adapter = build_external_adapter(secret_settings, _ACTIVE_REDACTOR)
    handler_registry = build_handler_registry(secret_settings, _ACTIVE_REDACTOR)
    run = execute_manual_run(
        paths.database_file,
        task_id,
        adapter,
        handler_registry,
        _ACTIVE_REDACTOR,
    )
    if as_json:
        _print_json({"run": run.as_dict(), "status": run.status})
    else:
        _print_run(run)
    return EXIT_EXECUTION_ERROR if run.status == "failed" else 0


def _run_due(
    paths: RuntimePaths,
    now: str | None,
    as_json: bool,
    secret_settings: Mapping[str, str],
) -> int:
    clock = SystemClock() if now is None else FixedClock(parse_scheduler_time(now))
    adapter = build_external_adapter(secret_settings, _ACTIVE_REDACTOR)
    handler_registry = build_handler_registry(secret_settings, _ACTIVE_REDACTOR)
    result = run_due_tasks(
        paths.database_file,
        clock,
        adapter,
        handler_registry,
        _ACTIVE_REDACTOR,
    )
    if as_json:
        _print_json(
            {
                "current_time": result.current_time,
                "runs": [run.as_dict() for run in result.runs],
                "status": result.status,
            }
        )
    elif not result.runs:
        _safe_print(f"No Tasks due at {result.current_time}.")
    else:
        for run in result.runs:
            _print_run(run)
    return (
        EXIT_EXECUTION_ERROR
        if any(run.status == "failed" for run in result.runs)
        else 0
    )


def _history(paths: RuntimePaths, task_id: str | None, as_json: bool) -> int:
    runs = list_runs(paths.database_file, task_id=task_id)
    if as_json:
        _print_json({"runs": [run.as_dict() for run in runs], "status": "ok"})
    elif not runs:
        _safe_print("No Runs recorded.")
    else:
        for run in runs:
            _print_run(run)
    return 0


def _print_run(run: Run) -> None:
    _safe_print(
        f"{run.id}  {run.task_name}  [{run.status}]  "
        f"{run.trigger}  {run.summary}"
    )


def _decisions(paths: RuntimePaths, as_json: bool) -> int:
    decisions = list_decisions(paths.database_file)
    if as_json:
        _print_json(
            {
                "decisions": [decision.as_dict() for decision in decisions],
                "status": "ok",
            }
        )
    elif not decisions:
        _safe_print("No Decisions recorded.")
    else:
        for decision in decisions:
            _safe_print(
                f"{decision.id}  Task {decision.task_id}  "
                f"[{decision.status}]  {decision.reason}"
            )
    return 0


def _decision_command(paths: RuntimePaths, options: argparse.Namespace) -> int:
    as_json = bool(options.as_json)
    if options.decision_command == "show":
        decision = get_decision(paths.database_file, options.decision_id)
        return _show_decision(decision, as_json)
    if options.decision_command in {"approve", "reject"}:
        decision = respond_to_decision(
            paths.database_file,
            options.decision_id,
            options.decision_command,
        )
        if as_json:
            _print_json(
                {"decision": decision.as_dict(), "status": decision.status}
            )
        else:
            verb = (
                "Approved"
                if options.decision_command == "approve"
                else "Rejected"
            )
            _safe_print(f"{verb} Decision {decision.id} for Task {decision.task_id}.")
        return 0
    raise DecisionError("unknown Decision command")


def _show_decision(decision: Decision, as_json: bool) -> int:
    if as_json:
        _print_json({"decision": decision.as_dict(), "status": "ok"})
        return 0
    _safe_print(f"Decision {decision.id}")
    _safe_print(f"Status: {decision.status}")
    _safe_print(f"Task: {decision.task_id}")
    _safe_print(f"Requesting Run: {decision.run_id}")
    _safe_print(f"Reason: {decision.reason}")
    _safe_print(f"Validation: {decision.validation_summary}")
    _safe_print(f"Rollback: {decision.rollback_summary}")
    _safe_print(f"Plan hash: {decision.plan_hash}")
    _safe_print("Plan:")
    for line in json.dumps(decision.plan, indent=2, sort_keys=True).splitlines():
        _safe_print(f"  {line}")
    if decision.response is not None:
        _safe_print(
            f"Response: {decision.response.action} by "
            f"{decision.response.responded_by} via {decision.response.channel} "
            f"at {decision.response.responded_at}"
        )
    if decision.approval_run_id is not None:
        _safe_print(f"Approval Run: {decision.approval_run_id}")
    _safe_print(f"Created: {decision.created_at}")
    _safe_print(f"Updated: {decision.updated_at}")
    return 0


def _search(paths: RuntimePaths, query: str, as_json: bool) -> int:
    tasks = search_tasks(paths.database_file, query)
    runs = search_runs(paths.database_file, query)
    decisions = search_decisions(paths.database_file, query)
    results = [
        *({"task": task.as_dict(), "type": "task"} for task in tasks),
        *({"run": run.as_dict(), "type": "run"} for run in runs),
        *(
            {"decision": decision.as_dict(), "type": "decision"}
            for decision in decisions
        ),
    ]
    if as_json:
        _print_json({"query": query, "results": results, "status": "ok"})
    elif not results:
        _safe_print("No matching Tasks, Runs, or Decisions.")
    else:
        for result in results:
            if result["type"] == "task":
                task_payload = cast(dict[str, object], result["task"])
                _safe_print(f"task  {task_payload['id']}  {task_payload['name']}")
            elif result["type"] == "run":
                run_payload = cast(dict[str, object], result["run"])
                _safe_print(
                    f"run  {run_payload['id']}  {run_payload['task_name']}  "
                    f"[{run_payload['status']}]"
                )
            else:
                decision_payload = cast(dict[str, object], result["decision"])
                _safe_print(
                    f"decision  {decision_payload['id']}  "
                    f"Task {decision_payload['task_id']}  "
                    f"[{decision_payload['status']}]"
                )
    return 0


def _report_task_conflict(error: TaskConflictError, as_json: bool) -> int:
    message = f"RunTasks validation failed: {error}"
    if as_json:
        _print_json(
            {
                "error": message,
                "existing_task_id": error.existing_task_id,
                "outcome": "update-existing",
                "reason": error.reason,
                "status": "duplicate",
            }
        )
    else:
        _safe_print(message, error=True)
    return EXIT_VALIDATION_ERROR


def _report_execution_error(message: str, as_json: bool) -> int:
    safe_message = f"RunTasks execution failed: {message}"
    if as_json:
        _print_json({"error": safe_message, "status": "error"})
    else:
        _safe_print(safe_message, error=True)
    return EXIT_EXECUTION_ERROR


def _report_error(message: str, as_json: bool) -> int:
    safe_message = f"RunTasks validation failed: {message}"
    if as_json:
        _print_json({"error": safe_message, "status": "error"})
    else:
        _safe_print(safe_message, error=True)
    return EXIT_VALIDATION_ERROR


def _safe_print(message: str, *, error: bool = False) -> None:
    print_text(message, error=error)


def _print_json(payload: dict[str, Any]) -> None:
    print_json(payload)


if __name__ == "__main__":
    raise SystemExit(main())
