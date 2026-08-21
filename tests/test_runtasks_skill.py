from __future__ import annotations

from datetime import datetime, time as clock_time, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from typing import Any, cast
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = PROJECT_ROOT / "skills" / "runtasks"
SKILL_FILE = SKILL_DIR / "SKILL.md"
CONFIRM_HELPER = SKILL_DIR / "scripts" / "confirm.py"
SCENARIOS_FILE = PROJECT_ROOT / "tests" / "fixtures" / "runtasks_skill_scenarios.json"
CLI = PROJECT_ROOT / "bin" / "runtasks"
PYTHON = Path(sys.executable)


class RunTasksSkillTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.temporary_path = Path(self.temporary_directory.name)
        self.home = self.temporary_path / "runtime-home"
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("RUNTASKS_")
        }
        self.environment["RUNTASKS_HOME"] = str(self.home)
        temporary_root = self.temporary_path / "tmp"
        temporary_root.mkdir()
        self.environment["TMPDIR"] = str(temporary_root)
        self.environment["PATH"] = self._path_with_uv_shim()
        initialized = self.run_cli("init")
        self.assertEqual(initialized.returncode, 0, initialized.stderr)

    def _path_with_uv_shim(self) -> str:
        shim_directory = self.temporary_path / "bin"
        shim_directory.mkdir()
        shim = shim_directory / "uv"
        python = shlex.quote(str(PYTHON))
        shim.write_text(
            f"""#!/bin/sh
set -eu
if [ "$1" != "run" ]; then
    echo "unsupported uv test invocation" >&2
    exit 64
fi
shift
if [ "$1" = "--project" ]; then
    shift 2
fi
if [ "$1" = "--locked" ]; then
    shift
fi
if [ "$1" != "runtasks" ]; then
    echo "unsupported uv test command" >&2
    exit 64
fi
shift
exec {python} -m runtasks.cli "$@"
""",
            encoding="utf-8",
        )
        shim.chmod(0o755)
        return f"{shim_directory}:{self.environment['PATH']}"

    def scenarios(self) -> list[dict[str, Any]]:
        value: object = json.loads(SCENARIOS_FILE.read_text(encoding="utf-8"))
        return cast(list[dict[str, Any]], value)

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(CLI), *arguments],
            cwd=PROJECT_ROOT,
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def run_helper(
        self,
        command: str,
        *arguments: str,
        proposal: dict[str, Any] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(PYTHON), str(CONFIRM_HELPER), command, *arguments],
            cwd=PROJECT_ROOT,
            env=self.environment,
            input=None if proposal is None else json.dumps(proposal),
            text=True,
            capture_output=True,
            check=False,
        )

    def stage(self, proposal: dict[str, Any]) -> tuple[Path, str]:
        result = self.run_helper("stage", proposal=proposal)
        self.assertEqual(result.returncode, 0, result.stderr)
        match = re.search(r"^Review file: (.+)$", result.stdout, flags=re.MULTILINE)
        self.assertIsNotNone(match, result.stdout)
        assert match is not None
        return Path(match.group(1)), result.stdout

    def listed_tasks(self) -> list[dict[str, Any]]:
        result = self.run_cli("--json", "task", "list")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = cast(dict[str, Any], json.loads(result.stdout))
        return cast(list[dict[str, Any]], payload["tasks"])

    def test_skill_uses_only_standard_frontmatter_and_is_repository_runnable(self) -> None:
        text = SKILL_FILE.read_text(encoding="utf-8")
        frontmatter_match = re.match(r"^---\n(.*?)\n---\n", text, flags=re.DOTALL)
        self.assertIsNotNone(frontmatter_match)
        assert frontmatter_match is not None
        frontmatter_lines = frontmatter_match.group(1).splitlines()
        keys = {line.split(":", 1)[0] for line in frontmatter_lines if ":" in line}

        self.assertEqual(keys, {"name", "description"})
        self.assertIn("name: runtasks", frontmatter_lines)
        self.assertEqual(SKILL_FILE.parent.name, "runtasks")
        self.assertIn("scripts/confirm.py stage", text)
        self.assertTrue(CONFIRM_HELPER.is_file())
        self.assertNotIn("disable-model-invocation", text)
        self.assertNotIn("conversation transcript API", text)

    def test_skill_documents_every_source_and_safety_branch(self) -> None:
        text = SKILL_FILE.read_text(encoding="utf-8")
        required_phrases = (
            "current conversation",
            "document",
            "pasted or selected text",
            "direct instruction",
            "existing Task",
            "one proposal for each independent schedule",
            "concise source_summary",
            "YES",
            "NO",
            "EDIT",
            "task list --json",
            "never write SQL",
            "manual_notification",
            "never turn prose into unattended shell commands",
        )
        for phrase in required_phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_all_source_and_multiple_task_scenarios_stage_complete_proposals_without_mutation(self) -> None:
        staged_paths: list[Path] = []
        required_sections = (
            "Task name",
            "Schedule",
            "Timezone",
            "Automatic behavior",
            "Importance conditions",
            "Notifications",
            "Approvals",
            "Execution",
            "Validation",
            "Rollback",
            "Source",
            "Assumptions",
            "Operation",
            "Structured proposal",
            "1. YES",
            "2. NO",
            "3. EDIT",
        )

        expected_source_types = {
            "current conversation": ["session"],
            "document": ["document"],
            "pasted or selected text": ["direct"],
            "direct instruction": ["direct"],
            "existing Task update": ["existing-task"],
            "multiple independent Tasks": ["direct", "direct"],
        }
        for scenario in self.scenarios():
            proposals = cast(list[dict[str, Any]], scenario["proposals"])
            source = cast(str, scenario["source"])
            now = datetime.fromisoformat(cast(str, scenario["now"]).replace("Z", "+00:00"))
            missing_details = cast(list[str], scenario["missing_routine_details"])
            risk_categories = cast(list[str], scenario["risk_categories"])
            self.assertTrue(source.strip())
            self.assertEqual(
                [proposal["task"]["source_type"] for proposal in proposals],
                expected_source_types[scenario["name"]],
            )
            if scenario["name"] == "current conversation":
                self.assertEqual(proposals[0]["task"]["schedule"]["days"], 14)
                importance = " ".join(
                    proposals[0]["task"]["policy"]["important_conditions"]
                )
                self.assertIn("OAuth", importance)
            elif scenario["name"] == "document":
                self.assertEqual(
                    proposals[0]["task"]["source_ref"], scenario["source_ref"]
                )
                self.assertIn("every week", source)
                self.assertEqual(
                    proposals[0]["task"]["schedule"]["days"],
                    7,
                )
            elif scenario["name"] == "pasted or selected text":
                self.assertEqual(
                    proposals[0]["task"]["schedule"],
                    {"type": "daily", "time": "09:00"},
                )
            elif scenario["name"] == "existing Task update":
                self.assertIn(proposals[0]["task_id"], source)
                self.assertEqual(proposals[0]["operation"], "update")
            elif scenario["name"] == "multiple independent Tasks":
                self.assertEqual(len(proposals), 2)
                self.assertEqual(
                    [proposal["task"]["schedule"] for proposal in proposals],
                    [
                        {"type": "daily", "time": "09:00"},
                        {"type": "interval-days", "days": 7, "time": "10:00"},
                    ],
                )

            for proposal in proposals:
                task = cast(dict[str, Any], proposal["task"])
                task_timezone = ZoneInfo(cast(str, task["timezone"]))
                schedule = cast(dict[str, Any], task["schedule"])
                hour, minute = (
                    int(part) for part in cast(str, schedule["time"]).split(":")
                )
                local_now = now.astimezone(task_timezone)
                next_local = datetime.combine(
                    local_now.date(),
                    clock_time(hour=hour, minute=minute),
                    tzinfo=task_timezone,
                )
                if next_local <= local_now:
                    next_local += timedelta(days=1)
                expected_next_run = next_local.astimezone(timezone.utc).isoformat(
                    timespec="seconds"
                ).replace("+00:00", "Z")
                self.assertEqual(task["next_run_at"], expected_next_run)

                source_summary = cast(str, task["source_summary"])
                self.assertLessEqual(len(source_summary), 300)
                self.assertNotEqual(source_summary.casefold(), source.casefold())

                policy = cast(dict[str, Any], task["policy"])
                assumptions = " ".join(cast(list[str], policy["assumptions"])).casefold()
                for detail in missing_details:
                    self.assertIn(detail, assumptions)
                if risk_categories:
                    approvals = " ".join(
                        cast(list[str], policy["approval_requirements"])
                    ).casefold()
                    for risk in risk_categories:
                        self.assertIn(risk, approvals)
                    self.assertEqual(task["action_mode"], "notify")
                    self.assertEqual(task["handler"], "manual_notification")

            for proposal in proposals:
                with self.subTest(scenario=scenario["name"], task=proposal["task"]["name"]):
                    review_file, output = self.stage(proposal)
                    staged_paths.append(review_file)
                    for section in required_sections:
                        self.assertIn(section, output)
                    self.assertIn(proposal["task"]["source_summary"], output)
                    self.assertIn(proposal["operation"].upper(), output)
                    self.assertEqual(self.listed_tasks(), [])

        self.assertEqual(len(staged_paths), 7)
        self.assertEqual(len(set(staged_paths)), 7)

    def test_no_discards_and_edit_restages_without_mutating_until_yes(self) -> None:
        proposal = self.scenarios()[3]["proposals"][0]
        first_review, _ = self.stage(proposal)

        rejected_apply = self.run_helper("apply", str(first_review), "NO")
        self.assertEqual(rejected_apply.returncode, 2)
        self.assertIn("exact confirmation YES", rejected_apply.stderr)
        self.assertEqual(self.listed_tasks(), [])

        discarded = self.run_helper("discard", str(first_review))
        self.assertEqual(discarded.returncode, 0, discarded.stderr)
        self.assertFalse(first_review.exists())
        self.assertEqual(self.listed_tasks(), [])

        edited = json.loads(json.dumps(proposal))
        edited["task"]["description"] = "Edited credential reminder reviewed by the operator."
        edited["task"]["policy"]["assumptions"] = [
            "The operator selected the edited description.",
            "The default run time is 09:00 Asia/Singapore.",
        ]
        edited_review, edited_output = self.stage(edited)
        self.assertIn("Edited credential reminder reviewed by the operator.", edited_output)
        self.assertEqual(self.listed_tasks(), [])

        applied = self.run_helper("apply", str(edited_review), "YES")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        task = json.loads(applied.stdout)["task"]
        self.assertEqual(task["description"], edited["task"]["description"])
        self.assertEqual(task["policy"], edited["task"]["policy"])
        self.assertFalse(edited_review.exists())

    def test_yes_submits_the_exact_reviewed_update_payload(self) -> None:
        original_proposal = self.scenarios()[2]["proposals"][0]
        original_review, _ = self.stage(original_proposal)
        created = self.run_helper("apply", str(original_review), "YES")
        self.assertEqual(created.returncode, 0, created.stderr)
        original = json.loads(created.stdout)["task"]

        update = json.loads(json.dumps(self.scenarios()[4]["proposals"][0]))
        update["task_id"] = original["id"]
        update["task"]["source_ref"] = original["id"]
        update_review, _ = self.stage(update)
        self.assertEqual(self.listed_tasks(), [original])

        applied = self.run_helper("apply", str(update_review), "YES")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        updated = json.loads(applied.stdout)["task"]
        for key, value in update["task"].items():
            self.assertEqual(updated[key], value, key)
        self.assertEqual(updated["id"], original["id"])
        self.assertEqual(updated["created_at"], original["created_at"])

    def test_duplicate_add_is_not_silently_converted_to_an_update(self) -> None:
        proposal = self.scenarios()[0]["proposals"][0]
        first_review, _ = self.stage(proposal)
        first = self.run_helper("apply", str(first_review), "YES")
        self.assertEqual(first.returncode, 0, first.stderr)
        original = json.loads(first.stdout)["task"]

        duplicate_review, _ = self.stage(proposal)
        self.assertEqual(len(self.listed_tasks()), 1)
        duplicate = self.run_helper("apply", str(duplicate_review), "YES")

        self.assertEqual(duplicate.returncode, 2)
        outcome = json.loads(duplicate.stdout)
        self.assertEqual(outcome["status"], "duplicate")
        self.assertEqual(outcome["outcome"], "update-existing")
        self.assertEqual(outcome["existing_task_id"], original["id"])
        self.assertTrue(duplicate_review.exists())
        self.assertEqual(self.listed_tasks(), [original])

        discarded = self.run_helper("discard", str(duplicate_review))
        self.assertEqual(discarded.returncode, 0, discarded.stderr)
        update = json.loads(json.dumps(proposal))
        update["operation"] = "update"
        update["task_id"] = original["id"]
        update["task"]["description"] = (
            "Reviewed update after the duplicate outcome identified the existing Task."
        )
        update["task"]["source_type"] = "existing-task"
        update["task"]["source_ref"] = original["id"]
        update["task"]["source_summary"] = (
            "Existing Task update reviewed after duplicate detection."
        )
        update["task"]["policy"]["assumptions"] = [
            "The duplicate outcome identified this existing Task.",
            "The default run time remains 09:00 Asia/Singapore.",
        ]
        update_review, _ = self.stage(update)
        self.assertEqual(self.listed_tasks(), [original])

        updated_result = self.run_helper("apply", str(update_review), "YES")
        self.assertEqual(updated_result.returncode, 0, updated_result.stderr)
        updated = json.loads(updated_result.stdout)["task"]
        self.assertEqual(updated["id"], original["id"])
        self.assertEqual(updated["description"], update["task"]["description"])
        self.assertEqual(len(self.listed_tasks()), 1)

    def test_changed_review_file_cannot_apply_a_different_payload(self) -> None:
        proposal = self.scenarios()[0]["proposals"][0]
        review_file, _ = self.stage(proposal)
        changed = json.loads(review_file.read_text(encoding="utf-8"))
        changed["task"]["name"] = "Unreviewed replacement name"
        review_file.write_text(json.dumps(changed), encoding="utf-8")

        result = self.run_helper("apply", str(review_file), "YES")

        self.assertEqual(result.returncode, 2)
        self.assertIn("changed after it was shown", result.stderr)
        self.assertEqual(self.listed_tasks(), [])

    def test_stage_rejects_a_payload_the_task_cli_would_not_accept(self) -> None:
        proposal = json.loads(json.dumps(self.scenarios()[0]["proposals"][0]))
        proposal["task"]["schedule"] = {
            "type": "interval-days",
            "days": 0,
            "time": "not-a-time",
        }
        proposal["task"]["timezone"] = "not/a-zone"
        proposal["task"]["next_run_at"] = "not-a-date"

        result = self.run_helper("stage", proposal=proposal)

        self.assertEqual(result.returncode, 2)
        self.assertIn("task payload is invalid", result.stderr)
        self.assertEqual(self.listed_tasks(), [])

    def test_stage_requires_disclosed_assumptions_and_complete_review_fields(self) -> None:
        proposal = json.loads(json.dumps(self.scenarios()[0]["proposals"][0]))
        del proposal["task"]["policy"]["assumptions"]

        result = self.run_helper("stage", proposal=proposal)

        self.assertEqual(result.returncode, 2)
        self.assertIn("policy.assumptions", result.stderr)
        self.assertEqual(self.listed_tasks(), [])


if __name__ == "__main__":
    unittest.main()
