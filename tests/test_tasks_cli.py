from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI = PROJECT_ROOT / "bin" / "runtasks"


class TaskCliTests(unittest.TestCase):
    def run_cli(
        self,
        home: Path,
        *arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("RUNTASKS_")
        }
        environment["RUNTASKS_HOME"] = str(home)
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

    def task_payload(self, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": "Pi MCP adapter update check",
            "description": "Review policy-backed adapter releases on a fortnightly schedule.",
            "source_type": "direct",
            "source_ref": None,
            "source_summary": "Check adapter releases and escalate important changes.",
            "schedule": {
                "type": "interval-days",
                "days": 14,
                "time": "09:00",
            },
            "timezone": "Asia/Singapore",
            "next_run_at": "2026-09-01T01:00:00Z",
            "action_mode": "approved-procedure",
            "handler": "pi_mcp_adapter",
            "policy": {
                "important_conditions": ["security", "OAuth safety"],
                "approval_required": True,
                "rollback": "Restore the exact prior version after failed validation.",
            },
        }
        payload.update(overrides)
        return payload

    def test_parser_validation_uses_the_json_error_contract_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"

            result = self.run_cli(home, "--json", "task", "show")

            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stderr, "")
            self.assertEqual(json.loads(result.stdout)["status"], "error")
            self.assertIn("command-line arguments are invalid", result.stdout)

            human_result = self.run_cli(
                home,
                "task",
                "add",
                "--json",
                json.dumps(self.task_payload()),
                "--bad-option",
            )
            self.assertEqual(human_result.returncode, 2)
            self.assertEqual(human_result.stdout, "")
            self.assertIn("unrecognized arguments", human_result.stderr)

    def test_task_commands_require_initialization_without_creating_partial_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"

            result = self.run_cli(
                home,
                "--json",
                "task",
                "add",
                "--json",
                json.dumps(self.task_payload()),
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("run 'runtasks init' first", json.loads(result.stdout)["error"])
            self.assertFalse(home.exists())

    def test_task_can_be_added_and_shown_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            self.initialize(home)
            payload = self.task_payload()

            added = self.run_cli(
                home,
                "--json",
                "task",
                "add",
                "--json",
                json.dumps(payload),
            )

            self.assertEqual(added.returncode, 0, added.stderr)
            added_payload = json.loads(added.stdout)
            self.assertEqual(added_payload["status"], "created")
            task = added_payload["task"]
            self.assertRegex(task["id"], r"^tsk_[0-9a-f]{24}$")
            self.assertEqual(task["name"], payload["name"])
            self.assertEqual(task["schedule"], payload["schedule"])
            self.assertEqual(task["timezone"], payload["timezone"])
            self.assertEqual(task["next_run_at"], "2026-09-01T01:00:00Z")
            self.assertEqual(task["action_mode"], payload["action_mode"])
            self.assertEqual(task["handler"], payload["handler"])
            self.assertEqual(task["policy"], payload["policy"])
            self.assertTrue(task["enabled"])
            self.assertEqual(task["status"], "enabled")
            self.assertTrue(task["available_for_scheduled_execution"])
            self.assertEqual(task["created_at"], task["updated_at"])

            shown = self.run_cli(home, "--json", "task", "show", task["id"])

            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertEqual(json.loads(shown.stdout), {"status": "ok", "task": task})

    def test_task_lifecycle_preserves_identity_and_marks_disabled_tasks_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            self.initialize(home)
            added = self.run_cli(
                home,
                "--json",
                "task",
                "add",
                "--json",
                json.dumps(self.task_payload()),
            )
            task = json.loads(added.stdout)["task"]

            listed = self.run_cli(home, "task", "list", "--json")
            updated = self.run_cli(
                home,
                "--json",
                "task",
                "update",
                task["id"],
                "--json",
                json.dumps(
                    {
                        "description": "Updated description after policy review.",
                        "source_summary": "Updated accepted source summary.",
                    }
                ),
            )
            disabled = self.run_cli(home, "task", "disable", task["id"], "--json")
            disabled_human = self.run_cli(home, "task", "show", task["id"])
            enabled = self.run_cli(home, "--json", "task", "enable", task["id"])
            removed = self.run_cli(home, "task", "remove", task["id"], "--json")
            final_list = self.run_cli(home, "--json", "task", "list")

            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertEqual(json.loads(listed.stdout)["tasks"], [task])

            self.assertEqual(updated.returncode, 0, updated.stderr)
            updated_task = json.loads(updated.stdout)["task"]
            self.assertEqual(updated_task["id"], task["id"])
            self.assertEqual(updated_task["created_at"], task["created_at"])
            self.assertNotEqual(updated_task["updated_at"], task["updated_at"])
            self.assertEqual(
                updated_task["description"],
                "Updated description after policy review.",
            )

            self.assertEqual(disabled.returncode, 0, disabled.stderr)
            disabled_task = json.loads(disabled.stdout)["task"]
            self.assertFalse(disabled_task["enabled"])
            self.assertEqual(disabled_task["status"], "disabled")
            self.assertFalse(disabled_task["available_for_scheduled_execution"])
            self.assertIn(
                "disabled (unavailable for scheduled execution)",
                disabled_human.stdout,
            )
            self.assertIn("Source: direct", disabled_human.stdout)
            self.assertIn('"approval_required": true', disabled_human.stdout)
            self.assertIn(f"Created: {task['created_at']}", disabled_human.stdout)
            self.assertIn("Updated:", disabled_human.stdout)

            self.assertEqual(enabled.returncode, 0, enabled.stderr)
            self.assertTrue(json.loads(enabled.stdout)["task"]["enabled"])
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertEqual(
                json.loads(removed.stdout),
                {"status": "removed", "task_id": task["id"]},
            )
            self.assertEqual(json.loads(final_list.stdout)["tasks"], [])

    def test_removed_task_is_tombstoned_and_can_be_registered_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            self.initialize(home)
            first_add = self.run_cli(
                home,
                "--json",
                "task",
                "add",
                "--json",
                json.dumps(self.task_payload()),
            )
            first_task = json.loads(first_add.stdout)["task"]

            removed = self.run_cli(home, "task", "remove", first_task["id"])
            hidden_show = self.run_cli(
                home,
                "--json",
                "task",
                "show",
                first_task["id"],
            )
            second_add = self.run_cli(
                home,
                "--json",
                "task",
                "add",
                "--json",
                json.dumps(self.task_payload()),
            )

            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertEqual(hidden_show.returncode, 0, hidden_show.stderr)
            removed_task = json.loads(hidden_show.stdout)["task"]
            self.assertEqual(removed_task["status"], "removed")
            self.assertFalse(removed_task["available_for_scheduled_execution"])
            self.assertIsNotNone(removed_task["removed_at"])
            self.assertEqual(second_add.returncode, 0, second_add.stderr)
            second_task = json.loads(second_add.stdout)["task"]
            self.assertNotEqual(second_task["id"], first_task["id"])

    def test_duplicate_proposals_return_update_oriented_outcomes_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            self.initialize(home)
            original = self.run_cli(
                home,
                "--json",
                "task",
                "add",
                "--json",
                json.dumps(self.task_payload()),
            )
            original_task = json.loads(original.stdout)["task"]

            identity_duplicate = self.run_cli(
                home,
                "--json",
                "task",
                "add",
                "--json",
                json.dumps(
                    self.task_payload(
                        name="  pi mcp ADAPTER update check  ",
                        description="Different prose for the same stable task identity.",
                    )
                ),
            )
            policy_duplicate = self.run_cli(
                home,
                "--json",
                "task",
                "add",
                "--json",
                json.dumps(
                    self.task_payload(
                        name="Adapter release review",
                        source_type="session",
                        source_ref="session-elsewhere",
                    )
                ),
            )
            changed_schedule_duplicate = self.run_cli(
                home,
                "--json",
                "task",
                "add",
                "--json",
                json.dumps(
                    self.task_payload(
                        name="Rescheduled adapter review",
                        source_type="session",
                        source_ref="another-session",
                        schedule={"type": "daily", "time": "09:00"},
                    )
                ),
            )
            changed_handler_duplicate = self.run_cli(
                home,
                "--json",
                "task",
                "add",
                "--json",
                json.dumps(
                    self.task_payload(
                        source_type="document",
                        source_ref="policies/review.md",
                        action_mode="notify",
                        handler="manual_notification",
                        policy={"message": "Review adapter releases manually."},
                    )
                ),
            )
            listed = self.run_cli(home, "--json", "task", "list")

            self.assertEqual(identity_duplicate.returncode, 2)
            self.assertEqual(
                json.loads(identity_duplicate.stdout),
                {
                    "error": (
                        "RunTasks validation failed: identity-equivalent task already "
                        f"exists; update task {original_task['id']} instead"
                    ),
                    "existing_task_id": original_task["id"],
                    "outcome": "update-existing",
                    "reason": "identity-equivalent",
                    "status": "duplicate",
                },
            )
            self.assertEqual(policy_duplicate.returncode, 2)
            policy_result = json.loads(policy_duplicate.stdout)
            self.assertEqual(policy_result["status"], "duplicate")
            self.assertEqual(policy_result["reason"], "policy-equivalent")
            self.assertEqual(policy_result["existing_task_id"], original_task["id"])
            self.assertEqual(changed_schedule_duplicate.returncode, 0)
            changed_schedule_task = json.loads(changed_schedule_duplicate.stdout)["task"]
            self.assertEqual(changed_handler_duplicate.returncode, 2)
            changed_handler_result = json.loads(changed_handler_duplicate.stdout)
            self.assertEqual(changed_handler_result["reason"], "identity-equivalent")
            self.assertEqual(
                {task["id"] for task in json.loads(listed.stdout)["tasks"]},
                {original_task["id"], changed_schedule_task["id"]},
            )

    def test_same_source_reference_is_identity_equivalent_after_rename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            self.initialize(home)
            first = self.run_cli(
                home,
                "--json",
                "task",
                "add",
                "--json",
                json.dumps(
                    self.task_payload(
                        source_type="document",
                        source_ref="policies/adapter-review.md",
                    )
                ),
            )
            first_task = json.loads(first.stdout)["task"]

            renamed = self.run_cli(
                home,
                "--json",
                "task",
                "add",
                "--json",
                json.dumps(
                    self.task_payload(
                        name="Renamed adapter policy",
                        source_type="document",
                        source_ref="policies/adapter-review.md",
                        action_mode="notify",
                        handler="manual_notification",
                        policy={"message": "Review this policy manually."},
                    )
                ),
            )

            self.assertEqual(renamed.returncode, 2)
            result = json.loads(renamed.stdout)
            self.assertEqual(result["reason"], "identity-equivalent")
            self.assertEqual(result["existing_task_id"], first_task["id"])

    def test_invalid_task_input_is_redacted_and_never_partially_written(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            self.initialize(home)
            cases = (
                (
                    {"schedule": {"type": "cron", "value": "* * * * *"}},
                    "schedule type must be daily or interval-days",
                ),
                (
                    {"timezone": "Not/A_Real_Timezone"},
                    "timezone must name an installed IANA timezone",
                ),
                (
                    {"timezone": "localtime"},
                    "timezone must name an installed IANA timezone",
                ),
                (
                    {"action_mode": "shell"},
                    "action_mode must be check, notify, or approved-procedure",
                ),
                ({"handler": "arbitrary_shell"}, "handler is not registered"),
                (
                    {"action_mode": "notify", "handler": "pi_mcp_adapter"},
                    "handler does not support the selected action_mode",
                ),
                (
                    {"next_run_at": "2026-09-01T02:00:00Z"},
                    "next_run_at must occur exactly at the schedule time in the task timezone",
                ),
                (
                    {"next_run_at": "2026-09-01T01:00:59Z"},
                    "next_run_at must occur exactly at the schedule time in the task timezone",
                ),
                (
                    {"policy": {"command": "curl https://example.invalid"}},
                    "policy contains executable or secret-bearing fields",
                ),
                (
                    {"policy": {"api_key": "must-not-be-stored"}},
                    "policy contains executable or secret-bearing fields",
                ),
                (
                    {"policy": {"access_token_value": "must-not-be-stored"}},
                    "policy contains executable or secret-bearing fields",
                ),
                (
                    {"policy": {"db_password_hash": "must-not-be-stored"}},
                    "policy contains executable or secret-bearing fields",
                ),
                (
                    {"policy": {"apiKey": "must-not-be-stored"}},
                    "policy contains executable or secret-bearing fields",
                ),
                (
                    {"policy": {"privateKey": "must-not-be-stored"}},
                    "policy contains executable or secret-bearing fields",
                ),
                (
                    {"policy": {"shellCommand": "must-not-run"}},
                    "policy contains executable or secret-bearing fields",
                ),
                (
                    {"policy": {"credential": "must-not-be-stored"}},
                    "policy contains executable or secret-bearing fields",
                ),
                (
                    {"policy": {"authorization": "Bearer must-not-be-stored"}},
                    "policy contains executable or secret-bearing fields",
                ),
                (
                    {"policy": {"message": "Bearer must-not-be-stored"}},
                    "policy contains secret-like values",
                ),
                (
                    {
                        "policy": {
                            "note": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
                        }
                    },
                    "policy contains secret-like values",
                ),
                (
                    {"policy": {"note": "AKIAABCDEFGHIJKLMNOP"}},
                    "policy contains secret-like values",
                ),
            )

            for overrides, expected_error in cases:
                with self.subTest(overrides=overrides):
                    result = self.run_cli(
                        home,
                        "--json",
                        "task",
                        "add",
                        "--json",
                        json.dumps(self.task_payload(**overrides)),
                    )
                    self.assertEqual(result.returncode, 2)
                    output = json.loads(result.stdout)
                    self.assertEqual(output["status"], "error")
                    self.assertIn(expected_error, output["error"])

            duplicate_key_payload = json.dumps(self.task_payload())[:-1] + (
                ',"handler":"manual_notification"}'
            )
            duplicate_key = self.run_cli(
                home,
                "--json",
                "task",
                "add",
                "--json",
                duplicate_key_payload,
            )
            self.assertEqual(duplicate_key.returncode, 2)
            self.assertIn(
                "must not contain duplicate object keys",
                json.loads(duplicate_key.stdout)["error"],
            )

            private_value = "private-token-value-must-not-leak"
            malformed = self.run_cli(
                home,
                "--json",
                "task",
                "add",
                "--json",
                '{"name":"' + private_value,
            )
            listed = self.run_cli(home, "--json", "task", "list")

            self.assertEqual(malformed.returncode, 2)
            self.assertNotIn(private_value, malformed.stdout + malformed.stderr)
            self.assertEqual(json.loads(listed.stdout)["tasks"], [])

    def test_failed_validation_preserves_every_existing_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            self.initialize(home)
            added = self.run_cli(
                home,
                "--json",
                "task",
                "add",
                "--json",
                json.dumps(self.task_payload()),
            )
            original_task = json.loads(added.stdout)["task"]

            failed_update = self.run_cli(
                home,
                "--json",
                "task",
                "update",
                original_task["id"],
                "--json",
                json.dumps(
                    {
                        "description": "This must not be persisted.",
                        "timezone": "Invalid/Timezone",
                    }
                ),
            )
            shown = self.run_cli(home, "--json", "task", "show", original_task["id"])

            self.assertEqual(failed_update.returncode, 2)
            self.assertEqual(json.loads(shown.stdout)["task"], original_task)

    def test_conflicting_update_rolls_back_and_leaves_the_registry_usable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            self.initialize(home)
            first_result = self.run_cli(
                home,
                "--json",
                "task",
                "add",
                "--json",
                json.dumps(self.task_payload()),
            )
            second_result = self.run_cli(
                home,
                "--json",
                "task",
                "add",
                "--json",
                json.dumps(
                    self.task_payload(
                        name="Daily manual review",
                        source_type="document",
                        source_ref="policies/manual-review.md",
                        schedule={"type": "daily", "time": "09:00"},
                        action_mode="notify",
                        handler="manual_notification",
                        policy={"message": "Review the dashboard."},
                    )
                ),
            )
            first = json.loads(first_result.stdout)["task"]
            second = json.loads(second_result.stdout)["task"]

            conflict = self.run_cli(
                home,
                "--json",
                "task",
                "update",
                first["id"],
                "--json",
                json.dumps(
                    {
                        "description": "This valid change must be rolled back.",
                        "schedule": second["schedule"],
                        "action_mode": second["action_mode"],
                        "handler": second["handler"],
                        "policy": second["policy"],
                    }
                ),
            )
            shown = self.run_cli(home, "--json", "task", "show", first["id"])
            listed = self.run_cli(home, "--json", "task", "list")

            self.assertEqual(conflict.returncode, 2)
            self.assertEqual(json.loads(conflict.stdout)["reason"], "policy-equivalent")
            self.assertEqual(json.loads(shown.stdout)["task"], first)
            self.assertEqual(len(json.loads(listed.stdout)["tasks"]), 2)

    def test_daily_manual_notification_is_a_supported_bounded_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            self.initialize(home)
            payload = self.task_payload(
                name="Daily operations reminder",
                schedule={"type": "daily", "time": "09:00"},
                action_mode="notify",
                handler="manual_notification",
                policy={"message": "Review the operations dashboard manually."},
            )

            result = self.run_cli(
                home,
                "--json",
                "task",
                "add",
                "--json",
                json.dumps(payload),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            task = json.loads(result.stdout)["task"]
            self.assertEqual(task["schedule"], {"type": "daily", "time": "09:00"})
            self.assertEqual(task["action_mode"], "notify")
            self.assertEqual(task["handler"], "manual_notification")

    def test_search_indexes_task_text_and_stays_synchronized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            self.initialize(home)
            added = self.run_cli(
                home,
                "--json",
                "task",
                "add",
                "--json",
                json.dumps(self.task_payload()),
            )
            task = json.loads(added.stdout)["task"]

            name_match = self.run_cli(home, "--json", "search", "fortnightly")
            punctuation_match = self.run_cli(home, "--json", "search", "policy-backed")
            source_match = self.run_cli(home, "search", "escalate", "--json")
            policy_match = self.run_cli(home, "--json", "search", "OAuth safety")
            update = self.run_cli(
                home,
                "task",
                "update",
                task["id"],
                "--json",
                json.dumps(
                    {
                        "description": "Inspect release evidence for compatibility regressions.",
                        "source_summary": "Review compatibility evidence.",
                        "policy": {
                            "important_conditions": ["compatibility regression"],
                            "approval_required": True,
                        },
                    }
                ),
            )
            old_match = self.run_cli(home, "--json", "search", "OAuth")
            new_match = self.run_cli(home, "--json", "search", "regression")
            remove = self.run_cli(home, "task", "remove", task["id"])
            removed_match = self.run_cli(home, "--json", "search", "regression")

            for result in (
                name_match,
                punctuation_match,
                source_match,
                policy_match,
                update,
                new_match,
                remove,
            ):
                self.assertEqual(result.returncode, 0, result.stderr)
            for result in (
                name_match,
                punctuation_match,
                source_match,
                policy_match,
                new_match,
            ):
                matches = json.loads(result.stdout)["results"]
                self.assertEqual(len(matches), 1)
                self.assertEqual(matches[0]["type"], "task")
                self.assertEqual(matches[0]["task"]["id"], task["id"])
            self.assertEqual(json.loads(old_match.stdout)["results"], [])
            self.assertEqual(json.loads(removed_match.stdout)["results"], [])


if __name__ == "__main__":
    unittest.main()
