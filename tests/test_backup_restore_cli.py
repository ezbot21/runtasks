from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from typing import Any, cast
import unittest
from unittest.mock import patch

from runtasks.backups import BackupError, create_backup, restore_backup
from runtasks.database import (
    DatabaseError,
    database_connection,
    initialize_database,
)
from tests.cli_test_support import run_cli


class BackupRestoreCliTests(unittest.TestCase):
    def downgrade_to_schema_two(self, database_path: Path) -> None:
        with sqlite3.connect(database_path) as connection:
            connection.execute("DROP TABLE telegram_decision_messages")
            connection.execute("DROP TABLE approval_run_trigger_requests")
            connection.execute("DROP TABLE decisions")
            connection.execute("DROP TABLE decision_fts")
            connection.execute("DROP TABLE runs")
            connection.execute("DROP TABLE run_fts")
            connection.execute("DELETE FROM schema_migrations WHERE version >= 3")

    def add_notify_task(
        self,
        home: Path,
        *,
        name: str,
        phrase: str,
    ) -> dict[str, Any]:
        payload = {
            "name": name,
            "description": f"{phrase} registry state.",
            "source_type": "direct",
            "source_ref": None,
            "source_summary": f"{phrase} source summary.",
            "schedule": {"type": "daily", "time": "09:00"},
            "timezone": "Asia/Singapore",
            "next_run_at": "2026-09-01T01:00:00Z",
            "action_mode": "notify",
            "handler": "manual_notification",
            "policy": {"message": f"Review {phrase.lower()} state."},
        }
        result = run_cli(
            home,
            "--json",
            "task",
            "add",
            "--json",
            json.dumps(payload),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return cast(dict[str, Any], json.loads(result.stdout)["task"])

    def test_restore_requires_explicit_replacement_and_recovers_user_visible_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_home = root / "source-home"
            restored_home = root / "restored-home"
            self.assertEqual(run_cli(source_home, "init").returncode, 0)
            task = self.add_notify_task(
                source_home,
                name="Restorable maintenance Task",
                phrase="Restorable",
            )
            executed = run_cli(source_home, "run", task["id"], "--json")
            self.assertEqual(executed.returncode, 0, executed.stderr)
            run = json.loads(executed.stdout)["run"]
            database_path = source_home / "var" / "data" / "runtasks.sqlite3"
            timestamp = "2026-09-02T00:00:00+00:00"
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    """
                    INSERT INTO decisions(
                        id, task_id, run_id, status, plan_json, plan_hash,
                        reason, validation_summary, rollback_summary,
                        response_action, response_channel, responded_by,
                        responded_at, approval_run_id, execution_scheduled_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, NULL, NULL,
                              NULL, NULL, NULL, NULL, ?, ?)
                    """,
                    (
                        "decision-restorable",
                        task["id"],
                        run["id"],
                        '{"operation":"review"}',
                        hashlib.sha256(
                            b'{"operation":"review"}'
                        ).hexdigest(),
                        "Restorable approval reason.",
                        "Restorable validation summary.",
                        "Restorable rollback summary.",
                        timestamp,
                        timestamp,
                    ),
                )
            backed_up = run_cli(source_home, "backup", "--json")
            self.assertEqual(backed_up.returncode, 0, backed_up.stderr)
            backup_path = json.loads(backed_up.stdout)["backup"]["path"]

            preview = run_cli(restored_home, "restore", backup_path, "--json")

            self.assertEqual(preview.returncode, 2)
            self.assertFalse(
                (restored_home / "var" / "data" / "runtasks.sqlite3").exists()
            )

            restored = run_cli(
                restored_home,
                "restore",
                backup_path,
                "--replace-live",
                "--json",
            )

            self.assertEqual(restored.returncode, 0, restored.stderr)
            restore_payload = json.loads(restored.stdout)
            self.assertEqual(restore_payload["status"], "restored")
            self.assertEqual(restore_payload["restore"]["source_schema_version"], 6)
            self.assertEqual(restore_payload["restore"]["schema_version"], 6)
            restored_status = run_cli(restored_home, "status", "--json")
            self.assertEqual(restored_status.returncode, 0, restored_status.stderr)
            self.assertEqual(
                json.loads(restored_status.stdout)["database"]["journal_mode"],
                "wal",
            )
            for command in (
                ("task", "list", "--json"),
                ("history", "--json"),
                ("decisions", "--json"),
                ("search", "restorable", "--json"),
            ):
                source_result = run_cli(source_home, *command)
                restored_result = run_cli(restored_home, *command)
                self.assertEqual(source_result.returncode, 0, source_result.stderr)
                self.assertEqual(restored_result.returncode, 0, restored_result.stderr)
                self.assertEqual(
                    json.loads(restored_result.stdout),
                    json.loads(source_result.stdout),
                )
            human_restore = run_cli(
                restored_home,
                "restore",
                backup_path,
                "--replace-live",
            )
            self.assertEqual(human_restore.returncode, 0, human_restore.stderr)
            self.assertIn("Restored schema 6 backup", human_restore.stdout)

    def test_replacing_live_state_first_creates_a_verified_safety_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_home = root / "source-home"
            live_home = root / "live-home"
            self.assertEqual(run_cli(source_home, "init").returncode, 0)
            source_backup = run_cli(source_home, "backup", "--json")
            self.assertEqual(source_backup.returncode, 0, source_backup.stderr)
            source_backup_path = json.loads(source_backup.stdout)["backup"]["path"]
            self.assertEqual(run_cli(live_home, "init").returncode, 0)
            self.add_notify_task(
                live_home,
                name="Live state safety marker",
                phrase="Safety backup",
            )

            result = run_cli(
                live_home,
                "restore",
                source_backup_path,
                "--replace-live",
                "--json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            safety_backup = json.loads(result.stdout)["restore"]["safety_backup"]
            self.assertIsNotNone(safety_backup)
            safety_path = Path(safety_backup["path"])
            with sqlite3.connect(f"file:{safety_path}?mode=ro", uri=True) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                    1,
                )
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            tasks = run_cli(live_home, "task", "list", "--json")
            self.assertEqual(tasks.returncode, 0, tasks.stderr)
            self.assertEqual(json.loads(tasks.stdout)["tasks"], [])

    def test_restore_rejects_database_validation_failures(self) -> None:
        for failure in (
            "foreign-key",
            "schema",
            "schema-extra",
            "fts",
            "fts-content",
            "fts-index",
        ):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source_home = root / "source-home"
                restored_home = root / "restored-home"
                self.assertEqual(run_cli(source_home, "init").returncode, 0)
                self.add_notify_task(
                    source_home,
                    name="Validation backup Task",
                    phrase="Validation backup",
                )
                backed_up = run_cli(source_home, "backup", "--json")
                self.assertEqual(backed_up.returncode, 0, backed_up.stderr)
                backup_path = Path(json.loads(backed_up.stdout)["backup"]["path"])
                with sqlite3.connect(backup_path) as connection:
                    if failure == "foreign-key":
                        connection.execute("PRAGMA foreign_keys = OFF")
                        connection.execute(
                            """
                            INSERT INTO runs(
                                id, task_id, task_name, trigger, status,
                                created_at, summary, details_json
                            ) VALUES (
                                'orphan-run', 'missing-task', 'Missing Task',
                                'manual', 'success',
                                '2026-09-01T00:00:00+00:00',
                                'Orphaned Run', '{}'
                            )
                            """
                        )
                    elif failure == "schema":
                        connection.execute("DROP TRIGGER tasks_fts_delete")
                    elif failure == "schema-extra":
                        connection.execute(
                            "CREATE TABLE task_fts_malicious(value TEXT)"
                        )
                    elif failure == "fts":
                        connection.execute("DROP TABLE task_fts")
                        connection.execute(
                            """
                            CREATE TABLE task_fts (
                                task_id TEXT,
                                name TEXT,
                                description TEXT,
                                source_summary TEXT,
                                policy TEXT
                            )
                            """
                        )
                    elif failure == "fts-content":
                        connection.execute("DELETE FROM task_fts")
                    else:
                        connection.execute(
                            "DELETE FROM task_fts_data WHERE id > 10"
                        )
                metadata_path = backup_path.with_suffix(".json")
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata["checksum_sha256"] = hashlib.sha256(
                    backup_path.read_bytes()
                ).hexdigest()
                metadata_path.write_text(
                    json.dumps(metadata, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

                result = run_cli(
                    restored_home,
                    "restore",
                    str(backup_path),
                    "--replace-live",
                    "--json",
                )

                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(result.stdout)["status"], "error")
                self.assertFalse(
                    (restored_home / "var" / "data" / "runtasks.sqlite3").exists()
                )

    def test_corrupt_backup_cannot_replace_a_healthy_live_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_home = root / "source-home"
            live_home = root / "live-home"
            self.assertEqual(run_cli(source_home, "init").returncode, 0)
            backed_up = run_cli(source_home, "backup", "--json")
            self.assertEqual(backed_up.returncode, 0, backed_up.stderr)
            backup_path = Path(json.loads(backed_up.stdout)["backup"]["path"])
            backup_path.write_bytes(b"not a SQLite database")
            self.assertEqual(run_cli(live_home, "init").returncode, 0)
            before = run_cli(live_home, "status", "--json")

            restored = run_cli(
                live_home,
                "restore",
                str(backup_path),
                "--replace-live",
                "--json",
            )
            after = run_cli(live_home, "status", "--json")

            self.assertEqual(restored.returncode, 2)
            self.assertEqual(json.loads(restored.stdout)["status"], "error")
            self.assertEqual(after.returncode, 0, after.stderr)
            self.assertEqual(json.loads(after.stdout), json.loads(before.stdout))
            self.assertEqual(list((live_home / "var" / "backups").iterdir()), [])

    def test_restore_refuses_to_replace_live_state_while_application_connection_is_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_home = root / "source-home"
            live_home = root / "live-home"
            self.assertEqual(run_cli(source_home, "init").returncode, 0)
            backed_up = run_cli(source_home, "backup", "--json")
            self.assertEqual(backed_up.returncode, 0, backed_up.stderr)
            backup_path = json.loads(backed_up.stdout)["backup"]["path"]
            self.assertEqual(run_cli(live_home, "init").returncode, 0)
            live_task = self.add_notify_task(
                live_home,
                name="Open connection live Task",
                phrase="Open connection",
            )
            live_database = live_home / "var" / "data" / "runtasks.sqlite3"

            with database_connection(live_database, enable_wal=False):
                result = run_cli(
                    live_home,
                    "restore",
                    backup_path,
                    "--replace-live",
                    "--json",
                )

            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["status"], "error")
            shown = run_cli(
                live_home,
                "task",
                "show",
                live_task["id"],
                "--json",
            )
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertEqual(json.loads(shown.stdout)["task"], live_task)
            self.assertEqual(list((live_home / "var" / "backups").iterdir()), [])

    def test_failed_restore_destination_leaves_live_database_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_home = root / "source-home"
            live_home = root / "live-home"
            self.assertEqual(run_cli(source_home, "init").returncode, 0)
            backed_up = run_cli(source_home, "backup", "--json")
            self.assertEqual(backed_up.returncode, 0, backed_up.stderr)
            backup_path = Path(json.loads(backed_up.stdout)["backup"]["path"])
            self.assertEqual(run_cli(live_home, "init").returncode, 0)
            live_task = self.add_notify_task(
                live_home,
                name="Failed destination live Task",
                phrase="Failed destination",
            )
            live_database = live_home / "var" / "data" / "runtasks.sqlite3"

            real_replace = os.replace

            def fail_live_replacement(source: str | Path, destination: str | Path) -> None:
                if Path(destination) == live_database:
                    raise OSError("simulated final destination failure")
                real_replace(source, destination)

            with patch(
                "runtasks.backups.os.replace",
                side_effect=fail_live_replacement,
            ):
                with self.assertRaises(BackupError):
                    restore_backup(
                        backup_path,
                        live_database,
                        live_home / "var" / "backups",
                        replace_live=True,
                    )

            shown = run_cli(
                live_home,
                "task",
                "show",
                live_task["id"],
                "--json",
            )
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertEqual(json.loads(shown.stdout)["task"], live_task)
            with sqlite3.connect(live_database) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )
            self.assertEqual(
                list((live_home / "var" / "data").glob(".restore-*.sqlite3")),
                [],
            )

    def test_failed_backup_destination_returns_json_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            self.assertEqual(run_cli(home, "init").returncode, 0)
            backup_directory = home / "var" / "backups"
            backup_directory.rmdir()
            backup_directory.write_text("not a directory", encoding="utf-8")

            result = run_cli(home, "backup", "--json")

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)["status"], "error")
            self.assertEqual(
                list((home / "var" / "data").glob("*.backup*")),
                [],
            )

    def test_pre_migration_backup_failure_leaves_the_live_schema_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            self.assertEqual(run_cli(home, "init").returncode, 0)
            database_path = home / "var" / "data" / "runtasks.sqlite3"
            self.downgrade_to_schema_two(database_path)

            def fail_backup() -> None:
                raise BackupError("simulated backup destination failure")

            with self.assertRaises(BackupError):
                initialize_database(
                    database_path,
                    before_existing_change=fail_backup,
                )

            with sqlite3.connect(database_path) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()[0],
                    2,
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name = 'runs'"
                    ).fetchone()
                )

    def test_backup_creates_a_verified_private_portable_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            initialized = run_cli(home, "init")
            self.assertEqual(initialized.returncode, 0, initialized.stderr)

            result = run_cli(home, "backup", "--json")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "created")
            backup = payload["backup"]
            backup_path = Path(backup["path"])
            metadata_path = Path(backup["metadata_path"])
            self.assertTrue(backup_path.is_file())
            self.assertTrue(metadata_path.is_file())
            self.assertEqual(backup_path.parent, home / "var" / "backups")
            self.assertEqual(backup_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(metadata_path.stat().st_mode & 0o777, 0o600)
            self.assertIn("-v6-", backup_path.name)
            self.assertEqual(backup["schema_version"], 6)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["schema_version"], 6)
            self.assertEqual(metadata["database_file"], backup_path.name)
            self.assertEqual(
                metadata["checksum_sha256"],
                hashlib.sha256(backup_path.read_bytes()).hexdigest(),
            )
            self.assertNotIn("policy", metadata)
            with sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True) as connection:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(
                    connection.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()[0],
                    6,
                )
            human_result = run_cli(home, "backup")
            self.assertEqual(human_result.returncode, 0, human_result.stderr)
            self.assertIn("Created backup", human_result.stdout)
            self.assertIn("schema 6", human_result.stdout)

    def test_mismatched_backup_name_and_metadata_is_rejected_and_not_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "runtime-home"
            restored_home = root / "restored-home"
            self.assertEqual(run_cli(home, "init").returncode, 0)
            result = run_cli(home, "backup", "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            artifact = json.loads(result.stdout)["backup"]
            backup_path = Path(artifact["path"])
            metadata_path = Path(artifact["metadata_path"])
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["created_at"] = "2000-01-01T00:00:00Z"
            metadata_path.write_text(
                json.dumps(metadata, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            restored = run_cli(
                restored_home,
                "restore",
                str(backup_path),
                "--replace-live",
                "--json",
            )
            next_backup = run_cli(home, "backup", "--json")

            self.assertEqual(restored.returncode, 2)
            self.assertEqual(json.loads(restored.stdout)["status"], "error")
            self.assertEqual(next_backup.returncode, 0, next_backup.stderr)
            self.assertTrue(backup_path.exists())
            self.assertTrue(metadata_path.exists())

    def test_init_backs_up_an_existing_schema_zero_database_before_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            database_path = home / "var" / "data" / "runtasks.sqlite3"
            database_path.parent.mkdir(parents=True)
            sqlite3.connect(database_path).close()

            result = run_cli(home, "init")

            self.assertEqual(result.returncode, 0, result.stderr)
            backups = list(
                (home / "var" / "backups").glob("runtasks-backup-v0-*.sqlite3")
            )
            self.assertEqual(len(backups), 1)
            with sqlite3.connect(database_path) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()[0],
                    6,
                )

    def test_init_creates_a_verified_backup_before_migrating_an_existing_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            self.assertEqual(run_cli(home, "init").returncode, 0)
            database_path = home / "var" / "data" / "runtasks.sqlite3"
            self.downgrade_to_schema_two(database_path)
            self.assertEqual(
                list((home / "var" / "backups").iterdir()),
                [],
            )

            result = run_cli(home, "init")

            self.assertEqual(result.returncode, 0, result.stderr)
            metadata_paths = list(
                (home / "var" / "backups").glob("runtasks-backup-v2-*.json")
            )
            self.assertEqual(len(metadata_paths), 1)
            metadata = json.loads(metadata_paths[0].read_text(encoding="utf-8"))
            backup_path = metadata_paths[0].parent / metadata["database_file"]
            self.assertTrue(backup_path.is_file())
            with sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            with sqlite3.connect(database_path) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()[0],
                    6,
                )
            restored_home = Path(directory) / "restored-home"
            restored = run_cli(
                restored_home,
                "restore",
                str(backup_path),
                "--replace-live",
                "--json",
            )
            self.assertEqual(restored.returncode, 0, restored.stderr)
            restored_payload = json.loads(restored.stdout)["restore"]
            self.assertEqual(restored_payload["source_schema_version"], 2)
            self.assertEqual(restored_payload["schema_version"], 6)

    def test_staging_migration_failure_cannot_replace_live_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_home = root / "source-home"
            live_home = root / "live-home"
            self.assertEqual(run_cli(source_home, "init").returncode, 0)
            source_database = source_home / "var" / "data" / "runtasks.sqlite3"
            self.downgrade_to_schema_two(source_database)
            backed_up = run_cli(source_home, "backup", "--json")
            self.assertEqual(backed_up.returncode, 0, backed_up.stderr)
            backup_path = json.loads(backed_up.stdout)["backup"]["path"]
            self.assertEqual(run_cli(live_home, "init").returncode, 0)
            before = run_cli(live_home, "status", "--json")

            with patch(
                "runtasks.backups.initialize_staged_database",
                side_effect=DatabaseError("simulated staging migration failure"),
            ):
                with self.assertRaises(BackupError):
                    restore_backup(
                        Path(backup_path),
                        live_home / "var" / "data" / "runtasks.sqlite3",
                        live_home / "var" / "backups",
                        replace_live=True,
                    )
            after = run_cli(live_home, "status", "--json")

            self.assertEqual(after.returncode, 0, after.stderr)
            self.assertEqual(json.loads(after.stdout), json.loads(before.stdout))
            self.assertEqual(list((live_home / "var" / "backups").iterdir()), [])

    def test_retention_failure_does_not_publish_an_unbounded_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            self.assertEqual(run_cli(home, "init").returncode, 0)
            database_path = home / "var" / "data" / "runtasks.sqlite3"
            backup_directory = home / "var" / "backups"
            start = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
            artifacts = [
                create_backup(
                    database_path,
                    backup_directory,
                    created_at=start + timedelta(days=offset),
                )
                for offset in range(14)
            ]
            blocked_path = artifacts[0].path
            real_unlink = Path.unlink

            def fail_old_backup_deletion(
                path: Path,
                missing_ok: bool = False,
            ) -> None:
                if path == blocked_path:
                    raise OSError("simulated retention destination failure")
                real_unlink(path, missing_ok=missing_ok)

            with patch("pathlib.Path.unlink", new=fail_old_backup_deletion):
                with self.assertRaises(BackupError):
                    create_backup(
                        database_path,
                        backup_directory,
                        created_at=start + timedelta(days=14),
                    )

            self.assertEqual(
                len(list(backup_directory.glob("runtasks-backup-v*-*.sqlite3"))),
                14,
            )
            self.assertEqual(
                len(list(backup_directory.glob("runtasks-backup-v*-*.json"))),
                14,
            )

    def test_retention_keeps_one_backup_for_each_of_the_latest_fourteen_days(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            self.assertEqual(run_cli(home, "init").returncode, 0)
            database_path = home / "var" / "data" / "runtasks.sqlite3"
            backup_directory = home / "var" / "backups"
            start = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
            artifacts = [
                create_backup(
                    database_path,
                    backup_directory,
                    created_at=start + timedelta(days=offset),
                )
                for offset in range(15)
            ]
            same_day_older = create_backup(
                database_path,
                backup_directory,
                created_at=start + timedelta(days=14, hours=1),
            )
            unknown_file = backup_directory / "operator-note.sqlite3"
            unknown_file.write_text("leave me alone", encoding="utf-8")

            newest = create_backup(
                database_path,
                backup_directory,
                created_at=start + timedelta(days=14, hours=2),
            )

            retained_metadata = sorted(
                backup_directory.glob("runtasks-backup-v*-*.json")
            )
            retained_databases = sorted(
                backup_directory.glob("runtasks-backup-v*-*.sqlite3")
            )
            self.assertEqual(len(retained_metadata), 14)
            self.assertEqual(len(retained_databases), 14)
            self.assertFalse(Path(artifacts[0].path).exists())
            self.assertFalse(Path(artifacts[0].metadata_path).exists())
            self.assertFalse(Path(same_day_older.path).exists())
            self.assertFalse(Path(same_day_older.metadata_path).exists())
            self.assertTrue(Path(newest.path).exists())
            self.assertTrue(unknown_file.exists())

    def test_backup_captures_a_consistent_snapshot_during_live_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            self.assertEqual(run_cli(home, "init").returncode, 0)
            task = self.add_notify_task(
                home,
                name="Concurrent backup Task",
                phrase="Concurrent backup",
            )
            task_id = task["id"]
            database_path = home / "var" / "data" / "runtasks.sqlite3"
            with sqlite3.connect(database_path) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()

            stop = threading.Event()
            started = threading.Event()

            def write_continuously() -> None:
                with database_connection(
                    database_path,
                    enable_wal=False,
                ) as connection:
                    sequence = 1
                    while not stop.is_set():
                        connection.execute(
                            "UPDATE tasks SET description = ?, updated_at = ? "
                            "WHERE id = ?",
                            (
                                f"Concurrent sequence {sequence}",
                                f"2026-09-01T00:00:{sequence % 60:02d}+00:00",
                                task_id,
                            ),
                        )
                        connection.commit()
                        sequence += 1
                        if sequence >= 10:
                            started.set()
                        time.sleep(0.001)

            writer = threading.Thread(target=write_continuously)
            writer.start()
            self.assertTrue(started.wait(timeout=5.0))
            try:
                result = run_cli(home, "backup", "--json")
            finally:
                stop.set()
                writer.join(timeout=5.0)

            self.assertEqual(result.returncode, 0, result.stderr)
            backup_path = Path(json.loads(result.stdout)["backup"]["path"])
            with sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True) as connection:
                description = connection.execute(
                    "SELECT description FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()[0]
                indexed_description = connection.execute(
                    "SELECT description FROM task_fts WHERE task_id = ?",
                    (task_id,),
                ).fetchone()[0]
                self.assertTrue(description.startswith("Concurrent sequence "))
                self.assertEqual(indexed_description, description)
                self.assertEqual(
                    connection.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )


if __name__ == "__main__":
    unittest.main()
