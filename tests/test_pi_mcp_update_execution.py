from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from typing import Any, cast

from runtasks.decisions import get_decision
from runtasks.pi_mcp_execution import (
    ExecutionNotificationAdapter,
    PiMcpExecutionAdapterError,
    PiMcpExecutionAdapters,
    execute_approved_pi_mcp_runs,
)
from runtasks.pi_mcp_execution_adapters import (
    FreshPiValidationAdapter,
    PiPackageAdapter,
    SystemdHealthAdapter,
    build_pi_mcp_execution_adapters,
)
from runtasks.redaction import Redactor
from runtasks.runs import get_run
from runtasks.pi_mcp_release_adapters import ProcessResult
from tests.cli_test_support import CLI, PROJECT_ROOT, run_cli


class PiMcpUpdateExecutionCliTests(unittest.TestCase):
    def initialize(self, home: Path) -> None:
        result = run_cli(home, "init")
        self.assertEqual(result.returncode, 0, result.stderr)

    def add_task(self, home: Path) -> dict[str, Any]:
        payload = {
            "name": "Pi MCP adapter update",
            "description": "Install an exact approved adapter release.",
            "source_type": "direct",
            "source_ref": None,
            "source_summary": "Important adapter updates require exact approval.",
            "schedule": {"type": "interval-days", "days": 14, "time": "09:00"},
            "timezone": "Asia/Singapore",
            "next_run_at": "2099-09-01T01:00:00Z",
            "action_mode": "approved-procedure",
            "handler": "pi_mcp_adapter",
            "policy": {
                "approval_required": True,
                "important_conditions": ["security", "OAuth safety"],
            },
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

    def create_approved_decision(
        self,
        home: Path,
        task_id: str,
    ) -> dict[str, Any]:
        release_outcome = {
            "status": "success",
            "summary": "Important Pi MCP adapter release requires approval.",
            "details": {
                "contract": "pi-mcp-release-check/v1",
                "outcome": "decision-required",
                "installed_version": "2.26.1",
                "available_version": "2.27.0",
                "assessment": {
                    "importance": "important",
                    "category": "credential-oauth",
                    "reason": "OAuth credential handling affects this installation.",
                    "recommendation": "Update after approval.",
                    "confidence": "high",
                },
                "evidence": [
                    {
                        "version": "2.27.0",
                        "sources": [
                            {
                                "source": "changelog",
                                "title": "2.27.0",
                                "body": "OAuth credential handling correction.",
                                "reference": "https://example.invalid/releases/2.27.0",
                            }
                        ],
                    }
                ],
                "source_failures": [],
                "source_references": [],
            },
        }
        checked = run_cli(
            home,
            "run",
            task_id,
            "--json",
            extra_environment={
                "RUNTASKS_EXTERNAL_ADAPTER": "fixture",
                "RUNTASKS_FIXTURE_EXTERNAL_OUTCOME": json.dumps(release_outcome),
            },
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)
        decision = json.loads(run_cli(home, "decisions", "--json").stdout)[
            "decisions"
        ][0]
        approved = run_cli(
            home,
            "decision",
            "approve",
            str(decision["id"]),
            "--json",
        )
        self.assertEqual(approved.returncode, 0, approved.stderr)
        return cast(dict[str, Any], json.loads(approved.stdout)["decision"])

    def execution_environment(
        self,
        event_log: Path,
        fixture: dict[str, object] | None = None,
    ) -> dict[str, str]:
        execution_fixture = (
            {
                "installed_versions": ["2.26.1", "2.27.0"],
                "install": {"status": "success"},
                "restart": {"status": "success"},
                "health": {"status": "success", "result": "healthy"},
                "pi_validation": {
                    "status": "success",
                    "result": "MCP_ADAPTER_OK",
                },
                "notification": {"status": "success"},
            }
            if fixture is None
            else fixture
        )
        return {
            "RUNTASKS_PI_MCP_EXECUTION_ADAPTER": "fixture",
            "RUNTASKS_FIXTURE_PI_MCP_EXECUTION": json.dumps(execution_fixture),
            "RUNTASKS_FIXTURE_PI_MCP_EXECUTION_LOG": str(event_log),
        }

    def test_approved_exact_plan_executes_once_in_order_and_completes_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            event_log = Path(directory) / "execution-events.jsonl"
            self.initialize(home)
            task = self.add_task(home)
            approved = self.create_approved_decision(home, str(task["id"]))

            executed = run_cli(
                home,
                "run-due",
                "--now",
                "2026-09-01T01:00:00Z",
                "--json",
                extra_environment=self.execution_environment(event_log),
            )
            repeated = run_cli(
                home,
                "run-due",
                "--now",
                "2026-09-01T01:00:00Z",
                "--json",
                extra_environment=self.execution_environment(event_log),
            )
            shown = run_cli(
                home,
                "decision",
                "show",
                str(approved["id"]),
                "--json",
            )
            searched = run_cli(
                home,
                "search",
                "MCP_ADAPTER_OK rollback not required",
                "--json",
            )

            self.assertEqual(executed.returncode, 0, executed.stderr)
            runs = json.loads(executed.stdout)["runs"]
            self.assertEqual(len(runs), 1)
            run = runs[0]
            self.assertEqual(run["id"], approved["approval_run_id"])
            self.assertEqual(run["trigger"], "approval")
            self.assertEqual(run["status"], "success")
            self.assertEqual(run["details"]["old_version"], "2.26.1")
            self.assertEqual(run["details"]["new_version"], "2.27.0")
            self.assertEqual(run["details"]["pi_web_health"], "healthy")
            self.assertEqual(run["details"]["mcp_validation"], "MCP_ADAPTER_OK")
            self.assertEqual(
                run["details"]["rollback"],
                {"required": False, "status": "not-required"},
            )
            self.assertTrue(run["details"]["mutation_performed"])

            events = [
                json.loads(line)
                for line in event_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [event["operation"] for event in events],
                [
                    "package.installed-version",
                    "package.install-exact",
                    "package.installed-version",
                    "service.restart",
                    "health.check",
                    "pi.validate-mcp",
                    "notification.send",
                ],
            )
            self.assertEqual(events[1]["version"], "2.27.0")
            self.assertEqual(events[3]["service"], "pi-web.service")
            self.assertEqual(events[5]["expected_result"], "MCP_ADAPTER_OK")
            notification = events[6]["text"]
            self.assertIn("Updated: 2.26.1 → 2.27.0", notification)
            self.assertIn("Pi Web: Healthy", notification)
            self.assertIn("Validation: MCP_ADAPTER_OK", notification)
            self.assertIn("Rollback: Not required", notification)
            self.assertIn("reopen", notification.lower())

            self.assertEqual(json.loads(repeated.stdout)["runs"], [])
            self.assertEqual(
                len(event_log.read_text(encoding="utf-8").splitlines()),
                7,
            )
            completed = json.loads(shown.stdout)["decision"]
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["approval_run_id"], run["id"])
            results = json.loads(searched.stdout)["results"]
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["type"], "run")
            self.assertEqual(results[0]["run"]["id"], run["id"])

    def test_pending_plan_cannot_enter_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            event_log = Path(directory) / "execution-events.jsonl"
            self.initialize(home)
            task = self.add_task(home)
            release_outcome = {
                "status": "decision-required",
                "summary": "Approval is required.",
                "details": {"mutation_performed": False},
                "decision": {
                    "plan": {
                        "handler": "pi_mcp_adapter",
                        "operation": "install-exact-version",
                        "parameters": {
                            "installed_version": "2.26.1",
                            "target_version": "2.27.0",
                        },
                        "validation": {"expected_result": "MCP_ADAPTER_OK"},
                        "rollback": {"target_version": "2.26.1"},
                    },
                    "reason": "Approval is required.",
                    "validation_summary": "Validate MCP_ADAPTER_OK.",
                    "rollback_summary": "Restore 2.26.1.",
                },
            }
            checked = run_cli(
                home,
                "run",
                str(task["id"]),
                "--json",
                extra_environment={
                    "RUNTASKS_FIXTURE_HANDLER_OUTCOME": json.dumps(release_outcome)
                },
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)

            executed = run_cli(
                home,
                "run-due",
                "--now",
                "2026-09-01T01:00:00Z",
                "--json",
                extra_environment=self.execution_environment(event_log),
            )

            self.assertEqual(executed.returncode, 0, executed.stderr)
            self.assertEqual(json.loads(executed.stdout)["runs"], [])
            self.assertFalse(event_log.exists())

    def test_non_pi_approval_work_is_left_for_its_registered_executor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            event_log = Path(directory) / "execution-events.jsonl"
            self.initialize(home)
            task = self.add_task(home)
            approved = self.create_approved_decision(home, str(task["id"]))
            changed_handler = run_cli(
                home,
                "task",
                "update",
                str(task["id"]),
                "--json",
                json.dumps(
                    {
                        "action_mode": "notify",
                        "handler": "manual_notification",
                    }
                ),
                "--output-json",
            )
            self.assertEqual(
                changed_handler.returncode,
                0,
                changed_handler.stderr,
            )

            executed = run_cli(
                home,
                "run-due",
                "--now",
                "2026-09-01T01:00:00Z",
                "--json",
                extra_environment=self.execution_environment(event_log),
            )

            self.assertEqual(executed.returncode, 0, executed.stderr)
            self.assertEqual(json.loads(executed.stdout)["runs"], [])
            self.assertFalse(event_log.exists())
            approval_run = get_run(
                home / "var" / "data" / "runtasks.sqlite3",
                str(approved["approval_run_id"]),
            )
            self.assertEqual(approval_run.status, "claimed")

    def test_old_version_precondition_failure_stops_before_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            event_log = Path(directory) / "execution-events.jsonl"
            self.initialize(home)
            task = self.add_task(home)
            approved = self.create_approved_decision(home, str(task["id"]))
            environment = self.execution_environment(
                event_log,
                {
                    "installed_versions": ["2.26.9"],
                    "install": {"status": "success"},
                    "restart": {"status": "success"},
                    "health": {"status": "success", "result": "healthy"},
                    "pi_validation": {
                        "status": "success",
                        "result": "MCP_ADAPTER_OK",
                    },
                    "notification": {"status": "success"},
                },
            )

            executed = run_cli(
                home,
                "run-due",
                "--now",
                "2026-09-01T01:00:00Z",
                "--json",
                extra_environment=environment,
            )
            repeated = run_cli(
                home,
                "run-due",
                "--now",
                "2026-09-01T01:00:00Z",
                "--json",
                extra_environment=environment,
            )

            self.assertEqual(executed.returncode, 1, executed.stderr)
            run = json.loads(executed.stdout)["runs"][0]
            self.assertEqual(run["status"], "failed")
            self.assertFalse(run["details"]["mutation_performed"])
            self.assertIn("approved old version", run["summary"])
            events = [
                json.loads(line)
                for line in event_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [event["operation"] for event in events],
                ["package.installed-version"],
            )
            self.assertEqual(json.loads(repeated.stdout)["runs"], [])
            decision = json.loads(
                run_cli(
                    home,
                    "decision",
                    "show",
                    str(approved["id"]),
                    "--json",
                ).stdout
            )["decision"]
            self.assertEqual(decision["status"], "failed")

    def test_interrupted_notification_claim_is_retried_without_reinstall(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            event_log = Path(directory) / "execution-events.jsonl"
            self.initialize(home)
            task = self.add_task(home)
            approved = self.create_approved_decision(home, str(task["id"]))
            environment = self.execution_environment(event_log)
            first = run_cli(
                home,
                "run-due",
                "--now",
                "2026-09-01T01:00:00Z",
                "--json",
                extra_environment=environment,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            database_path = home / "var" / "data" / "runtasks.sqlite3"
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    """
                    UPDATE decision_execution_outcomes SET
                        notification_status = 'sending',
                        notification_claimed_at = ?,
                        notification_attempts = 0,
                        notification_last_attempt_at = NULL,
                        notification_last_error = NULL,
                        notification_delivered_at = NULL
                    WHERE decision_id = ?
                    """,
                    ("2026-09-01T01:00:00Z", approved["id"]),
                )

            restarted = run_cli(
                home,
                "run-due",
                "--now",
                "2026-09-01T01:10:00Z",
                "--json",
                extra_environment=environment,
            )

            self.assertEqual(restarted.returncode, 0, restarted.stderr)
            self.assertEqual(json.loads(restarted.stdout)["runs"], [])
            events = [
                json.loads(line)
                for line in event_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [event["operation"] for event in events].count(
                    "package.install-exact"
                ),
                1,
            )
            self.assertEqual(
                [event["operation"] for event in events].count(
                    "notification.send"
                ),
                2,
            )

    def test_target_metadata_mismatch_fails_before_service_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            event_log = Path(directory) / "execution-events.jsonl"
            self.initialize(home)
            task = self.add_task(home)
            self.create_approved_decision(home, str(task["id"]))

            executed = run_cli(
                home,
                "run-due",
                "--now",
                "2026-09-01T01:00:00Z",
                "--json",
                extra_environment=self.execution_environment(
                    event_log,
                    {
                        "installed_versions": ["2.26.1", "2.27.1"],
                        "install": {"status": "success"},
                        "restart": {"status": "success"},
                        "health": {"status": "success", "result": "healthy"},
                        "pi_validation": {
                            "status": "success",
                            "result": "MCP_ADAPTER_OK",
                        },
                        "notification": {"status": "success"},
                    },
                ),
            )

            self.assertEqual(executed.returncode, 1, executed.stderr)
            run = json.loads(executed.stdout)["runs"][0]
            self.assertEqual(run["status"], "failed")
            self.assertTrue(run["details"]["mutation_performed"])
            self.assertIn("package metadata", run["summary"])
            events = [
                json.loads(line)
                for line in event_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [event["operation"] for event in events],
                [
                    "package.installed-version",
                    "package.install-exact",
                    "package.installed-version",
                ],
            )

    def test_competing_one_shot_processes_install_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            event_log = Path(directory) / "execution-events.jsonl"
            self.initialize(home)
            task = self.add_task(home)
            self.create_approved_decision(home, str(task["id"]))
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("RUNTASKS_")
            }
            environment.update(self.execution_environment(event_log))
            environment["HOME"] = str(home.parent)
            environment["RUNTASKS_HOME"] = str(home)
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
            completed = [process.communicate(timeout=15) for process in processes]

            for process, (_, stderr) in zip(processes, completed, strict=True):
                self.assertEqual(process.returncode, 0, stderr)
            payloads = [json.loads(stdout) for stdout, _ in completed]
            self.assertEqual(sum(len(payload["runs"]) for payload in payloads), 1)
            events = [
                json.loads(line)
                for line in event_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [event["operation"] for event in events].count(
                    "package.install-exact"
                ),
                1,
            )

    def test_invalid_oldest_approval_is_failed_without_blocking_valid_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            event_log = Path(directory) / "execution-events.jsonl"
            self.initialize(home)
            first_task = self.add_task(home)
            first_decision = self.create_approved_decision(
                home,
                str(first_task["id"]),
            )
            removed = run_cli(home, "task", "remove", str(first_task["id"]))
            self.assertEqual(removed.returncode, 0, removed.stderr)
            second_task = self.add_task(home)
            second_decision = self.create_approved_decision(
                home,
                str(second_task["id"]),
            )

            executed = run_cli(
                home,
                "run-due",
                "--now",
                "2026-09-01T01:00:00Z",
                "--json",
                extra_environment=self.execution_environment(event_log),
            )

            self.assertEqual(executed.returncode, 1, executed.stderr)
            runs = json.loads(executed.stdout)["runs"]
            self.assertEqual(
                [(run["id"], run["status"]) for run in runs],
                [
                    (first_decision["approval_run_id"], "failed"),
                    (second_decision["approval_run_id"], "success"),
                ],
            )
            events = [
                json.loads(line)
                for line in event_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [event["operation"] for event in events].count(
                    "package.install-exact"
                ),
                1,
            )
            shown_first = json.loads(
                run_cli(
                    home,
                    "decision",
                    "show",
                    str(first_decision["id"]),
                    "--json",
                ).stdout
            )["decision"]
            shown_second = json.loads(
                run_cli(
                    home,
                    "decision",
                    "show",
                    str(second_decision["id"]),
                    "--json",
                ).stdout
            )["decision"]
            self.assertEqual(shown_first["status"], "failed")
            self.assertEqual(shown_second["status"], "completed")

    def test_plan_hash_mismatch_is_recorded_without_external_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            event_log = Path(directory) / "execution-events.jsonl"
            self.initialize(home)
            task = self.add_task(home)
            approved = self.create_approved_decision(home, str(task["id"]))
            database_path = home / "var" / "data" / "runtasks.sqlite3"
            with sqlite3.connect(database_path) as connection:
                connection.execute("DROP TRIGGER decisions_immutable_plan")
                connection.execute("DROP TRIGGER decisions_resolved_immutable")
                plan = dict(approved["plan"])
                parameters = dict(cast(dict[str, object], plan["parameters"]))
                parameters["target_version"] = "2.28.0"
                plan["parameters"] = parameters
                connection.execute(
                    "UPDATE decisions SET plan_json = ? WHERE id = ?",
                    (
                        json.dumps(plan, separators=(",", ":"), sort_keys=True),
                        approved["id"],
                    ),
                )

            executed = run_cli(
                home,
                "run-due",
                "--now",
                "2026-09-01T01:00:00Z",
                "--json",
                extra_environment=self.execution_environment(event_log),
            )

            self.assertEqual(executed.returncode, 1, executed.stderr)
            run = json.loads(executed.stdout)["runs"][0]
            self.assertEqual(run["status"], "failed")
            self.assertFalse(run["details"]["mutation_performed"])
            self.assertIn("plan hash", run["summary"])
            self.assertEqual(
                run["details"]["steps"][0]["name"],
                "plan-verification",
            )
            self.assertFalse(event_log.exists())

    def test_success_is_durable_before_notification_and_delivery_retries_without_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            event_log = Path(directory) / "execution-events.jsonl"
            self.initialize(home)
            task = self.add_task(home)
            approved = self.create_approved_decision(home, str(task["id"]))
            database_path = home / "var" / "data" / "runtasks.sqlite3"
            settings = self.execution_environment(event_log)
            base_adapters = build_pi_mcp_execution_adapters(settings, Redactor())
            notification = ObservingNotificationAdapter(
                database_path,
                str(approved["id"]),
                str(approved["approval_run_id"]),
                failures=1,
            )
            adapters = PiMcpExecutionAdapters(
                package=base_adapters.package,
                service=base_adapters.service,
                health=base_adapters.health,
                mcp_validation=base_adapters.mcp_validation,
                notification=notification,
            )

            first_runs = execute_approved_pi_mcp_runs(
                database_path,
                adapters,
                Redactor(),
            )
            second_runs = execute_approved_pi_mcp_runs(
                database_path,
                adapters,
                Redactor(),
            )

            self.assertEqual(len(first_runs), 1)
            self.assertEqual(first_runs[0].status, "success")
            self.assertEqual(second_runs, ())
            self.assertEqual(
                notification.observed_states,
                [("completed", "success"), ("completed", "success")],
            )
            self.assertEqual(notification.attempts, 2)
            events = [
                json.loads(line)
                for line in event_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [event["operation"] for event in events].count(
                    "package.install-exact"
                ),
                1,
            )
            shown = get_run(database_path, str(approved["approval_run_id"]))
            notification_details = cast(
                dict[str, object],
                shown.details["notification"],
            )
            self.assertEqual(notification_details["status"], "delivered")

    def test_process_restart_does_not_reenter_a_running_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            event_log = Path(directory) / "execution-events.jsonl"
            self.initialize(home)
            task = self.add_task(home)
            approved = self.create_approved_decision(home, str(task["id"]))
            database_path = home / "var" / "data" / "runtasks.sqlite3"
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    """
                    UPDATE runs SET status = 'running', started_at = ?
                    WHERE id = ? AND status = 'claimed'
                    """,
                    ("2026-09-01T01:00:00Z", approved["approval_run_id"]),
                )

            restarted = run_cli(
                home,
                "run-due",
                "--now",
                "2026-09-01T01:01:00Z",
                "--json",
                extra_environment=self.execution_environment(event_log),
            )

            self.assertEqual(restarted.returncode, 0, restarted.stderr)
            self.assertEqual(json.loads(restarted.stdout)["runs"], [])
            self.assertFalse(event_log.exists())
            self.assertEqual(
                get_run(database_path, str(approved["approval_run_id"])).status,
                "running",
            )


class ObservingNotificationAdapter(ExecutionNotificationAdapter):
    def __init__(
        self,
        database_path: Path,
        decision_id: str,
        run_id: str,
        *,
        failures: int,
    ) -> None:
        self._database_path = database_path
        self._decision_id = decision_id
        self._run_id = run_id
        self._failures = failures
        self.attempts = 0
        self.observed_states: list[tuple[str, str]] = []

    def send(self, text: str) -> None:
        self.attempts += 1
        self.assert_success_message(text)
        self.observed_states.append(
            (
                get_decision(self._database_path, self._decision_id).status,
                get_run(self._database_path, self._run_id).status,
            )
        )
        if self.attempts <= self._failures:
            raise RuntimeError("private notification failure")

    @staticmethod
    def assert_success_message(text: str) -> None:
        if "RunTasks update completed successfully" not in text:
            raise AssertionError("success notification text is invalid")


class RecordingProcessRunner:
    def __init__(self, results: list[ProcessResult | Exception]) -> None:
        self.results = list(results)
        self.calls: list[tuple[tuple[str, ...], float, Path | None]] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        cwd: Path | None = None,
    ) -> ProcessResult:
        self.calls.append((argv, timeout_seconds, cwd))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class PiMcpExecutionAdapterContractTests(unittest.TestCase):
    def test_health_requires_one_exact_active_line(self) -> None:
        ambiguous_outputs = (
            ProcessResult(0, "active\nactivating\n", ""),
            ProcessResult(0, " active\n", ""),
            ProcessResult(0, "active\n", "warning"),
            ProcessResult(3, "inactive\n", ""),
        )
        for result in ambiguous_outputs:
            with self.subTest(result=result):
                adapter = SystemdHealthAdapter(
                    process_runner=RecordingProcessRunner([result])
                )
                with self.assertRaises(PiMcpExecutionAdapterError):
                    adapter.check("pi-web.service")

    def test_fresh_pi_validation_uses_a_new_mcp_only_process(self) -> None:
        runner = RecordingProcessRunner(
            [ProcessResult(0, "MCP_ADAPTER_OK\n", "")]
        )
        adapter = FreshPiValidationAdapter(process_runner=runner)

        self.assertEqual(adapter.validate_mcp("MCP_ADAPTER_OK"), "MCP_ADAPTER_OK")
        self.assertEqual(
            runner.calls[0][0],
            (
                "pi",
                "--no-session",
                "--tools",
                "mcp",
                "-p",
                "Call the mcp tool with an empty object. If successful, reply exactly MCP_ADAPTER_OK.",
            ),
        )

    def test_fresh_pi_validation_rejects_surrounding_output_and_process_failures(self) -> None:
        invalid_results: tuple[ProcessResult | Exception, ...] = (
            ProcessResult(0, "MCP_ADAPTER_OK\nextra\n", ""),
            ProcessResult(0, "MCP_ADAPTER_OK\n", "warning"),
            ProcessResult(1, "MCP_ADAPTER_OK\n", ""),
            TimeoutError("private timeout detail"),
        )
        for result in invalid_results:
            with self.subTest(result=result):
                adapter = FreshPiValidationAdapter(
                    process_runner=RecordingProcessRunner([result])
                )
                with self.assertRaises(PiMcpExecutionAdapterError):
                    adapter.validate_mcp("MCP_ADAPTER_OK")

    def test_package_adapter_installs_only_an_exact_pin_and_rejects_malformed_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent_dir = Path(directory) / "agent"
            package_path = (
                agent_dir
                / "npm"
                / "node_modules"
                / "pi-mcp-adapter"
                / "package.json"
            )
            package_path.parent.mkdir(parents=True)
            runner = RecordingProcessRunner([ProcessResult(0, "installed\n", "")])
            adapter = PiPackageAdapter(
                agent_dir=agent_dir,
                process_runner=runner,
            )

            package_path.write_text(
                json.dumps({"name": "pi-mcp-adapter", "version": "2.27.0"}),
                encoding="utf-8",
            )
            self.assertEqual(adapter.installed_version(), "2.27.0")
            adapter.install_exact("2.27.0")
            self.assertEqual(
                runner.calls[0][0],
                ("pi", "install", "npm:pi-mcp-adapter@2.27.0"),
            )

            for metadata in (
                "not-json",
                json.dumps({"name": "other-package", "version": "2.27.0"}),
                json.dumps({"name": "pi-mcp-adapter", "version": "latest"}),
            ):
                with self.subTest(metadata=metadata):
                    package_path.write_text(metadata, encoding="utf-8")
                    with self.assertRaises(PiMcpExecutionAdapterError):
                        adapter.installed_version()
            with self.assertRaises(PiMcpExecutionAdapterError):
                adapter.install_exact("latest")
            self.assertEqual(len(runner.calls), 1)


if __name__ == "__main__":
    unittest.main()
