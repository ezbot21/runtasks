from __future__ import annotations

import argparse
import json
import sys
from typing import Any, NoReturn, Sequence

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
from runtasks.paths import RuntimePaths
from runtasks.secrets import SecretConfigurationError, load_secret_settings
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


EXIT_VALIDATION_ERROR = 2
_JSON_ARGUMENT_ERRORS = False


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

    search_parser = subparsers.add_parser("search", help="search registered tasks")
    search_parser.add_argument("query")
    _add_output_json_flag(search_parser)

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
        settings = load_app_settings(paths)
        load_secret_settings(paths)
        if options.command == "status":
            return _status(paths, settings, options.as_json)
        if options.command == "init":
            if options.as_json:
                raise ConfigurationError("--json is not supported by init")
            return _initialize(paths)
        if options.command == "task":
            return _task_command(paths, options)
        if options.command == "search":
            return _search(paths, options.query, options.as_json)
    except TaskConflictError as error:
        return _report_task_conflict(error, getattr(options, "as_json", False))
    except (
        ConfigurationError,
        DatabaseError,
        SecretConfigurationError,
        TaskError,
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
    if command in {"search", "status"}:
        return "--json" in arguments[1:]
    if command != "task" or len(arguments) < 2:
        return False
    task_command = arguments[1]
    return task_command not in {"add", "update"} and "--json" in arguments[2:]


def _initialize(paths: RuntimePaths) -> int:
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

    database_changed = initialize_database(paths.database_file)
    changed = changed or database_changed

    if changed:
        print(f"Initialized RunTasks at {paths.home}")
    else:
        print(f"RunTasks is already initialized at {paths.home}")
    return 0


def _status(paths: RuntimePaths, settings: AppSettings, as_json: bool) -> int:
    health = inspect_database(paths.database_file) if paths.database_file.is_file() else None
    payload = _status_payload(paths, settings, health)
    initialized = bool(payload["initialized"])
    if as_json:
        _print_json(payload)
    elif initialized:
        print(f"RunTasks is initialized at {paths.home}")
    else:
        print(f"RunTasks is not initialized at {paths.home}")
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
            print("No Tasks registered.")
        else:
            for task in tasks:
                print(f"{task.id}  {task.name}  [{task.human_availability}]")
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
            print(f"Created Task {task.id}: {task.name}")
        return 0

    if options.task_command == "update":
        current = get_task(paths.database_file, options.task_id)
        task_input = parse_task_update_json(options.payload_json, current)
        task = update_task(paths.database_file, options.task_id, task_input)
        if as_json:
            _print_json({"status": "updated", "task": task.as_dict()})
        else:
            print(f"Updated Task {task.id}: {task.name}")
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
            print(f"{lifecycle} Task {task.id}: {task.name}")
        return 0

    if options.task_command == "remove":
        task = get_task(paths.database_file, options.task_id)
        remove_task(paths.database_file, options.task_id)
        if as_json:
            _print_json({"status": "removed", "task_id": task.id})
        else:
            print(f"Removed Task {task.id}: {task.name}")
        return 0

    raise TaskError("unknown task command")


def _show_task(task: Task, as_json: bool) -> int:
    if as_json:
        _print_json({"status": "ok", "task": task.as_dict()})
        return 0
    source = task.source_type
    if task.source_ref is not None:
        source = f"{source} ({task.source_ref})"
    print(f"Task {task.id}: {task.name}")
    print(f"Status: {task.human_availability}")
    print(f"Schedule: {task.schedule.human_description()} {task.timezone_name}")
    print(f"Next due: {task.next_run_at}")
    print(f"Action: {task.action_mode} via {task.handler}")
    print(f"Description: {task.description}")
    print(f"Source: {source}")
    print(f"Source summary: {task.source_summary}")
    print("Policy:")
    for line in json.dumps(task.policy, indent=2, sort_keys=True).splitlines():
        print(f"  {line}")
    print(f"Created: {task.created_at}")
    print(f"Updated: {task.updated_at}")
    if task.removed_at is not None:
        print(f"Removed: {task.removed_at}")
    return 0


def _search(paths: RuntimePaths, query: str, as_json: bool) -> int:
    tasks = search_tasks(paths.database_file, query)
    if as_json:
        _print_json(
            {
                "query": query,
                "results": [
                    {"task": task.as_dict(), "type": "task"} for task in tasks
                ],
                "status": "ok",
            }
        )
    elif not tasks:
        print("No matching Tasks.")
    else:
        for task in tasks:
            print(f"task  {task.id}  {task.name}")
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
        print(message, file=sys.stderr)
    return EXIT_VALIDATION_ERROR


def _report_error(message: str, as_json: bool) -> int:
    safe_message = f"RunTasks validation failed: {message}"
    if as_json:
        _print_json({"error": safe_message, "status": "error"})
    else:
        print(safe_message, file=sys.stderr)
    return EXIT_VALIDATION_ERROR


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
