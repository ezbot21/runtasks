from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI = PROJECT_ROOT / "bin" / "runtasks"


class RunTasksCliTests(unittest.TestCase):
    def run_cli(
        self,
        home: Path | None,
        *arguments: str,
        extra_environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("RUNTASKS_")
        }
        if home is not None:
            environment["RUNTASKS_HOME"] = str(home)
        if extra_environment:
            environment.update(extra_environment)

        return subprocess.run(
            [str(CLI), *arguments],
            cwd=PROJECT_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_status_reports_an_uninitialized_temporary_home_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"

            result = self.run_cli(home, "status", "--json")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout),
                {
                    "configuration": {
                        "daily_run_time": "09:00",
                        "source": "defaults",
                        "timezone": "Asia/Singapore",
                    },
                    "database": {
                        "exists": False,
                        "path": str(home / "var" / "data" / "runtasks.sqlite3"),
                    },
                    "home": str(home),
                    "initialized": False,
                    "status": "uninitialized",
                },
            )
            self.assertFalse(home.exists())

    def test_status_defaults_to_runtasks_under_the_current_users_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            user_home = Path(directory) / "user"
            user_home.mkdir()

            result = self.run_cli(
                None,
                "status",
                "--json",
                extra_environment={"HOME": str(user_home)},
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["home"], str(user_home / "runtasks"))
            self.assertFalse((user_home / "runtasks").exists())

    def test_json_flag_is_accepted_before_the_command_for_automation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"

            result = self.run_cli(home, "--json", "status")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "uninitialized")

    def test_human_output_concisely_reports_both_runtime_states(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"

            before = self.run_cli(home, "status")
            initialization = self.run_cli(home, "init")
            after = self.run_cli(home, "status")

            self.assertEqual(before.returncode, 0, before.stderr)
            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            self.assertEqual(after.returncode, 0, after.stderr)
            self.assertEqual(before.stdout.strip(), f"RunTasks is not initialized at {home}")
            self.assertEqual(initialization.stdout.strip(), f"Initialized RunTasks at {home}")
            self.assertEqual(after.stdout.strip(), f"RunTasks is initialized at {home}")

    def test_status_loads_non_secret_toml_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            init_result = self.run_cli(home, "init")
            self.assertEqual(init_result.returncode, 0, init_result.stderr)
            (home / "config" / "runtasks.toml").write_text(
                'timezone = "UTC"\ndaily_run_time = "23:15"\n',
                encoding="utf-8",
            )

            result = self.run_cli(home, "status", "--json")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout)["configuration"],
                {
                    "daily_run_time": "23:15",
                    "source": str(home / "config" / "runtasks.toml"),
                    "timezone": "UTC",
                },
            )

    def test_status_never_prints_process_environment_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            token = "bot-token-must-not-leak"
            invalid_ids = "private-id-value"

            result = self.run_cli(
                home,
                "status",
                "--json",
                extra_environment={
                    "RUNTASKS_TELEGRAM_BOT_TOKEN": token,
                    "RUNTASKS_TELEGRAM_ALLOWED_USER_IDS": invalid_ids,
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "uninitialized")
            self.assertNotIn(token, result.stdout + result.stderr)
            self.assertNotIn(invalid_ids, result.stdout + result.stderr)
            self.assertFalse(home.exists())

    def test_private_environment_file_failure_is_nonzero_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            home.mkdir()
            token = "file-token-must-not-leak"
            invalid_ids = "file-private-id-value"
            (home / ".env").write_text(
                "\n".join(
                    (
                        f"RUNTASKS_TELEGRAM_BOT_TOKEN={token}",
                        invalid_ids,
                    )
                ),
                encoding="utf-8",
            )

            result = self.run_cli(home, "status", "--json")

            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["status"], "error")
            self.assertNotIn(token, result.stdout + result.stderr)
            self.assertNotIn(invalid_ids, result.stdout + result.stderr)

    def test_invalid_toml_configuration_is_nonzero_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            config_directory = home / "config"
            config_directory.mkdir(parents=True)
            private_value = "private-config-value"
            (config_directory / "runtasks.toml").write_text(
                f'timezone = "{private_value}\n',
                encoding="utf-8",
            )

            result = self.run_cli(home, "status", "--json")

            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["status"], "error")
            self.assertNotIn(private_value, result.stdout + result.stderr)

    def test_runtime_home_filesystem_failure_is_nonzero_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "private-runtime-home-value"
            home.write_text("not a directory", encoding="utf-8")

            result = self.run_cli(home, "init")

            self.assertEqual(result.returncode, 2)
            self.assertIn("runtime home could not be accessed", result.stderr)
            self.assertNotIn(str(home), result.stdout + result.stderr)

    def test_status_does_not_change_an_uninitialized_databases_journal_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            database_file = home / "var" / "data" / "runtasks.sqlite3"
            database_file.parent.mkdir(parents=True)
            sqlite3.connect(database_file).close()

            result = self.run_cli(home, "status", "--json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "uninitialized")
            self.assertEqual(payload["database"]["journal_mode"], "delete")
            with sqlite3.connect(database_file) as connection:
                journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(journal_mode, "delete")

    def test_repeated_init_preserves_existing_runtime_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            first_init = self.run_cli(home, "init")
            self.assertEqual(first_init.returncode, 0, first_init.stderr)
            log_directory = home / "var" / "logs"
            database_file = home / "var" / "data" / "runtasks.sqlite3"
            log_directory.chmod(0o750)
            database_file.chmod(0o640)

            second_init = self.run_cli(home, "init")

            self.assertEqual(second_init.returncode, 0, second_init.stderr)
            self.assertEqual(stat.S_IMODE(log_directory.stat().st_mode), 0o750)
            self.assertEqual(stat.S_IMODE(database_file.stat().st_mode), 0o640)

    def test_init_creates_a_healthy_runtime_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"

            first_init = self.run_cli(home, "init")
            second_init = self.run_cli(home, "init")
            status = self.run_cli(home, "status", "--json")

            self.assertEqual(first_init.returncode, 0, first_init.stderr)
            self.assertEqual(second_init.returncode, 0, second_init.stderr)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(first_init.stdout.strip(), f"Initialized RunTasks at {home}")
            self.assertEqual(
                second_init.stdout.strip(),
                f"RunTasks is already initialized at {home}",
            )

            self.assertEqual(
                json.loads(status.stdout),
                {
                    "configuration": {
                        "daily_run_time": "09:00",
                        "source": str(home / "config" / "runtasks.toml"),
                        "timezone": "Asia/Singapore",
                    },
                    "database": {
                        "busy_timeout_ms": 5000,
                        "exists": True,
                        "foreign_keys": True,
                        "fts5": True,
                        "journal_mode": "wal",
                        "path": str(home / "var" / "data" / "runtasks.sqlite3"),
                        "schema_version": 5,
                    },
                    "home": str(home),
                    "initialized": True,
                    "status": "initialized",
                },
            )
            for relative_directory in ("config", "var/data", "var/logs", "var/backups"):
                self.assertTrue((home / relative_directory).is_dir())

    def test_init_migrates_a_bootstrap_database_to_the_task_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            database_file = home / "var" / "data" / "runtasks.sqlite3"
            database_file.parent.mkdir(parents=True)
            with sqlite3.connect(database_file) as connection:
                connection.execute(
                    """
                    CREATE TABLE schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
                    ("2026-01-01T00:00:00+00:00",),
                )

            initialization = self.run_cli(home, "init")
            status = self.run_cli(home, "status", "--json")
            tasks = self.run_cli(home, "--json", "task", "list")

            self.assertEqual(initialization.returncode, 0, initialization.stderr)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(json.loads(status.stdout)["database"]["schema_version"], 5)
            self.assertEqual(tasks.returncode, 0, tasks.stderr)
            self.assertEqual(json.loads(tasks.stdout)["tasks"], [])

    def test_init_migrates_schema_two_without_losing_registered_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            self.assertEqual(self.run_cli(home, "init").returncode, 0)
            task_payload = {
                "name": "Manual maintenance reminder",
                "description": "Review maintenance state manually.",
                "source_type": "direct",
                "source_ref": None,
                "source_summary": "A retained schema-two Task.",
                "schedule": {"type": "daily", "time": "09:00"},
                "timezone": "Asia/Singapore",
                "next_run_at": "2026-09-01T01:00:00Z",
                "action_mode": "notify",
                "handler": "manual_notification",
                "policy": {"message": "Review maintenance state."},
            }
            added = self.run_cli(
                home,
                "--json",
                "task",
                "add",
                "--json",
                json.dumps(task_payload),
            )
            self.assertEqual(added.returncode, 0, added.stderr)
            task = json.loads(added.stdout)["task"]
            database_file = home / "var" / "data" / "runtasks.sqlite3"
            with sqlite3.connect(database_file) as connection:
                connection.execute("DROP TABLE decisions")
                connection.execute("DROP TABLE decision_fts")
                connection.execute("DROP TABLE runs")
                connection.execute("DROP TABLE run_fts")
                connection.execute("DELETE FROM schema_migrations WHERE version >= 3")

            migrated = self.run_cli(home, "init")
            status = self.run_cli(home, "--json", "status")
            shown = self.run_cli(home, "--json", "task", "show", task["id"])
            history = self.run_cli(home, "--json", "history", task["id"])

            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            self.assertEqual(json.loads(status.stdout)["database"]["schema_version"], 5)
            self.assertEqual(json.loads(shown.stdout)["task"], task)
            self.assertEqual(json.loads(history.stdout)["runs"], [])

    def test_init_migrates_schema_four_without_losing_run_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            self.assertEqual(self.run_cli(home, "init").returncode, 0)
            task_payload = {
                "name": "Retained schema-four reminder",
                "description": "Keep this Task and its Run during migration.",
                "source_type": "direct",
                "source_ref": None,
                "source_summary": "Schema-four migration coverage.",
                "schedule": {"type": "daily", "time": "09:00"},
                "timezone": "Asia/Singapore",
                "next_run_at": "2026-09-01T01:00:00Z",
                "action_mode": "notify",
                "handler": "manual_notification",
                "policy": {"message": "Review retained migration state."},
            }
            added = self.run_cli(
                home,
                "--json",
                "task",
                "add",
                "--json",
                json.dumps(task_payload),
            )
            self.assertEqual(added.returncode, 0, added.stderr)
            task = json.loads(added.stdout)["task"]
            executed = self.run_cli(home, "run", task["id"], "--json")
            self.assertEqual(executed.returncode, 0, executed.stderr)
            retained_run = json.loads(executed.stdout)["run"]
            database_file = home / "var" / "data" / "runtasks.sqlite3"
            with sqlite3.connect(database_file) as connection:
                connection.execute("DROP TABLE decisions")
                connection.execute("DROP TABLE decision_fts")
                connection.execute("DELETE FROM schema_migrations WHERE version = 5")
                version = connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
            self.assertEqual(version, 4)

            migrated = self.run_cli(home, "init")
            status = self.run_cli(home, "status", "--json")
            history = self.run_cli(home, "history", task["id"], "--json")
            decisions = self.run_cli(home, "decisions", "--json")

            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            self.assertEqual(json.loads(status.stdout)["database"]["schema_version"], 5)
            self.assertEqual(json.loads(history.stdout)["runs"], [retained_run])
            self.assertEqual(json.loads(decisions.stdout)["decisions"], [])


if __name__ == "__main__":
    unittest.main()
