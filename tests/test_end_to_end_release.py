from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any, Sequence, cast
import unittest

from runtasks.one_shot import OneShotRunTrigger
from runtasks.redaction import Redactor
from runtasks.telegram_config import load_telegram_settings
from runtasks.telegram_decisions import (
    TelegramDecisionButton,
    TelegramDecisionClient,
    listen_for_decisions,
)
from runtasks.telegram_updates import TelegramCallbackRecord, TelegramUpdateRecord
from tests.cli_test_support import run_cli


_OPERATOR_ID = 700001
_FAKE_TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"  # release-check: allow-fake-secret
_ADVANCED_NOW = "2026-09-15T01:00:00Z"


class _RecordedUpdates:
    def __init__(self, batches: Sequence[Sequence[TelegramUpdateRecord]] = ()) -> None:
        self._batches = [list(batch) for batch in batches]

    async def get_bot_username(self) -> str:
        return "runtasks_bot"

    async def get_webhook_url(self) -> str:
        return ""

    async def get_updates(
        self,
        *,
        timeout_seconds: int,
        offset: int | None = None,
    ) -> list[TelegramUpdateRecord]:
        del timeout_seconds, offset
        return self._batches.pop(0) if self._batches else []


class _RecordedDecisionClient(TelegramDecisionClient):
    def __init__(self) -> None:
        self.interactive: list[
            tuple[int, str, tuple[TelegramDecisionButton, ...], int | None, int]
        ] = []
        self.messages: list[tuple[int, str, int | None]] = []
        self.callback_answers: list[tuple[str, str, bool]] = []
        self._next_message_id = 500

    async def send_message(
        self,
        *,
        destination: int,
        text: str,
        thread_id: int | None = None,
    ) -> None:
        self.messages.append((destination, text, thread_id))

    async def send_interactive_message(
        self,
        *,
        destination: int,
        text: str,
        buttons: Sequence[TelegramDecisionButton],
        thread_id: int | None = None,
    ) -> int:
        message_id = self._next_message_id
        self._next_message_id += 1
        self.interactive.append(
            (destination, text, tuple(buttons), thread_id, message_id)
        )
        return message_id

    async def answer_callback(
        self,
        *,
        callback_id: str,
        text: str,
        show_alert: bool = False,
    ) -> None:
        self.callback_answers.append((callback_id, text, show_alert))


class _RecordedOneShotTrigger(OneShotRunTrigger):
    def __init__(self) -> None:
        self.requests = 0

    async def request(self) -> None:
        self.requests += 1


class PublicReleaseEndToEndTests(unittest.IsolatedAsyncioTestCase):
    def release_outcome(self) -> dict[str, object]:
        return {
            "status": "success",
            "summary": "Important fake Pi MCP release requires approval.",
            "details": {
                "contract": "pi-mcp-release-check/v1",
                "outcome": "decision-required",
                "installed_version": "2.26.1",
                "available_version": "2.27.0",
                "assessment": {
                    "importance": "important",
                    "category": "credential-oauth",
                    "reason": "Fake OAuth credential correction affects this installation.",
                    "recommendation": "Install the exact fake release after approval.",
                    "confidence": "high",
                },
                "evidence": [
                    {
                        "version": "2.27.0",
                        "sources": [
                            {
                                "source": "changelog",
                                "title": "2.27.0",
                                "body": "Fake OAuth credential handling correction.",
                                "reference": "https://example.invalid/releases/2.27.0",
                            }
                        ],
                    }
                ],
                "source_failures": [],
                "source_references": [
                    "https://example.invalid/releases/2.27.0"
                ],
            },
        }

    def initialize_and_discover_decision(self, home: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        initialized = run_cli(home, "init")
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        task_payload = {
            "name": "Pi MCP adapter public-release proof",
            "description": "Inspect all fake stable releases every 14 days.",
            "source_type": "direct",
            "source_ref": None,
            "source_summary": "Escalate important or uncertain fake releases.",
            "schedule": {"type": "interval-days", "days": 14, "time": "09:00"},
            "timezone": "Asia/Singapore",
            "next_run_at": "2026-09-01T01:00:00Z",
            "action_mode": "approved-procedure",
            "handler": "pi_mcp_adapter",
            "policy": {
                "approval_required": True,
                "important_conditions": ["security", "OAuth safety"],
            },
        }
        added = run_cli(
            home,
            "--json",
            "task",
            "add",
            "--json",
            json.dumps(task_payload),
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        task = cast(dict[str, Any], json.loads(added.stdout)["task"])

        checked = run_cli(
            home,
            "run-due",
            "--now",
            _ADVANCED_NOW,
            "--json",
            extra_environment={
                "RUNTASKS_EXTERNAL_ADAPTER": "fixture",
                "RUNTASKS_FIXTURE_EXTERNAL_OUTCOME": json.dumps(
                    self.release_outcome()
                ),
            },
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)
        checked_run = json.loads(checked.stdout)["runs"][0]
        self.assertEqual(checked_run["status"], "decision-required")
        self.assertEqual(checked_run["scheduled_for"], "2026-09-01T01:00:00Z")
        decisions = run_cli(home, "decisions", "--json")
        self.assertEqual(decisions.returncode, 0, decisions.stderr)
        decision = cast(dict[str, Any], json.loads(decisions.stdout)["decisions"][0])
        return task, decision

    async def approve_through_fake_telegram(
        self,
        home: Path,
        decision: dict[str, Any],
    ) -> _RecordedOneShotTrigger:
        settings = load_telegram_settings(
            {
                "RUNTASKS_TELEGRAM_BOT_TOKEN": _FAKE_TOKEN,
                "RUNTASKS_TELEGRAM_ALLOWED_USER_IDS": str(_OPERATOR_ID),
                "RUNTASKS_TELEGRAM_NOTIFICATION_CHAT_ID": str(_OPERATOR_ID),
            },
            require_destination=True,
        )
        client = _RecordedDecisionClient()
        trigger = _RecordedOneShotTrigger()
        database_path = home / "var" / "data" / "runtasks.sqlite3"
        redactor = Redactor.from_secret_values((_FAKE_TOKEN,))
        await listen_for_decisions(
            _RecordedUpdates(),
            client,
            settings,
            database_path,
            trigger,
            redactor,
            max_batches=0,
        )
        self.assertEqual(len(client.interactive), 1)
        _, text, buttons, _, message_id = client.interactive[0]
        self.assertIn("Pi MCP adapter public-release proof", text)
        self.assertNotIn(str(decision["id"]), text)
        approve = next(button for button in buttons if button.text == "1. APPROVE")
        callback = TelegramCallbackRecord(
            callback_id="callback-public-release",
            user_id=_OPERATOR_ID,
            chat_id=_OPERATOR_ID,
            chat_type="private",
            message_id=message_id,
            data=approve.callback_data,
        )
        await listen_for_decisions(
            _RecordedUpdates(
                ((TelegramUpdateRecord(1, None, callback),),)
            ),
            client,
            settings,
            database_path,
            trigger,
            redactor,
            max_batches=1,
        )
        self.assertGreaterEqual(trigger.requests, 1)
        shown = run_cli(
            home,
            "decision",
            "show",
            str(decision["id"]),
            "--json",
        )
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(json.loads(shown.stdout)["decision"]["status"], "approved")
        return trigger

    def execution_environment(
        self,
        event_log: Path,
        fixture: dict[str, object],
    ) -> dict[str, str]:
        return {
            "RUNTASKS_PI_MCP_EXECUTION_ADAPTER": "fixture",
            "RUNTASKS_FIXTURE_PI_MCP_EXECUTION": json.dumps(fixture),
            "RUNTASKS_FIXTURE_PI_MCP_EXECUTION_LOG": str(event_log),
        }

    async def test_complete_fake_approval_flow_updates_validates_and_searches_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            event_log = Path(directory) / "events.jsonl"
            task, decision = self.initialize_and_discover_decision(home)
            await self.approve_through_fake_telegram(home, decision)

            executed = run_cli(
                home,
                "run-due",
                "--now",
                _ADVANCED_NOW,
                "--json",
                extra_environment=self.execution_environment(
                    event_log,
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
                    },
                ),
            )

            self.assertEqual(executed.returncode, 0, executed.stderr)
            approval_run = json.loads(executed.stdout)["runs"][0]
            self.assertEqual(approval_run["status"], "success")
            self.assertEqual(approval_run["details"]["new_version"], "2.27.0")
            operations = [
                json.loads(line)["operation"]
                for line in event_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                operations,
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
            history = run_cli(home, "history", str(task["id"]), "--json")
            self.assertEqual(history.returncode, 0, history.stderr)
            self.assertEqual(
                {run["status"] for run in json.loads(history.stdout)["runs"]},
                {"decision-required", "success"},
            )
            searched = run_cli(home, "search", "MCP_ADAPTER_OK", "--json")
            self.assertEqual(searched.returncode, 0, searched.stderr)
            self.assertIn(
                "run",
                {result["type"] for result in json.loads(searched.stdout)["results"]},
            )

    async def test_fake_validation_failure_proves_exact_rollback_and_critical_failure_reporting(self) -> None:
        scenarios = (
            (
                "rollback-verified",
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
                        {"status": "success", "result": "MCP_ADAPTER_OK"},
                    ],
                    "notification": {"status": "success"},
                },
                "rolled-back",
            ),
            (
                "rollback-critical",
                {
                    "installed_versions": ["2.26.1", "2.27.0"],
                    "install": [
                        {"status": "success"},
                        {"status": "failed", "error": "synthetic rollback failure"},
                    ],
                    "restart": {"status": "success"},
                    "health": {"status": "success", "result": "healthy"},
                    "pi_validation": {"status": "failed", "result": "invalid"},
                    "notification": {"status": "success"},
                },
                "failed",
            ),
        )
        for name, fixture, expected_status in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                home = Path(directory) / "runtime-home"
                event_log = Path(directory) / "events.jsonl"
                _, decision = self.initialize_and_discover_decision(home)
                await self.approve_through_fake_telegram(home, decision)

                executed = run_cli(
                    home,
                    "run-due",
                    "--now",
                    _ADVANCED_NOW,
                    "--json",
                    extra_environment=self.execution_environment(event_log, fixture),
                )

                self.assertEqual(executed.returncode, 1, executed.stderr)
                approval_run = json.loads(executed.stdout)["runs"][0]
                self.assertEqual(approval_run["status"], expected_status)
                events = [
                    json.loads(line)
                    for line in event_log.read_text(encoding="utf-8").splitlines()
                ]
                installs = [
                    event["version"]
                    for event in events
                    if event["operation"] == "package.install-exact"
                ]
                self.assertEqual(installs, ["2.27.0", "2.26.1"])
                notification = cast(str, events[-1]["text"])
                if expected_status == "rolled-back":
                    self.assertEqual(
                        approval_run["details"]["rollback"]["restored_version"],
                        "2.26.1",
                    )
                    self.assertIn("rollback verified", notification.lower())
                else:
                    self.assertEqual(
                        approval_run["details"]["outcome"],
                        "critical-rollback-failure",
                    )
                    self.assertIn("CRITICAL", notification)
                    self.assertIn("URGENT", notification)


if __name__ == "__main__":
    unittest.main()
