from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
            self.assertEqual(
                {result["type"] for result in results},
                {"decision", "run"},
            )

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
            environment.update(
                {
                    "RUNTASKS_EXTERNAL_ADAPTER": "fixture",
                    "RUNTASKS_FIXTURE_EXTERNAL_OUTCOME": json.dumps(
                        {
                            "status": "success",
                            "summary": "Fresh drift check found the current pin.",
                            "details": {
                                "contract": "pi-mcp-release-check/v1",
                                "outcome": "no-change",
                                "installed_version": "2.26.9",
                                "available_version": "2.26.9",
                                "assessment": None,
                                "evidence": [],
                                "source_failures": [],
                            },
                        }
                    ),
                }
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
            runs = json.loads(executed.stdout)["runs"]
            self.assertEqual(
                [run["status"] for run in runs],
                ["failed", "no-change"],
            )
            run = runs[0]
            self.assertFalse(run["details"]["mutation_performed"])
            self.assertEqual(run["details"]["outcome"], "stale-plan")
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
            self.assertEqual(decision["status"], "superseded")
            self.assertEqual(
                decision["execution"]["details"]["outcome"],
                "stale-plan",
            )

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
                        "installed_versions": ["2.26.1", "2.27.1", "2.26.1"],
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
            self.assertEqual(run["status"], "rolled-back")
            self.assertTrue(run["details"]["mutation_performed"])
            self.assertEqual(
                run["details"]["failed_step"],
                "target-metadata-verification",
            )
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
                    "package.install-exact",
                    "package.installed-version",
                    "service.restart",
                    "health.check",
                    "pi.validate-mcp",
                    "notification.send",
                ],
            )

    def test_validation_failure_rolls_back_exact_pin_and_sends_redacted_urgent_notification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            event_log = Path(directory) / "execution-events.jsonl"
            self.initialize(home)
            task = self.add_task(home)
            approved = self.create_approved_decision(home, str(task["id"]))
            fake_credential = "ghp_abcdefghijklmnopqrstuvwxyz123456"

            executed = run_cli(
                home,
                "run-due",
                "--now",
                "2026-09-01T01:00:00Z",
                "--json",
                extra_environment=self.execution_environment(
                    event_log,
                    {
                        "installed_versions": ["2.26.1", "2.27.0", "2.26.1"],
                        "install": [
                            {"status": "success"},
                            {"status": "success"},
                        ],
                        "restart": [
                            {"status": "success"},
                            {"status": "success"},
                        ],
                        "health": [
                            {"status": "success", "result": "healthy"},
                            {"status": "success", "result": "healthy"},
                        ],
                        "pi_validation": [
                            {
                                "status": "failed",
                                "result": "invalid",
                                "error": f"token={fake_credential}",
                            },
                            {
                                "status": "success",
                                "result": "MCP_ADAPTER_OK",
                            },
                        ],
                        "notification": {"status": "success"},
                    },
                ),
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
                "rollback verified restored",
                "--json",
            )

            self.assertEqual(executed.returncode, 1, executed.stderr)
            run = json.loads(executed.stdout)["runs"][0]
            self.assertEqual(run["status"], "rolled-back")
            self.assertEqual(run["details"]["failed_step"], "mcp-validation")
            self.assertEqual(
                run["details"]["rollback"],
                {
                    "attempted": True,
                    "mcp_validation": "MCP_ADAPTER_OK",
                    "pi_web_health": "healthy",
                    "required": True,
                    "restored_version": "2.26.1",
                    "status": "verified",
                    "target_version": "2.26.1",
                },
            )
            serialized_run = json.dumps(run, sort_keys=True)
            self.assertNotIn(fake_credential, serialized_run)
            self.assertIn("[REDACTED]", serialized_run)

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
                    "package.install-exact",
                    "package.installed-version",
                    "service.restart",
                    "health.check",
                    "pi.validate-mcp",
                    "notification.send",
                ],
            )
            self.assertEqual(events[1]["version"], "2.27.0")
            self.assertEqual(events[6]["version"], "2.26.1")
            notification = events[-1]["text"]
            self.assertIn("URGENT", notification)
            self.assertIn("Attempted: 2.26.1 → 2.27.0", notification)
            self.assertIn("Failed step: mcp-validation", notification)
            self.assertIn("Rollback: Restored exact 2.26.1", notification)
            self.assertIn("Validation: MCP_ADAPTER_OK", notification)
            self.assertNotIn(fake_credential, notification)

            decision = json.loads(shown.stdout)["decision"]
            self.assertEqual(decision["status"], "rolled-back")
            self.assertEqual(decision["execution"]["status"], "rolled-back")
            self.assertEqual(
                decision["execution"]["details"]["rollback"]["status"],
                "verified",
            )
            result_types = {
                result["type"]
                for result in json.loads(searched.stdout)["results"]
            }
            self.assertEqual(result_types, {"decision", "run"})

    def test_failures_before_mutation_skip_rollback_and_post_mutation_steps_recover(self) -> None:
        scenarios: tuple[
            tuple[str, dict[str, object], str, int, str], ...
        ] = (
            (
                "old-version-precondition",
                {
                    "installed_versions": [],
                    "install": {"status": "success"},
                    "restart": {"status": "success"},
                    "health": {"status": "success", "result": "healthy"},
                    "pi_validation": {
                        "status": "success",
                        "result": "MCP_ADAPTER_OK",
                    },
                    "notification": {"status": "success"},
                },
                "failed",
                0,
                "not-required",
            ),
            (
                "install-exact-version",
                {
                    "installed_versions": ["2.26.1", "2.26.1"],
                    "install": [
                        {"status": "failed"},
                        {"status": "success"},
                    ],
                    "restart": {"status": "success"},
                    "health": {"status": "success", "result": "healthy"},
                    "pi_validation": {
                        "status": "success",
                        "result": "MCP_ADAPTER_OK",
                    },
                    "notification": {"status": "success"},
                },
                "failed",
                1,
                "ambiguous",
            ),
            (
                "pi-web-restart",
                {
                    "installed_versions": ["2.26.1", "2.27.0", "2.26.1"],
                    "install": [{"status": "success"}, {"status": "success"}],
                    "restart": [{"status": "failed"}, {"status": "success"}],
                    "health": {"status": "success", "result": "healthy"},
                    "pi_validation": {
                        "status": "success",
                        "result": "MCP_ADAPTER_OK",
                    },
                    "notification": {"status": "success"},
                },
                "rolled-back",
                2,
                "verified",
            ),
            (
                "pi-web-health",
                {
                    "installed_versions": ["2.26.1", "2.27.0", "2.26.1"],
                    "install": [{"status": "success"}, {"status": "success"}],
                    "restart": [{"status": "success"}, {"status": "success"}],
                    "health": [
                        {"status": "failed", "result": "unhealthy"},
                        {"status": "success", "result": "healthy"},
                    ],
                    "pi_validation": {
                        "status": "success",
                        "result": "MCP_ADAPTER_OK",
                    },
                    "notification": {"status": "success"},
                },
                "rolled-back",
                2,
                "verified",
            ),
        )
        for (
            failed_step,
            fixture,
            expected_status,
            expected_installs,
            expected_rollback,
        ) in scenarios:
            with self.subTest(failed_step=failed_step):
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
                            fixture,
                        ),
                    )

                    run = json.loads(executed.stdout)["runs"][0]
                    self.assertEqual(run["status"], expected_status)
                    self.assertEqual(run["details"]["failed_step"], failed_step)
                    events = [
                        json.loads(line)
                        for line in event_log.read_text(encoding="utf-8").splitlines()
                    ]
                    operations = [event["operation"] for event in events]
                    self.assertEqual(
                        operations.count("package.install-exact"),
                        expected_installs,
                    )
                    self.assertEqual(
                        run["details"]["rollback"]["status"],
                        expected_rollback,
                    )
                    if expected_rollback == "not-required":
                        self.assertFalse(run["details"]["mutation_performed"])
                        self.assertNotIn("service.restart", operations)
                    elif expected_rollback == "ambiguous":
                        self.assertEqual(executed.returncode, 1, executed.stderr)
                        self.assertNotIn("service.restart", operations)
                    else:
                        self.assertEqual(executed.returncode, 1, executed.stderr)

    def test_each_rollback_step_failure_is_critical_redacted_and_notified(self) -> None:
        fake_credential = "ghp_rollbackcredential1234567890"
        scenarios = (
            (
                "rollback-install-exact-version",
                {
                    "installed_versions": ["2.26.1", "2.27.0"],
                    "install": [
                        {"status": "success"},
                        {"status": "failed", "error": f"token={fake_credential}"},
                    ],
                    "restart": {"status": "success"},
                    "health": {"status": "success", "result": "healthy"},
                    "pi_validation": {"status": "failed", "result": "invalid"},
                    "notification": {"status": "success"},
                },
            ),
            (
                "rollback-metadata-verification",
                {
                    "installed_versions": ["2.26.1", "2.27.0", "2.26.2"],
                    "install": [{"status": "success"}, {"status": "success"}],
                    "restart": {"status": "success"},
                    "health": {"status": "success", "result": "healthy"},
                    "pi_validation": {"status": "failed", "result": "invalid"},
                    "notification": {"status": "success"},
                },
            ),
            (
                "rollback-pi-web-restart",
                {
                    "installed_versions": ["2.26.1", "2.27.0", "2.26.1"],
                    "install": [{"status": "success"}, {"status": "success"}],
                    "restart": [
                        {"status": "success"},
                        {"status": "failed", "error": f"token={fake_credential}"},
                    ],
                    "health": {"status": "success", "result": "healthy"},
                    "pi_validation": {"status": "failed", "result": "invalid"},
                    "notification": {"status": "success"},
                },
            ),
            (
                "rollback-pi-web-health",
                {
                    "installed_versions": ["2.26.1", "2.27.0", "2.26.1"],
                    "install": [{"status": "success"}, {"status": "success"}],
                    "restart": [{"status": "success"}, {"status": "success"}],
                    "health": [
                        {"status": "success", "result": "healthy"},
                        {
                            "status": "failed",
                            "result": "unhealthy",
                            "error": f"token={fake_credential}",
                        },
                    ],
                    "pi_validation": {"status": "failed", "result": "invalid"},
                    "notification": {"status": "success"},
                },
            ),
            (
                "rollback-mcp-validation",
                {
                    "installed_versions": ["2.26.1", "2.27.0", "2.26.1"],
                    "install": [{"status": "success"}, {"status": "success"}],
                    "restart": [{"status": "success"}, {"status": "success"}],
                    "health": [
                        {"status": "success", "result": "healthy"},
                        {"status": "success", "result": "healthy"},
                    ],
                    "pi_validation": [
                        {"status": "failed", "result": "invalid"},
                        {
                            "status": "failed",
                            "result": "invalid",
                            "error": f"token={fake_credential}",
                        },
                    ],
                    "notification": {"status": "success"},
                },
            ),
        )
        for rollback_step, fixture in scenarios:
            with self.subTest(rollback_step=rollback_step):
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
                        extra_environment=self.execution_environment(
                            event_log,
                            fixture,
                        ),
                    )

                    self.assertEqual(executed.returncode, 1, executed.stderr)
                    run = json.loads(executed.stdout)["runs"][0]
                    self.assertEqual(run["status"], "failed")
                    self.assertEqual(
                        run["details"]["outcome"],
                        "critical-rollback-failure",
                    )
                    failed_steps = [
                        step["name"]
                        for step in run["details"]["steps"]
                        if step["status"] == "failed"
                    ]
                    self.assertIn(rollback_step, failed_steps)
                    self.assertNotIn(fake_credential, json.dumps(run))
                    events = [
                        json.loads(line)
                        for line in event_log.read_text(encoding="utf-8").splitlines()
                    ]
                    notification = events[-1]["text"]
                    self.assertIn("URGENT", notification)
                    self.assertIn("CRITICAL", notification)
                    self.assertNotIn(fake_credential, notification)
                    decision = json.loads(
                        run_cli(
                            home,
                            "decision",
                            "show",
                            str(approved["id"]),
                            "--json",
                        ).stdout
                    )["decision"]
                    self.assertEqual(decision["status"], "rollback-failed")
                    self.assertEqual(
                        decision["execution"]["notification_delivery"]["status"],
                        "delivered",
                    )

    def test_interrupted_rollback_is_reconciled_without_duplicate_installation(self) -> None:
        scenarios: tuple[
            tuple[str, str, tuple[str, ...], str, int], ...
        ] = (
            (
                "rollback-start-old-metadata",
                "rollback-install-started",
                ("2.26.1",),
                "failed",
                0,
            ),
            (
                "rollback-start-target-metadata",
                "rollback-install-started",
                ("2.27.0",),
                "failed",
                0,
            ),
            (
                "rollback-completed",
                "rollback-installed",
                ("2.26.1",),
                "rolled-back",
                0,
            ),
            (
                "rollback-required",
                "rollback-required",
                ("2.26.1",),
                "rolled-back",
                1,
            ),
        )
        for (
            name,
            phase,
            installed_versions,
            expected_status,
            expected_installs,
        ) in scenarios:
            with self.subTest(name=name):
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
                            ("2026-09-01T00:50:00Z", approved["approval_run_id"]),
                        )
                        connection.execute(
                            """
                            INSERT INTO pi_mcp_execution_recovery(
                                decision_id, approval_run_id, phase, failed_step,
                                failure_summary, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                approved["id"],
                                approved["approval_run_id"],
                                phase,
                                "mcp-validation",
                                "fresh Pi validation failed",
                                "2026-09-01T00:50:00Z",
                            ),
                        )
                    if name == "rollback-required":
                        removed = run_cli(
                            home,
                            "task",
                            "remove",
                            str(task["id"]),
                        )
                        self.assertEqual(removed.returncode, 0, removed.stderr)

                    executed = run_cli(
                        home,
                        "run-due",
                        "--now",
                        "2026-09-01T01:00:00Z",
                        "--json",
                        extra_environment=self.execution_environment(
                            event_log,
                            {
                                "installed_versions": list(installed_versions),
                                "install": {"status": "success"},
                                "restart": {"status": "success"},
                                "health": {
                                    "status": "success",
                                    "result": "healthy",
                                },
                                "pi_validation": {
                                    "status": "success",
                                    "result": "MCP_ADAPTER_OK",
                                },
                                "notification": {"status": "success"},
                            },
                        ),
                    )

                    run = json.loads(executed.stdout)["runs"][0]
                    self.assertEqual(run["status"], expected_status)
                    events = [
                        json.loads(line)
                        for line in event_log.read_text(encoding="utf-8").splitlines()
                    ]
                    operations = [event["operation"] for event in events]
                    self.assertEqual(
                        operations.count("package.install-exact"),
                        expected_installs,
                    )
                    self.assertEqual(operations[-1], "notification.send")
                    if phase in {"rollback-installed", "rollback-required"}:
                        self.assertEqual(executed.returncode, 1, executed.stderr)
                        self.assertEqual(
                            run["details"]["rollback"]["status"],
                            "verified",
                        )
                    else:
                        self.assertEqual(executed.returncode, 1, executed.stderr)
                        self.assertEqual(
                            run["details"]["rollback"]["status"],
                            "ambiguous",
                        )
                        self.assertIn("CRITICAL", events[-1]["text"])

    def test_checkpointed_critical_rollback_outcome_survives_process_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            event_log = Path(directory) / "execution-events.jsonl"
            self.initialize(home)
            task = self.add_task(home)
            approved = self.create_approved_decision(home, str(task["id"]))
            database_path = home / "var" / "data" / "runtasks.sqlite3"
            details = {
                "decision_id": approved["id"],
                "failed_step": "mcp-validation",
                "failure": "fresh Pi validation failed",
                "handler": "pi_mcp_adapter",
                "mutation_performed": True,
                "mutation_status": "performed",
                "new_version": "2.27.0",
                "old_version": "2.26.1",
                "outcome": "critical-rollback-failure",
                "plan_hash": approved["plan_hash"],
                "rollback": {
                    "attempted": True,
                    "failure": "rollback health check failed",
                    "mcp_validation": "not-checked",
                    "pi_web_health": "failed",
                    "required": True,
                    "restored_version": "2.26.1",
                    "status": "failed",
                    "target_version": "2.26.1",
                },
                "steps": [
                    {
                        "name": "rollback-pi-web-health",
                        "status": "failed",
                        "summary": "rollback health check failed",
                    }
                ],
            }
            pending_outcome = json.dumps(
                {
                    "decision_status": "rollback-failed",
                    "details": details,
                    "fresh_check_required": False,
                    "notification_required": True,
                    "status": "failed",
                    "summary": "CRITICAL: rollback health check failed",
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    """
                    UPDATE runs SET status = 'running', started_at = ?
                    WHERE id = ? AND status = 'claimed'
                    """,
                    ("2026-09-01T01:00:00Z", approved["approval_run_id"]),
                )
                connection.execute(
                    """
                    INSERT INTO pi_mcp_execution_recovery(
                        decision_id, approval_run_id, phase, failed_step,
                        failure_summary, pending_outcome_json, updated_at
                    ) VALUES (?, ?, 'rollback-installed', ?, ?, ?, ?)
                    """,
                    (
                        approved["id"],
                        approved["approval_run_id"],
                        "mcp-validation",
                        "fresh Pi validation failed",
                        pending_outcome,
                        "2026-09-01T01:00:00Z",
                    ),
                )

            restarted = run_cli(
                home,
                "run-due",
                "--now",
                "2026-09-01T01:01:00Z",
                "--json",
                extra_environment=self.execution_environment(
                    event_log,
                    {
                        "installed_versions": [],
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

            self.assertEqual(restarted.returncode, 1, restarted.stderr)
            run = json.loads(restarted.stdout)["runs"][0]
            self.assertEqual(run["details"]["outcome"], "critical-rollback-failure")
            events = [
                json.loads(line)
                for line in event_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [event["operation"] for event in events],
                ["notification.send"],
            )
            self.assertIn("CRITICAL", events[0]["text"])

    def test_schema_eight_migration_preserves_history_and_refuses_inflight_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            event_log = Path(directory) / "execution-events.jsonl"
            self.initialize(home)
            task = self.add_task(home)
            approved = self.create_approved_decision(home, str(task["id"]))
            completed = run_cli(
                home,
                "run-due",
                "--now",
                "2026-09-01T01:00:00Z",
                "--json",
                extra_environment=self.execution_environment(event_log),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            before = json.loads(
                run_cli(
                    home,
                    "decision",
                    "show",
                    str(approved["id"]),
                    "--json",
                ).stdout
            )["decision"]
            database_path = home / "var" / "data" / "runtasks.sqlite3"
            with sqlite3.connect(database_path) as connection:
                connection.execute("DROP TABLE pi_mcp_execution_recovery")
                connection.execute("DROP TRIGGER decision_execution_fts_insert")
                connection.execute("DROP TRIGGER decision_execution_fts_update")
                connection.execute("DROP TRIGGER decision_execution_fts_delete")
                connection.execute("DROP TABLE decision_execution_fts")
                connection.execute("DELETE FROM schema_migrations WHERE version = 9")

            migrated = run_cli(home, "init")
            after = json.loads(
                run_cli(
                    home,
                    "decision",
                    "show",
                    str(approved["id"]),
                    "--json",
                ).stdout
            )["decision"]
            searched = json.loads(
                run_cli(home, "search", "MCP_ADAPTER_OK", "--json").stdout
            )["results"]

            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            self.assertEqual(after, before)
            self.assertEqual(
                {result["type"] for result in searched},
                {"decision", "run"},
            )

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
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
                connection.execute("DROP TABLE pi_mcp_execution_recovery")
                connection.execute("DROP TRIGGER decision_execution_fts_insert")
                connection.execute("DROP TRIGGER decision_execution_fts_update")
                connection.execute("DROP TRIGGER decision_execution_fts_delete")
                connection.execute("DROP TABLE decision_execution_fts")
                connection.execute("DELETE FROM schema_migrations WHERE version = 9")

            migrated = run_cli(home, "init")
            with sqlite3.connect(database_path) as connection:
                schema_version = connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                run_status = connection.execute(
                    "SELECT status FROM runs WHERE id = ?",
                    (approved["approval_run_id"],),
                ).fetchone()[0]

            self.assertEqual(migrated.returncode, 2)
            self.assertIn("legacy Pi MCP approval Run", migrated.stderr)
            self.assertEqual(schema_version, 8)
            self.assertEqual(run_status, "running")

    def test_competing_one_shot_processes_install_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            event_log = Path(directory) / "execution-events.jsonl"
            self.initialize(home)
            task = self.add_task(home)
            self.create_approved_decision(home, str(task["id"]))
            database_path = home / "var" / "data" / "runtasks.sqlite3"
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "UPDATE tasks SET next_run_at = ? WHERE id = ?",
                    ("2026-09-01T01:00:00Z", task["id"]),
                )
            request_log = Path(directory) / "release-check-requests.jsonl"
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("RUNTASKS_")
            }
            environment.update(self.execution_environment(event_log))
            environment.update(
                {
                    "RUNTASKS_EXTERNAL_ADAPTER": "fixture",
                    "RUNTASKS_FIXTURE_EXTERNAL_OUTCOME": json.dumps(
                        {
                            "status": "success",
                            "summary": "Concurrent-safe fresh release check.",
                            "details": {
                                "contract": "pi-mcp-release-check/v1",
                                "outcome": "no-change",
                                "installed_version": "2.27.0",
                                "available_version": "2.27.0",
                                "assessment": None,
                                "evidence": [],
                                "source_failures": [],
                            },
                        }
                    ),
                    "RUNTASKS_FIXTURE_REQUEST_LOG": str(request_log),
                }
            )
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
            self.assertEqual(sum(len(payload["runs"]) for payload in payloads), 2)
            observed_statuses = {
                run["status"]
                for payload in payloads
                for run in payload["runs"]
            }
            self.assertEqual(observed_statuses, {"success", "no-change"})
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
                len(request_log.read_text(encoding="utf-8").splitlines()),
                1,
            )

    def test_account_global_lock_serializes_separate_runtime_homes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            event_log = root / "execution-events.jsonl"
            lock_path = root / "account-global-pi-mcp.lock"
            shared_version_path = root / "installed-version.txt"
            shared_version_path.write_text("2.26.1", encoding="utf-8")
            executions: list[tuple[Path, PiMcpExecutionAdapters]] = []
            for index in range(2):
                home = root / f"runtime-home-{index}"
                self.initialize(home)
                task = self.add_task(home)
                self.create_approved_decision(home, str(task["id"]))
                settings = self.execution_environment(event_log)
                settings.update(
                    {
                        "RUNTASKS_FIXTURE_PI_MCP_EXECUTION_LOCK": str(lock_path),
                        "RUNTASKS_FIXTURE_PI_MCP_SHARED_VERSION_PATH": str(
                            shared_version_path
                        ),
                    }
                )
                executions.append(
                    (
                        home / "var" / "data" / "runtasks.sqlite3",
                        build_pi_mcp_execution_adapters(settings, Redactor()),
                    )
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        execute_approved_pi_mcp_runs,
                        database_path,
                        adapters,
                        Redactor(),
                    )
                    for database_path, adapters in executions
                ]
                results = [future.result(timeout=15) for future in futures]

            self.assertEqual(
                sorted(run.status for result in results for run in result),
                ["failed", "success"],
            )
            self.assertEqual(
                shared_version_path.read_text(encoding="utf-8"),
                "2.27.0",
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
                lock_path=base_adapters.lock_path,
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

    def test_notification_is_redacted_before_adapter_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            event_log = Path(directory) / "execution-events.jsonl"
            self.initialize(home)
            task = self.add_task(home)
            approved = self.create_approved_decision(home, str(task["id"]))
            fake_credential = "ghp_notificationboundary1234567890"
            database_path = home / "var" / "data" / "runtasks.sqlite3"
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "UPDATE tasks SET name = ? WHERE id = ?",
                    (f"Secret-bearing task {fake_credential}", task["id"]),
                )
            settings = self.execution_environment(event_log)
            base_adapters = build_pi_mcp_execution_adapters(
                settings,
                Redactor.from_secret_values((fake_credential,)),
            )
            notification = CapturingNotificationAdapter()
            adapters = PiMcpExecutionAdapters(
                package=base_adapters.package,
                service=base_adapters.service,
                health=base_adapters.health,
                mcp_validation=base_adapters.mcp_validation,
                notification=notification,
                lock_path=base_adapters.lock_path,
            )

            runs = execute_approved_pi_mcp_runs(
                database_path,
                adapters,
                Redactor.from_secret_values((fake_credential,)),
            )

            self.assertEqual(runs[0].status, "success")
            self.assertEqual(len(notification.texts), 1)
            self.assertNotIn(fake_credential, notification.texts[0])
            self.assertIn("[REDACTED]", notification.texts[0])
            decision = get_decision(database_path, str(approved["id"]))
            execution = decision.execution
            if execution is None:
                self.fail("completed Decision is missing its execution outcome")
            self.assertEqual(
                execution.notification_delivery.status,
                "delivered",
            )

    def test_process_restart_before_target_mutation_stays_critical_without_package_work(self) -> None:
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
                connection.execute(
                    """
                    INSERT INTO pi_mcp_execution_recovery(
                        decision_id, approval_run_id, phase, updated_at
                    ) VALUES (?, ?, 'target-install-started', ?)
                    """,
                    (
                        approved["id"],
                        approved["approval_run_id"],
                        "2026-09-01T01:00:00Z",
                    ),
                )

            restarted = run_cli(
                home,
                "run-due",
                "--now",
                "2026-09-01T01:01:00Z",
                "--json",
                extra_environment=self.execution_environment(
                    event_log,
                    {
                        "installed_versions": ["2.26.1"],
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

            self.assertEqual(restarted.returncode, 1, restarted.stderr)
            run = json.loads(restarted.stdout)["runs"][0]
            self.assertEqual(
                run["details"]["outcome"],
                "critical-execution-ambiguity",
            )
            self.assertIsNone(run["details"]["mutation_performed"])
            self.assertEqual(run["details"]["mutation_status"], "unknown")
            events = [
                json.loads(line)
                for line in event_log.read_text(encoding="utf-8").splitlines()
            ]
            operations = [event["operation"] for event in events]
            self.assertNotIn("package.install-exact", operations)
            self.assertEqual(operations[-1], "notification.send")
            self.assertIn("CRITICAL", events[-1]["text"])

    def test_process_restart_after_target_mutation_resumes_without_reinstalling(self) -> None:
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
                connection.execute(
                    """
                    INSERT INTO pi_mcp_execution_recovery(
                        decision_id, approval_run_id, phase, updated_at
                    ) VALUES (?, ?, 'target-install-started', ?)
                    """,
                    (
                        approved["id"],
                        approved["approval_run_id"],
                        "2026-09-01T01:00:00Z",
                    ),
                )

            restarted = run_cli(
                home,
                "run-due",
                "--now",
                "2026-09-01T01:10:00Z",
                "--json",
                extra_environment=self.execution_environment(
                    event_log,
                    {
                        "installed_versions": ["2.27.0", "2.27.0"],
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

            self.assertEqual(restarted.returncode, 0, restarted.stderr)
            run = json.loads(restarted.stdout)["runs"][0]
            self.assertEqual(run["status"], "success")
            events = [
                json.loads(line)
                for line in event_log.read_text(encoding="utf-8").splitlines()
            ]
            operations = [event["operation"] for event in events]
            self.assertNotIn("package.install-exact", operations)
            self.assertEqual(
                operations,
                [
                    "package.installed-version",
                    "package.installed-version",
                    "service.restart",
                    "health.check",
                    "pi.validate-mcp",
                    "notification.send",
                ],
            )


class CapturingNotificationAdapter(ExecutionNotificationAdapter):
    def __init__(self) -> None:
        self.texts: list[str] = []

    def send(self, text: str) -> None:
        self.texts.append(text)


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
