from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

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


EXIT_VALIDATION_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="runtasks")
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

    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parser = build_parser()
    options = parser.parse_args(arguments)

    try:
        paths = RuntimePaths.from_environment()
        settings = load_app_settings(paths)
        load_secret_settings(paths)
        if options.command == "status":
            return _status(paths, settings, options.as_json)
        if options.command == "init":
            if options.as_json:
                raise ConfigurationError("--json is only supported by status")
            return _initialize(paths)
    except (ConfigurationError, DatabaseError, SecretConfigurationError) as error:
        return _report_error(str(error), getattr(options, "as_json", False))
    except (OSError, RuntimeError, ValueError):
        return _report_error(
            "runtime home could not be accessed",
            getattr(options, "as_json", False),
        )

    parser.error("unknown command")
    return EXIT_VALIDATION_ERROR


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
