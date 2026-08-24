from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from typing import Any, cast

from tests.cli_test_support import run_cli


class PiMcpHandlerCliTests(unittest.TestCase):
    def initialize(self, home: Path) -> None:
        result = run_cli(home, "init")
        self.assertEqual(result.returncode, 0, result.stderr)

    def add_task(self, home: Path, *, next_run_at: str) -> dict[str, Any]:
        payload = {
            "name": "Pi MCP adapter release check",
            "description": "Inspect every intervening stable adapter release.",
            "source_type": "document",
            "source_ref": "policy-note",
            "source_summary": "Keep the exact pin unless an update is important.",
            "schedule": {"type": "interval-days", "days": 14, "time": "09:00"},
            "timezone": "Asia/Singapore",
            "next_run_at": next_run_at,
            "action_mode": "approved-procedure",
            "handler": "pi_mcp_adapter",
            "policy": {
                "approval_required": True,
                "important_conditions": ["security", "OAuth safety"],
                "active_mcp_servers": ["stripe"],
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

    def adapter_environment(
        self,
        details: dict[str, object],
        request_log: Path,
    ) -> dict[str, str]:
        return {
            "RUNTASKS_EXTERNAL_ADAPTER": "fixture",
            "RUNTASKS_FIXTURE_EXTERNAL_OUTCOME": json.dumps(
                {
                    "status": "success",
                    "summary": "Fixture Pi MCP release inspection.",
                    "details": details,
                }
            ),
            "RUNTASKS_FIXTURE_REQUEST_LOG": str(request_log),
        }

    def test_same_version_scheduled_check_is_no_change_advances_fourteen_days_and_creates_no_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            request_log = Path(directory) / "requests.jsonl"
            self.initialize(home)
            task = self.add_task(home, next_run_at="2026-09-01T01:00:00Z")
            environment = self.adapter_environment(
                {
                    "contract": "pi-mcp-release-check/v1",
                    "outcome": "no-change",
                    "installed_version": "2.26.1",
                    "available_version": "2.26.1",
                    "assessment": None,
                    "evidence": [],
                    "source_failures": [],
                },
                request_log,
            )

            executed = run_cli(
                home,
                "run-due",
                "--now",
                "2026-09-01T01:00:00Z",
                "--json",
                extra_environment=environment,
            )
            decisions = run_cli(home, "decisions", "--json")
            shown = run_cli(home, "task", "show", str(task["id"]), "--json")

            self.assertEqual(executed.returncode, 0, executed.stderr)
            run = json.loads(executed.stdout)["runs"][0]
            self.assertEqual(run["status"], "no-change")
            self.assertEqual(run["details"]["installed_version"], "2.26.1")
            self.assertFalse(run["details"]["mutation_performed"])
            self.assertEqual(
                json.loads(shown.stdout)["task"]["next_run_at"],
                "2026-09-15T01:00:00Z",
            )
            self.assertEqual(json.loads(decisions.stdout)["decisions"], [])
            request = json.loads(request_log.read_text(encoding="utf-8"))
            self.assertEqual(request["operation"], "pi_mcp_adapter.inspect")
            self.assertEqual(
                request["parameters"]["importance_context"],
                {
                    "active_mcp_servers": ["stripe"],
                    "important_conditions": ["security", "OAuth safety"],
                },
            )

    def test_confident_non_important_is_searchable_and_quiet_while_important_creates_exact_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            request_log = Path(directory) / "requests.jsonl"
            self.initialize(home)
            task = self.add_task(home, next_run_at="2026-09-01T01:00:00Z")
            evidence = [
                {
                    "version": "3.0.0",
                    "sources": [
                        {
                            "source": "changelog",
                            "title": "3.0.0",
                            "body": "Documentation and refactoring only.",
                            "reference": "https://example.invalid/changelog#300",
                        }
                    ],
                }
            ]
            non_important = self.adapter_environment(
                {
                    "contract": "pi-mcp-release-check/v1",
                    "outcome": "non-important",
                    "installed_version": "2.26.1",
                    "available_version": "3.0.0",
                    "assessment": {
                        "importance": "non-important",
                        "category": "routine",
                        "reason": "Only documentation and refactoring changed.",
                        "recommendation": "Remain pinned.",
                        "confidence": "high",
                    },
                    "evidence": evidence,
                    "source_failures": [],
                },
                request_log,
            )

            first = run_cli(
                home,
                "run",
                str(task["id"]),
                "--json",
                extra_environment=non_important,
            )
            searched = run_cli(home, "search", "documentation refactoring", "--json")
            decisions_before = run_cli(home, "decisions", "--json")

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(json.loads(first.stdout)["run"]["status"], "non-important")
            self.assertEqual(len(json.loads(searched.stdout)["results"]), 1)
            self.assertEqual(json.loads(decisions_before.stdout)["decisions"], [])

            important = self.adapter_environment(
                {
                    "contract": "pi-mcp-release-check/v1",
                    "outcome": "decision-required",
                    "installed_version": "2.26.1",
                    "available_version": "2.26.2",
                    "assessment": {
                        "importance": "important",
                        "category": "security",
                        "reason": "Patch release fixes a relevant security defect.",
                        "recommendation": "Update after approval.",
                        "confidence": "high",
                    },
                    "evidence": evidence,
                    "source_failures": [],
                },
                request_log,
            )
            second = run_cli(
                home,
                "run",
                str(task["id"]),
                "--json",
                extra_environment=important,
            )
            decisions_after = run_cli(home, "decisions", "--json")

            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(json.loads(second.stdout)["run"]["status"], "decision-required")
            decisions = json.loads(decisions_after.stdout)["decisions"]
            self.assertEqual(len(decisions), 1)
            plan = decisions[0]["plan"]
            self.assertEqual(plan["parameters"]["installed_version"], "2.26.1")
            self.assertEqual(plan["parameters"]["target_version"], "2.26.2")
            self.assertEqual(plan["rollback"]["target_version"], "2.26.1")
            self.assertEqual(plan["validation"]["expected_mcp_result"], "MCP_ADAPTER_OK")


if __name__ == "__main__":
    unittest.main()
