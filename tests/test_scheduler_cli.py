from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest
from typing import Any, cast


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI = PROJECT_ROOT / "bin" / "runtasks"


class SchedulerCliTests(unittest.TestCase):
    def cli_environment(
        self,
        home: Path,
        extra_environment: dict[str, str] | None = None,
    ) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("RUNTASKS_")
        }
        environment["RUNTASKS_HOME"] = str(home)
        if extra_environment is not None:
            environment.update(extra_environment)
        return environment

    def run_cli(
        self,
        home: Path,
        *arguments: str,
        extra_environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(CLI), *arguments],
            cwd=PROJECT_ROOT,
            env=self.cli_environment(home, extra_environment),
            text=True,
            capture_output=True,
            check=False,
        )

    def initialize(self, home: Path) -> None:
        result = self.run_cli(home, "init")
        self.assertEqual(result.returncode, 0, result.stderr)

    def add_task(self, home: Path, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": "Scheduled operations reminder",
            "description": "Review the operations dashboard.",
            "source_type": "direct",
            "source_ref": None,
            "source_summary": "A scheduled review is required.",
            "schedule": {"type": "daily", "time": "09:00"},
            "timezone": "Asia/Singapore",
            "next_run_at": "2026-09-01T01:00:00Z",
            "action_mode": "notify",
            "handler": "manual_notification",
            "policy": {"message": "Review the dashboard manually."},
        }
        payload.update(overrides)
        if "policy" not in overrides:
            payload["policy"] = {"message": f"Review {payload['name']} manually."}
        result = self.run_cli(
            home,
            "--json",
            "task",
            "add",
            "--json",
            json.dumps(payload),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return cast(dict[str, Any], json.loads(result.stdout)["task"])

    def fixture_environment(
        self,
        request_log: Path | None = None,
        *,
        status: str = "success",
        delay_seconds: str | None = None,
    ) -> dict[str, str]:
        environment = {
            "RUNTASKS_FIXTURE_HANDLER_OUTCOME": json.dumps(
                {
                    "status": status,
                    "summary": f"Scheduled fixture finished with {status}.",
                    "details": {"fixture_status": status},
                }
            )
        }
        if request_log is not None:
            environment["RUNTASKS_FIXTURE_HANDLER_REQUEST_LOG"] = str(request_log)
        if delay_seconds is not None:
            environment["RUNTASKS_FIXTURE_HANDLER_DELAY_SECONDS"] = delay_seconds
        return environment

    def test_run_due_selects_only_enabled_tasks_due_at_the_deterministic_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            self.initialize(home)
            due = self.add_task(home, name="Due Task")
            self.add_task(
                home,
                name="Future Task",
                next_run_at="2026-09-02T01:00:00Z",
            )
            disabled = self.add_task(home, name="Disabled Task")
            self.assertEqual(
                self.run_cli(home, "task", "disable", disabled["id"]).returncode,
                0,
            )

            result = self.run_cli(
                home,
                "run-due",
                "--now",
                "2026-09-01T01:00:00Z",
                "--json",
                extra_environment=self.fixture_environment(),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "executed")
            self.assertEqual(payload["current_time"], "2026-09-01T01:00:00Z")
            self.assertEqual(len(payload["runs"]), 1)
            run = payload["runs"][0]
            self.assertEqual(run["task_id"], due["id"])
            self.assertEqual(run["trigger"], "scheduled")
            self.assertEqual(run["scheduled_for"], "2026-09-01T01:00:00Z")
            self.assertEqual(run["next_run_at"], "2026-09-02T01:00:00Z")
            self.assertEqual(
                run["details"]["scheduling"],
                {
                    "missed_occurrences_skipped": 0,
                    "next_run_at": "2026-09-02T01:00:00Z",
                    "scheduled_for": "2026-09-01T01:00:00Z",
                },
            )

    def test_interval_task_catches_up_once_and_repeated_invocations_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            self.initialize(home)
            task = self.add_task(
                home,
                name="Fortnightly Task",
                schedule={"type": "interval-days", "days": 14, "time": "09:00"},
                next_run_at="2026-08-01T01:00:00Z",
            )
            environment = self.fixture_environment()

            first = self.run_cli(
                home,
                "--json",
                "run-due",
                "--now",
                "2026-09-01T01:00:00Z",
                extra_environment=environment,
            )
            second = self.run_cli(
                home,
                "run-due",
                "--now",
                "2026-09-01T01:00:00Z",
                "--json",
                extra_environment=environment,
            )
            shown = self.run_cli(home, "task", "show", task["id"], "--json")
            history = self.run_cli(home, "history", task["id"], "--json")

            self.assertEqual(first.returncode, 0, first.stderr)
            first_run = json.loads(first.stdout)["runs"][0]
            self.assertEqual(first_run["scheduled_for"], "2026-08-01T01:00:00Z")
            self.assertEqual(first_run["next_run_at"], "2026-09-12T01:00:00Z")
            self.assertEqual(
                first_run["details"]["scheduling"]["missed_occurrences_skipped"],
                2,
            )
            self.assertEqual(json.loads(second.stdout)["status"], "no-due-work")
            self.assertEqual(json.loads(second.stdout)["runs"], [])
            self.assertEqual(
                json.loads(shown.stdout)["task"]["next_run_at"],
                "2026-09-12T01:00:00Z",
            )
            self.assertEqual(json.loads(history.stdout)["runs"], [first_run])

    def test_schedule_uses_zoneinfo_across_dst_and_defaults_to_singapore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            self.initialize(home)
            new_york_task = self.add_task(
                home,
                name="New York Task",
                timezone="America/New_York",
                next_run_at="2026-03-07T14:00:00Z",
            )
            default_payload = {
                "name": "Default timezone Task",
                "description": "Use the default task timezone.",
                "source_type": "direct",
                "source_ref": None,
                "source_summary": "Timezone default coverage.",
                "schedule": {"type": "daily", "time": "09:00"},
                "next_run_at": "2026-09-01T01:00:00Z",
                "action_mode": "notify",
                "handler": "manual_notification",
                "policy": {"message": "Review manually."},
            }
            default_add = self.run_cli(
                home,
                "--json",
                "task",
                "add",
                "--json",
                json.dumps(default_payload),
            )

            executed = self.run_cli(
                home,
                "run-due",
                "--now",
                "2026-03-07T14:00:00Z",
                "--json",
                extra_environment=self.fixture_environment(),
            )
            shown = self.run_cli(home, "task", "show", new_york_task["id"])

            self.assertEqual(default_add.returncode, 0, default_add.stderr)
            default_task = json.loads(default_add.stdout)["task"]
            self.assertEqual(default_task["timezone"], "Asia/Singapore")
            self.assertEqual(
                default_task["next_run_local"],
                "2026-09-01T09:00:00+08:00[Asia/Singapore]",
            )
            run = json.loads(executed.stdout)["runs"][0]
            self.assertEqual(run["task_id"], new_york_task["id"])
            self.assertEqual(run["next_run_at"], "2026-03-08T13:00:00Z")
            self.assertIn(
                "2026-03-08T09:00:00-04:00[America/New_York]",
                shown.stdout,
            )

    def test_handler_failure_is_observable_and_does_not_repeat_the_claimed_interval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            self.initialize(home)
            task = self.add_task(home, name="Failing scheduled Task")
            environment = self.fixture_environment(status="failed")

            failed = self.run_cli(
                home,
                "run-due",
                "--now",
                "2026-09-01T01:00:00Z",
                "--json",
                extra_environment=environment,
            )
            repeated = self.run_cli(
                home,
                "run-due",
                "--now",
                "2026-09-01T01:00:00Z",
                "--json",
                extra_environment=environment,
            )
            history = self.run_cli(home, "history", task["id"], "--json")

            self.assertEqual(failed.returncode, 1, failed.stderr)
            failed_run = json.loads(failed.stdout)["runs"][0]
            self.assertEqual(failed_run["status"], "failed")
            self.assertEqual(failed_run["trigger"], "scheduled")
            self.assertEqual(json.loads(repeated.stdout)["status"], "no-due-work")
            self.assertEqual(json.loads(history.stdout)["runs"], [failed_run])

    def test_competing_cli_processes_claim_and_execute_one_run_for_one_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            request_log = Path(directory) / "handler-requests.jsonl"
            self.initialize(home)
            task = self.add_task(home, name="Concurrent scheduled Task")
            environment = self.cli_environment(
                home,
                self.fixture_environment(request_log, delay_seconds="0.25"),
            )
            command = [
                str(CLI),
                "run-due",
                "--now",
                "2026-09-01T01:00:00Z",
                "--json",
            ]

            processes = [
                subprocess.Popen(
                    command,
                    cwd=PROJECT_ROOT,
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for _ in range(2)
            ]
            completed = [process.communicate(timeout=10) for process in processes]
            history = self.run_cli(home, "history", task["id"], "--json")

            for process, (_, stderr) in zip(processes, completed, strict=True):
                self.assertEqual(process.returncode, 0, stderr)
            payloads = [json.loads(stdout) for stdout, _ in completed]
            self.assertEqual(
                sorted(payload["status"] for payload in payloads),
                ["executed", "no-due-work"],
            )
            runs = json.loads(history.stdout)["runs"]
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["trigger"], "scheduled")
            requests = request_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(requests), 1)
            self.assertEqual(json.loads(requests[0])["run_id"], runs[0]["id"])

    def test_process_interruption_leaves_one_observable_claim_without_reexecution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            request_log = Path(directory) / "handler-requests.jsonl"
            self.initialize(home)
            task = self.add_task(home, name="Interrupted scheduled Task")
            environment = self.cli_environment(
                home,
                self.fixture_environment(request_log, delay_seconds="10"),
            )
            process = subprocess.Popen(
                [
                    str(CLI),
                    "run-due",
                    "--now",
                    "2026-09-01T01:00:00Z",
                    "--json",
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            database_file = home / "var" / "data" / "runtasks.sqlite3"

            deadline = time.monotonic() + 5
            observed_status: str | None = None
            while time.monotonic() < deadline:
                if request_log.exists():
                    with sqlite3.connect(database_file) as connection:
                        row = connection.execute(
                            "SELECT status FROM runs WHERE task_id = ?",
                            (task["id"],),
                        ).fetchone()
                    if row is not None:
                        observed_status = str(row[0])
                        break
                time.sleep(0.02)
            self.assertEqual(observed_status, "running")
            process.kill()
            process.communicate(timeout=5)

            repeated = self.run_cli(
                home,
                "run-due",
                "--now",
                "2026-09-01T01:00:00Z",
                "--json",
                extra_environment=self.fixture_environment(request_log),
            )
            history = self.run_cli(home, "history", task["id"], "--json")

            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(json.loads(repeated.stdout)["status"], "no-due-work")
            runs = json.loads(history.stdout)["runs"]
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["status"], "running")
            self.assertEqual(runs[0]["scheduled_for"], "2026-09-01T01:00:00Z")
            self.assertEqual(len(request_log.read_text(encoding="utf-8").splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
