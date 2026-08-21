from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from typing import Any, cast


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI = PROJECT_ROOT / "bin" / "runtasks"


class DecisionCliTests(unittest.TestCase):
    def run_cli(
        self,
        home: Path,
        *arguments: str,
        extra_environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("RUNTASKS_")
        }
        environment["RUNTASKS_HOME"] = str(home)
        if extra_environment is not None:
            environment.update(extra_environment)
        return subprocess.run(
            [str(CLI), *arguments],
            cwd=PROJECT_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def initialize(self, home: Path) -> None:
        result = self.run_cli(home, "init")
        self.assertEqual(result.returncode, 0, result.stderr)

    def add_approved_task(self, home: Path) -> dict[str, Any]:
        payload = {
            "name": "Approved adapter update",
            "description": "Inspect adapter releases and request exact approval.",
            "source_type": "direct",
            "source_ref": None,
            "source_summary": "Important adapter updates require approval.",
            "schedule": {"type": "daily", "time": "09:00"},
            "timezone": "Asia/Singapore",
            "next_run_at": "2026-09-01T01:00:00Z",
            "action_mode": "approved-procedure",
            "handler": "pi_mcp_adapter",
            "policy": {
                "approval_required": True,
                "important_conditions": ["security", "OAuth safety"],
            },
        }
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

    def decision_environment(
        self,
        request_log: Path,
        *,
        private_value: str = "private-plan-evidence",
        reverse_plan_keys: bool = False,
    ) -> dict[str, str]:
        plan_items: list[tuple[str, object]] = [
            ("handler", "pi_mcp_adapter"),
            ("operation", "install-exact-version"),
            (
                "parameters",
                {
                    "installed_version": "2.26.1",
                    "restart_service": "pi-web.service",
                    "target_version": "2.27.0",
                },
            ),
            (
                "validation",
                {"expected_result": "MCP_ADAPTER_OK", "health_check": "Pi Web"},
            ),
            ("rollback", {"target_version": "2.26.1"}),
            (
                "evidence",
                {
                    "release": "Security and OAuth correction",
                    "private_note": private_value,
                    private_value: "Secret-bearing evidence key was redacted.",
                },
            ),
        ]
        if reverse_plan_keys:
            plan_items.reverse()
        outcome = {
            "status": "decision-required",
            "summary": "Important adapter update requires approval.",
            "details": {"assessment": "security update"},
            "decision": {
                "plan": dict(plan_items),
                "reason": "OAuth credential handling needs a reviewed update.",
                "validation_summary": "Require exact MCP_ADAPTER_OK validation.",
                "rollback_summary": "Restore exact version 2.26.1 on failure.",
            },
        }
        return {
            "RUNTASKS_TEST_CREDENTIAL": private_value,
            "RUNTASKS_FIXTURE_HANDLER_OUTCOME": json.dumps(outcome),
            "RUNTASKS_FIXTURE_HANDLER_REQUEST_LOG": str(request_log),
        }

    def create_decision(
        self,
        home: Path,
        task_id: str,
        request_log: Path,
        *,
        private_value: str = "private-plan-evidence",
        reverse_plan_keys: bool = False,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        environment = self.decision_environment(
            request_log,
            private_value=private_value,
            reverse_plan_keys=reverse_plan_keys,
        )
        result = self.run_cli(
            home,
            "run",
            task_id,
            "--json",
            extra_environment=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        run = cast(dict[str, Any], json.loads(result.stdout)["run"])
        self.assertEqual(run["status"], "decision-required")
        return run, environment

    def test_handler_request_creates_an_immutable_searchable_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            request_log = Path(directory) / "handler-requests.jsonl"
            private_value = "decision-evidence-must-not-leak"
            self.initialize(home)
            task = self.add_approved_task(home)

            run, environment = self.create_decision(
                home,
                str(task["id"]),
                request_log,
                private_value=private_value,
            )
            listed = self.run_cli(
                home,
                "decisions",
                "--json",
                extra_environment={"RUNTASKS_TEST_CREDENTIAL": private_value},
            )
            human_list = self.run_cli(home, "decisions")

            self.assertEqual(listed.returncode, 0, listed.stderr)
            decisions = json.loads(listed.stdout)["decisions"]
            self.assertEqual(len(decisions), 1)
            decision = decisions[0]
            self.assertRegex(decision["id"], r"^dcs_[0-9a-f]{24}$")
            self.assertEqual(decision["task_id"], task["id"])
            self.assertEqual(decision["run_id"], run["id"])
            self.assertEqual(decision["status"], "pending")
            self.assertIsNone(decision["response"])
            self.assertIsNone(decision["approval_run_id"])
            self.assertIsNotNone(decision["created_at"])
            self.assertEqual(decision["created_at"], decision["updated_at"])
            self.assertEqual(decision["plan"]["evidence"]["private_note"], "[REDACTED]")
            self.assertIn("[REDACTED]", decision["plan"]["evidence"])
            expected_hash = hashlib.sha256(
                json.dumps(
                    decision["plan"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            self.assertEqual(decision["plan_hash"], expected_hash)
            self.assertEqual(run["details"]["decision_id"], decision["id"])
            self.assertEqual(run["details"]["plan_hash"], decision["plan_hash"])
            self.assertIn(decision["id"], human_list.stdout)
            self.assertIn("pending", human_list.stdout)

            shown = self.run_cli(
                home,
                "decision",
                "show",
                decision["id"],
                "--json",
                extra_environment={"RUNTASKS_TEST_CREDENTIAL": private_value},
            )
            human_show = self.run_cli(home, "decision", "show", decision["id"])
            reason_search = self.run_cli(home, "search", "credential handling", "--json")
            validation_search = self.run_cli(home, "search", "MCP_ADAPTER_OK", "--json")
            rollback_search = self.run_cli(home, "search", "Restore exact version", "--json")

            self.assertEqual(json.loads(shown.stdout)["decision"], decision)
            self.assertIn(decision["plan_hash"], human_show.stdout)
            self.assertIn("install-exact-version", human_show.stdout)
            for result in (
                listed,
                shown,
                human_list,
                human_show,
                reason_search,
                validation_search,
                rollback_search,
            ):
                self.assertNotIn(private_value, result.stdout + result.stderr)
            for result in (reason_search, validation_search, rollback_search):
                matching = json.loads(result.stdout)["results"]
                self.assertEqual(len(matching), 1)
                self.assertEqual(matching[0]["type"], "decision")
                self.assertEqual(matching[0]["decision"]["id"], decision["id"])

            database_file = home / "var" / "data" / "runtasks.sqlite3"
            with sqlite3.connect(database_file) as connection:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE decisions SET plan_json = '{}' WHERE id = ?",
                        (decision["id"],),
                    )
            unchanged = self.run_cli(
                home,
                "decision",
                "show",
                decision["id"],
                "--json",
            )
            self.assertEqual(json.loads(unchanged.stdout)["decision"], decision)

            requests = request_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(requests), 1)
            self.assertEqual(json.loads(requests[0])["run_id"], run["id"])
            self.assertIn("RUNTASKS_FIXTURE_HANDLER_OUTCOME", environment)

    def test_canonical_plan_hash_is_deterministic_for_key_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            request_log = Path(directory) / "handler-requests.jsonl"
            self.initialize(home)
            task = self.add_approved_task(home)

            self.create_decision(home, str(task["id"]), request_log)
            self.create_decision(
                home,
                str(task["id"]),
                request_log,
                reverse_plan_keys=True,
            )
            listed = self.run_cli(home, "decisions", "--json")

            self.assertEqual(listed.returncode, 0, listed.stderr)
            decisions = json.loads(listed.stdout)["decisions"]
            self.assertEqual(len(decisions), 2)
            self.assertEqual(decisions[0]["plan"], decisions[1]["plan"])
            self.assertEqual(decisions[0]["plan_hash"], decisions[1]["plan_hash"])

    def test_invalid_decision_request_fails_the_run_without_partial_decision_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            self.initialize(home)
            task = self.add_approved_task(home)
            malformed_environment = {
                "RUNTASKS_FIXTURE_HANDLER_OUTCOME": json.dumps(
                    {
                        "status": "decision-required",
                        "summary": "This request omits the immutable plan.",
                        "details": {"mutation_performed": False},
                    }
                )
            }

            executed = self.run_cli(
                home,
                "run",
                str(task["id"]),
                "--json",
                extra_environment=malformed_environment,
            )
            listed = self.run_cli(home, "decisions", "--json")

            self.assertEqual(executed.returncode, 1, executed.stderr)
            run = json.loads(executed.stdout)["run"]
            self.assertEqual(run["status"], "failed")
            self.assertIn("omitted the required Decision request", run["summary"])
            self.assertEqual(json.loads(listed.stdout)["decisions"], [])

            request_log = Path(directory) / "invalid-plan-requests.jsonl"
            incomplete_environment = self.decision_environment(request_log)
            incomplete_outcome = json.loads(
                incomplete_environment["RUNTASKS_FIXTURE_HANDLER_OUTCOME"]
            )
            incomplete_outcome["decision"]["plan"] = {
                "handler": "pi_mcp_adapter"
            }
            incomplete_environment["RUNTASKS_FIXTURE_HANDLER_OUTCOME"] = json.dumps(
                incomplete_outcome
            )
            incomplete = self.run_cli(
                home,
                "run",
                str(task["id"]),
                "--json",
                extra_environment=incomplete_environment,
            )
            self.assertEqual(incomplete.returncode, 1, incomplete.stderr)
            self.assertIn(
                "missing authorization fields",
                json.loads(incomplete.stdout)["run"]["summary"],
            )

            non_finite_environment = self.decision_environment(request_log)
            non_finite_outcome = json.loads(
                non_finite_environment["RUNTASKS_FIXTURE_HANDLER_OUTCOME"]
            )
            non_finite_outcome["decision"]["plan"]["parameters"][
                "target_version"
            ] = float("nan")
            non_finite_environment["RUNTASKS_FIXTURE_HANDLER_OUTCOME"] = json.dumps(
                non_finite_outcome
            )
            non_finite = self.run_cli(
                home,
                "run",
                str(task["id"]),
                "--json",
                extra_environment=non_finite_environment,
            )
            self.assertEqual(non_finite.returncode, 1, non_finite.stderr)
            self.assertIn(
                "JSON-compatible",
                json.loads(non_finite.stdout)["run"]["summary"],
            )

            request_log = Path(directory) / "secret-operation-requests.jsonl"
            private_value = "must-not-become-an-authorized-target"
            secret_environment = self.decision_environment(
                request_log,
                private_value=private_value,
            )
            secret_outcome = json.loads(
                secret_environment["RUNTASKS_FIXTURE_HANDLER_OUTCOME"]
            )
            secret_outcome["decision"]["plan"]["parameters"][
                "target_version"
            ] = private_value
            secret_environment["RUNTASKS_FIXTURE_HANDLER_OUTCOME"] = json.dumps(
                secret_outcome
            )
            secret_operation = self.run_cli(
                home,
                "run",
                str(task["id"]),
                "--json",
                extra_environment=secret_environment,
            )

            self.assertEqual(secret_operation.returncode, 1, secret_operation.stderr)
            secret_run = json.loads(secret_operation.stdout)["run"]
            self.assertEqual(secret_run["status"], "failed")
            self.assertIn("secret-bearing values", secret_run["summary"])
            self.assertNotIn(
                private_value,
                secret_operation.stdout + secret_operation.stderr,
            )
            self.assertEqual(
                json.loads(self.run_cli(home, "decisions", "--json").stdout)[
                    "decisions"
                ],
                [],
            )

    def test_approval_audit_time_never_precedes_a_replayed_run_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            request_log = Path(directory) / "handler-requests.jsonl"
            self.initialize(home)
            task = self.add_approved_task(home)
            replay_time = "2099-09-01T01:00:00Z"
            environment = self.decision_environment(request_log)

            executed = self.run_cli(
                home,
                "run-due",
                "--now",
                replay_time,
                "--json",
                extra_environment=environment,
            )
            self.assertEqual(executed.returncode, 0, executed.stderr)
            pending = json.loads(
                self.run_cli(home, "decisions", "--json").stdout
            )["decisions"][0]
            approved = self.run_cli(
                home,
                "decision",
                "approve",
                pending["id"],
                "--json",
            )

            self.assertEqual(approved.returncode, 0, approved.stderr)
            decision = json.loads(approved.stdout)["decision"]
            self.assertEqual(decision["created_at"], replay_time)
            created_at = datetime.fromisoformat(
                decision["created_at"].replace("Z", "+00:00")
            )
            responded_at = datetime.fromisoformat(
                decision["response"]["responded_at"].replace("Z", "+00:00")
            )
            updated_at = datetime.fromisoformat(
                decision["updated_at"].replace("Z", "+00:00")
            )
            self.assertGreaterEqual(responded_at, created_at)
            self.assertGreaterEqual(updated_at, created_at)
            self.assertEqual(decision["task_id"], task["id"])

    def test_approval_is_idempotent_and_schedules_execution_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            request_log = Path(directory) / "handler-requests.jsonl"
            self.initialize(home)
            task = self.add_approved_task(home)
            initial_run, _ = self.create_decision(
                home,
                str(task["id"]),
                request_log,
            )
            pending = json.loads(
                self.run_cli(home, "decisions", "--json").stdout
            )["decisions"][0]

            first = self.run_cli(home, "decision", "approve", pending["id"], "--json")
            repeated = self.run_cli(home, "--json", "decision", "approve", pending["id"])
            conflicting = self.run_cli(
                home,
                "decision",
                "reject",
                pending["id"],
                "--json",
            )
            history = self.run_cli(home, "history", str(task["id"]), "--json")

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            first_decision = json.loads(first.stdout)["decision"]
            repeated_decision = json.loads(repeated.stdout)["decision"]
            self.assertEqual(first_decision, repeated_decision)
            self.assertEqual(first_decision["status"], "approved")
            self.assertEqual(first_decision["response"]["action"], "approve")
            self.assertEqual(first_decision["response"]["channel"], "cli")
            self.assertEqual(first_decision["response"]["responded_by"], "local-user")
            self.assertIsNotNone(first_decision["response"]["responded_at"])
            self.assertRegex(first_decision["approval_run_id"], r"^run_[0-9a-f]{24}$")
            self.assertIsNotNone(first_decision["execution_scheduled_at"])

            runs = json.loads(history.stdout)["runs"]
            self.assertEqual(len(runs), 2)
            approval_runs = [run for run in runs if run["trigger"] == "approval"]
            self.assertEqual(len(approval_runs), 1)
            approval_run = approval_runs[0]
            self.assertEqual(approval_run["id"], first_decision["approval_run_id"])
            self.assertEqual(approval_run["status"], "claimed")
            self.assertEqual(approval_run["details"]["decision_id"], pending["id"])
            self.assertEqual(approval_run["details"]["plan_hash"], pending["plan_hash"])
            self.assertEqual(
                [run for run in runs if run["id"] == initial_run["id"]][0]["status"],
                "decision-required",
            )
            self.assertEqual(len(request_log.read_text(encoding="utf-8").splitlines()), 1)

            self.assertEqual(conflicting.returncode, 2)
            self.assertEqual(json.loads(conflicting.stdout)["status"], "error")
            self.assertIn("cannot transition", conflicting.stdout)

            database_file = home / "var" / "data" / "runtasks.sqlite3"
            with sqlite3.connect(database_file) as connection:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE decisions SET responded_by = 'rewritten' WHERE id = ?",
                        (pending["id"],),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "DELETE FROM decisions WHERE id = ?",
                        (pending["id"],),
                    )

    def test_competing_approvals_schedule_only_one_execution_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            request_log = Path(directory) / "handler-requests.jsonl"
            self.initialize(home)
            task = self.add_approved_task(home)
            self.create_decision(home, str(task["id"]), request_log)
            pending = json.loads(
                self.run_cli(home, "decisions", "--json").stdout
            )["decisions"][0]
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("RUNTASKS_")
            }
            environment["RUNTASKS_HOME"] = str(home)
            command = [
                str(CLI),
                "decision",
                "approve",
                pending["id"],
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
            history = self.run_cli(home, "history", str(task["id"]), "--json")

            for process, (_, stderr) in zip(processes, completed, strict=True):
                self.assertEqual(process.returncode, 0, stderr)
            decisions = [json.loads(stdout)["decision"] for stdout, _ in completed]
            self.assertEqual(decisions[0], decisions[1])
            approval_runs = [
                run
                for run in json.loads(history.stdout)["runs"]
                if run["trigger"] == "approval"
            ]
            self.assertEqual(len(approval_runs), 1)
            self.assertEqual(approval_runs[0]["id"], decisions[0]["approval_run_id"])
            self.assertEqual(len(request_log.read_text(encoding="utf-8").splitlines()), 1)

    def test_removed_task_cannot_schedule_a_pending_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            request_log = Path(directory) / "handler-requests.jsonl"
            self.initialize(home)
            task = self.add_approved_task(home)
            initial_run, _ = self.create_decision(
                home,
                str(task["id"]),
                request_log,
            )
            pending = json.loads(
                self.run_cli(home, "decisions", "--json").stdout
            )["decisions"][0]
            removed = self.run_cli(home, "task", "remove", str(task["id"]))

            approved = self.run_cli(
                home,
                "decision",
                "approve",
                pending["id"],
                "--json",
            )
            rejected = self.run_cli(
                home,
                "decision",
                "reject",
                pending["id"],
                "--json",
            )
            history = self.run_cli(home, "history", str(task["id"]), "--json")

            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertEqual(approved.returncode, 2)
            self.assertIn("Task is removed", approved.stdout)
            self.assertEqual(rejected.returncode, 0, rejected.stderr)
            self.assertEqual(json.loads(rejected.stdout)["decision"]["status"], "rejected")
            self.assertEqual(json.loads(history.stdout)["runs"], [initial_run])

    def test_rejection_is_idempotent_and_never_schedules_or_mutates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            request_log = Path(directory) / "handler-requests.jsonl"
            self.initialize(home)
            task = self.add_approved_task(home)
            initial_run, _ = self.create_decision(
                home,
                str(task["id"]),
                request_log,
            )
            pending = json.loads(
                self.run_cli(home, "decisions", "--json").stdout
            )["decisions"][0]

            first = self.run_cli(home, "decision", "reject", pending["id"], "--json")
            repeated = self.run_cli(home, "decision", "reject", pending["id"], "--json")
            conflicting = self.run_cli(home, "decision", "approve", pending["id"], "--json")
            history = self.run_cli(home, "history", str(task["id"]), "--json")

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            first_decision = json.loads(first.stdout)["decision"]
            self.assertEqual(first_decision, json.loads(repeated.stdout)["decision"])
            self.assertEqual(first_decision["status"], "rejected")
            self.assertEqual(first_decision["response"]["action"], "reject")
            self.assertIsNone(first_decision["approval_run_id"])
            self.assertIsNone(first_decision["execution_scheduled_at"])

            runs = json.loads(history.stdout)["runs"]
            self.assertEqual(runs, [initial_run])
            self.assertEqual(len(request_log.read_text(encoding="utf-8").splitlines()), 1)
            self.assertEqual(conflicting.returncode, 2)
            self.assertIn("cannot transition", conflicting.stdout)


if __name__ == "__main__":
    unittest.main()
