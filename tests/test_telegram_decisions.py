from __future__ import annotations

import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from typing import Any, Mapping, Sequence, cast

from runtasks.notifications import NotificationDestinationError
from runtasks.one_shot import OneShotRunTrigger, OneShotRunTriggerError
from runtasks.redaction import Redactor
from runtasks.telegram_config import load_telegram_settings
from runtasks.telegram_errors import TelegramDeliveryError
from runtasks.telegram_decisions import (
    TelegramDecisionButton,
    TelegramDecisionClient,
    listen_for_decisions,
)
from runtasks.telegram_updates import (
    TelegramCallbackRecord,
    TelegramUpdateRecord,
)
from tests.cli_test_support import run_cli
from tests.telegram_fakes import FakeTelegramClient


TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"  # release-check: allow-fake-secret
USER_ID = 998877665


class RecordedUpdateSource:
    def __init__(self, batches: Sequence[Sequence[TelegramUpdateRecord]]) -> None:
        self.batches = [list(batch) for batch in batches]
        self.poll_requests: list[tuple[int, int | None]] = []

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
        self.poll_requests.append((timeout_seconds, offset))
        return self.batches.pop(0) if self.batches else []


class RecordingDecisionClient(TelegramDecisionClient):
    def __init__(self) -> None:
        self.interactive_messages: list[
            tuple[int, str, tuple[TelegramDecisionButton, ...], int | None, int]
        ] = []
        self.messages: list[tuple[int, str, int | None]] = []
        self.callback_answers: list[tuple[str, str, bool]] = []
        self._next_message_id = 100

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
        self.interactive_messages.append(
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


class FailingDecisionClient(RecordingDecisionClient):
    async def send_interactive_message(
        self,
        *,
        destination: int,
        text: str,
        buttons: Sequence[TelegramDecisionButton],
        thread_id: int | None = None,
    ) -> int:
        del destination, text, buttons, thread_id
        raise TelegramDeliveryError("private Telegram transport diagnostics")


class InvalidDestinationDecisionClient(RecordingDecisionClient):
    async def send_interactive_message(
        self,
        *,
        destination: int,
        text: str,
        buttons: Sequence[TelegramDecisionButton],
        thread_id: int | None = None,
    ) -> int:
        del destination, text, buttons, thread_id
        raise NotificationDestinationError("notification destination is invalid")


class RecordingOneShotTrigger(OneShotRunTrigger):
    def __init__(self) -> None:
        self.requests = 0

    async def request(self) -> None:
        self.requests += 1


class FailingOneShotTrigger(OneShotRunTrigger):
    def __init__(self) -> None:
        self.requests = 0

    async def request(self) -> None:
        self.requests += 1
        raise OneShotRunTriggerError("private systemctl diagnostics")


class TelegramDecisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.home = Path(self._temporary_directory.name) / "runtime-home"
        self.database_path = self.home / "var" / "data" / "runtasks.sqlite3"
        self.settings = load_telegram_settings(
            {
                "RUNTASKS_TELEGRAM_BOT_TOKEN": TOKEN,
                "RUNTASKS_TELEGRAM_ALLOWED_USER_IDS": str(USER_ID),
                "RUNTASKS_TELEGRAM_NOTIFICATION_CHAT_ID": str(USER_ID),
            },
            require_destination=True,
        )
        self.task, self.decision = self._create_pending_decision()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _run_cli(
        self,
        *arguments: str,
        extra_environment: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        result = run_cli(
            self.home,
            *arguments,
            extra_environment=extra_environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return cast(dict[str, Any], json.loads(result.stdout))

    def _create_pending_decision(self) -> tuple[dict[str, Any], dict[str, Any]]:
        initialized = run_cli(self.home, "init")
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        task_payload = {
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
        task = self._run_cli(
            "--json",
            "task",
            "add",
            "--json",
            json.dumps(task_payload),
        )["task"]
        outcome = {
            "status": "decision-required",
            "summary": "Important adapter update requires approval.",
            "details": {"assessment": "security update"},
            "decision": {
                "plan": {
                    "handler": "pi_mcp_adapter",
                    "operation": "install-exact-version",
                    "parameters": {
                        "installed_version": "2.26.1",
                        "restart_service": "pi-web.service",
                        "target_version": "2.27.0",
                    },
                    "validation": {
                        "expected_result": "MCP_ADAPTER_OK",
                        "health_check": "Pi Web",
                    },
                    "rollback": {"target_version": "2.26.1"},
                    "assessment": {
                        "importance": "important",
                        "category": "credential-oauth",
                        "reason": "OAuth credential handling needs a reviewed update.",
                        "recommendation": "Install the exact proposed version after approval.",
                        "confidence": "high",
                    },
                    "evidence": {
                        "release": "Security and OAuth correction",
                        "private_note": "private-plan-evidence",
                    },
                },
                "reason": "OAuth credential handling needs a reviewed update.",
                "validation_summary": "Require exact MCP_ADAPTER_OK validation.",
                "rollback_summary": "Restore exact version 2.26.1 on failure.",
            },
        }
        self._run_cli(
            "run",
            str(task["id"]),
            "--json",
            extra_environment={
                "RUNTASKS_TEST_CREDENTIAL": "private-plan-evidence",
                "RUNTASKS_FIXTURE_HANDLER_OUTCOME": json.dumps(outcome),
            },
        )
        decision = self._run_cli("decisions", "--json")["decisions"][0]
        return cast(dict[str, Any], task), cast(dict[str, Any], decision)

    async def test_schema_five_migration_preserves_pending_decisions(self) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("DROP TABLE pi_mcp_execution_recovery")
            connection.execute("DROP TABLE decision_execution_fts")
            connection.execute("DROP TABLE decision_execution_outcomes")
            connection.execute("DROP TABLE decision_notification_deliveries")
            connection.execute("DROP TABLE telegram_decision_messages")
            connection.execute("DROP TABLE approval_run_trigger_requests")
            connection.execute("DELETE FROM schema_migrations WHERE version >= 6")

        initialized = run_cli(self.home, "init")
        shown = self._run_cli(
            "decision", "show", str(self.decision["id"]), "--json"
        )["decision"]
        status = self._run_cli("status", "--json")

        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.assertEqual(shown, self.decision)
        self.assertEqual(status["database"]["schema_version"], 9)
        self.assertEqual(
            shown["notification_delivery"],
            {
                "attempts": 0,
                "delivered_at": None,
                "last_attempt_at": None,
                "last_error": None,
                "status": "pending",
            },
        )

    async def test_schema_six_migration_preserves_delivered_notification_outcomes(self) -> None:
        messages = RecordingDecisionClient()
        await listen_for_decisions(
            RecordedUpdateSource([]),
            messages,
            self.settings,
            self.database_path,
            RecordingOneShotTrigger(),
            Redactor(),
            max_batches=0,
        )
        delivered_before = self._run_cli(
            "decision", "show", str(self.decision["id"]), "--json"
        )["decision"]["notification_delivery"]
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("DROP TABLE pi_mcp_execution_recovery")
            connection.execute("DROP TABLE decision_execution_fts")
            connection.execute("DROP TABLE decision_execution_outcomes")
            connection.execute("DROP TABLE decision_notification_deliveries")
            connection.execute("DELETE FROM schema_migrations WHERE version >= 7")

        initialized = run_cli(self.home, "init")
        delivered_after = self._run_cli(
            "decision", "show", str(self.decision["id"]), "--json"
        )["decision"]["notification_delivery"]

        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.assertEqual(delivered_before["status"], "delivered")
        self.assertEqual(delivered_after, delivered_before)

    async def test_schema_seven_migration_preserves_pending_decisions(self) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("DROP TABLE pi_mcp_execution_recovery")
            connection.execute("DROP TABLE decision_execution_fts")
            connection.execute("DROP TABLE decision_execution_outcomes")
            connection.execute("DELETE FROM schema_migrations WHERE version >= 8")

        initialized = run_cli(self.home, "init")
        shown = self._run_cli(
            "decision", "show", str(self.decision["id"]), "--json"
        )["decision"]
        status = self._run_cli("status", "--json")

        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.assertEqual(shown, self.decision)
        self.assertEqual(status["database"]["schema_version"], 9)

    async def test_invalid_notification_destination_is_not_retried_silently(self) -> None:
        with self.assertRaises(NotificationDestinationError):
            await listen_for_decisions(
                RecordedUpdateSource([]),
                InvalidDestinationDecisionClient(),
                self.settings,
                self.database_path,
                RecordingOneShotTrigger(),
                Redactor(),
                max_batches=0,
            )

        shown = self._run_cli(
            "decision", "show", str(self.decision["id"]), "--json"
        )["decision"]
        self.assertEqual(
            shown["notification_delivery"],
            {
                "attempts": 0,
                "delivered_at": None,
                "last_attempt_at": None,
                "last_error": None,
                "status": "pending",
            },
        )

    async def test_notification_failure_is_retryable_and_does_not_duplicate_the_decision(self) -> None:
        await listen_for_decisions(
            RecordedUpdateSource([]),
            FailingDecisionClient(),
            self.settings,
            self.database_path,
            RecordingOneShotTrigger(),
            Redactor.from_secret_values(["private Telegram transport diagnostics"]),
            max_batches=0,
        )

        failed = self._run_cli(
            "decision", "show", str(self.decision["id"]), "--json"
        )["decision"]
        self.assertEqual(failed["status"], "pending")
        self.assertEqual(
            failed["notification_delivery"],
            {
                "attempts": 1,
                "delivered_at": None,
                "last_attempt_at": failed["notification_delivery"]["last_attempt_at"],
                "last_error": "notification delivery failed",
                "status": "retryable-failure",
            },
        )
        self.assertIsNotNone(failed["notification_delivery"]["last_attempt_at"])
        self.assertNotIn(
            "private Telegram transport diagnostics",
            json.dumps(failed),
        )
        self.assertEqual(len(self._run_cli("decisions", "--json")["decisions"]), 1)

        messages = RecordingDecisionClient()
        await listen_for_decisions(
            RecordedUpdateSource([]),
            messages,
            self.settings,
            self.database_path,
            RecordingOneShotTrigger(),
            Redactor(),
            max_batches=0,
        )

        delivered = self._run_cli(
            "decision", "show", str(self.decision["id"]), "--json"
        )["decision"]
        self.assertEqual(delivered["status"], "pending")
        self.assertEqual(delivered["notification_delivery"]["status"], "delivered")
        self.assertEqual(delivered["notification_delivery"]["attempts"], 2)
        self.assertIsNotNone(delivered["notification_delivery"]["delivered_at"])
        self.assertIsNone(delivered["notification_delivery"]["last_error"])
        self.assertEqual(len(messages.interactive_messages), 1)
        self.assertEqual(len(self._run_cli("decisions", "--json")["decisions"]), 1)

    async def test_pending_decision_is_sent_with_compact_inline_controls(self) -> None:
        updates = FakeTelegramClient(update_batches=[[]])
        messages = RecordingDecisionClient()
        trigger = RecordingOneShotTrigger()

        await listen_for_decisions(
            updates,
            messages,
            self.settings,
            self.database_path,
            trigger,
            Redactor.from_secret_values(["private-plan-evidence"]),
            max_batches=1,
        )

        self.assertEqual(len(messages.interactive_messages), 1)
        destination, text, buttons, thread_id, _ = messages.interactive_messages[0]
        self.assertEqual(destination, USER_ID)
        self.assertIsNone(thread_id)
        self.assertEqual(
            text,
            """RunTasks needs your decision

Task: Approved adapter update
Installed version: 2.26.1
Proposed stable version: 2.27.0
Category: credential-oauth
Confidence: high
Reason: OAuth credential handling needs a reviewed update.

Exact operation:
install-exact-version via pi_mcp_adapter

Procedure:
- installed_version: 2.26.1
- restart_service: pi-web.service
- target_version: 2.27.0

Validation:
Require exact MCP_ADAPTER_OK validation.

Rollback:
Restore exact version 2.26.1 on failure.

Approval authorizes only this exact stored plan.""",
        )
        decision_reference = str(self.decision["id"])[4:]
        self.assertEqual(
            buttons,
            (
                TelegramDecisionButton(
                    "1. APPROVE", f"rt1:{decision_reference}:a"
                ),
                TelegramDecisionButton(
                    "2. REJECT", f"rt1:{decision_reference}:r"
                ),
                TelegramDecisionButton(
                    "3. DETAILS", f"rt1:{decision_reference}:d"
                ),
            ),
        )
        self.assertTrue(all(len(button.callback_data.encode("utf-8")) <= 64 for button in buttons))
        self.assertTrue(all("private-plan-evidence" not in button.callback_data for button in buttons))
        self.assertEqual(messages.messages, [])
        self.assertEqual(trigger.requests, 0)

    async def test_malformed_unknown_and_unauthorized_callbacks_leave_decision_pending(self) -> None:
        decision_reference = str(self.decision["id"])[4:]
        callbacks = [
            TelegramCallbackRecord(
                "malformed",
                USER_ID,
                USER_ID,
                "private",
                100,
                "approve-everything",
            ),
            TelegramCallbackRecord(
                "unauthorized-user",
                112233445,
                USER_ID,
                "private",
                100,
                f"rt1:{decision_reference}:a",
            ),
            TelegramCallbackRecord(
                "unauthorized-chat",
                USER_ID,
                112233445,
                "private",
                100,
                f"rt1:{decision_reference}:r",
            ),
            TelegramCallbackRecord(
                "unknown-reference",
                USER_ID,
                USER_ID,
                "private",
                100,
                "rt1:000000000000000000000000:a",
            ),
            TelegramCallbackRecord(
                "expired-message",
                USER_ID,
                USER_ID,
                "private",
                999,
                f"rt1:{decision_reference}:a",
            ),
        ]
        updates = RecordedUpdateSource(
            [[
                TelegramUpdateRecord(201 + index, None, callback)
                for index, callback in enumerate(callbacks)
            ]]
        )
        messages = RecordingDecisionClient()
        trigger = RecordingOneShotTrigger()

        await listen_for_decisions(
            updates,
            messages,
            self.settings,
            self.database_path,
            trigger,
            Redactor(),
            max_batches=1,
        )

        shown = self._run_cli(
            "decision", "show", str(self.decision["id"]), "--json"
        )["decision"]
        self.assertEqual(shown["status"], "pending")
        self.assertIsNone(shown["response"])
        self.assertEqual(len(messages.interactive_messages), 1)
        self.assertEqual(messages.messages, [])
        self.assertEqual(trigger.requests, 0)
        self.assertEqual(
            messages.callback_answers,
            [
                ("malformed", "This RunTasks control is invalid.", True),
                (
                    "unauthorized-user",
                    "You are not authorized to respond to this Decision.",
                    True,
                ),
                (
                    "unauthorized-chat",
                    "You are not authorized to respond to this Decision.",
                    True,
                ),
                (
                    "unknown-reference",
                    "This Decision is unknown or expired.",
                    True,
                ),
                (
                    "expired-message",
                    "This Decision is unknown or expired.",
                    True,
                ),
            ],
        )

    async def test_repeated_approval_requests_at_most_one_separate_execution(self) -> None:
        decision_reference = str(self.decision["id"])[4:]
        callbacks = [
            TelegramUpdateRecord(
                205 + index,
                message=None,
                callback=TelegramCallbackRecord(
                    callback_id=f"callback-approve-{index}",
                    user_id=USER_ID,
                    chat_id=USER_ID,
                    chat_type="private",
                    message_id=100,
                    data=f"rt1:{decision_reference}:a",
                ),
            )
            for index in range(3)
        ]
        messages = RecordingDecisionClient()
        trigger = RecordingOneShotTrigger()

        await listen_for_decisions(
            RecordedUpdateSource([callbacks]),
            messages,
            self.settings,
            self.database_path,
            trigger,
            Redactor(),
            max_batches=1,
        )

        shown = self._run_cli(
            "decision", "show", str(self.decision["id"]), "--json"
        )["decision"]
        history = self._run_cli(
            "history", str(self.task["id"]), "--json"
        )["runs"]
        self.assertEqual(shown["status"], "approved")
        self.assertEqual(shown["response"]["channel"], "telegram")
        self.assertEqual(shown["response"]["responded_by"], str(USER_ID))
        approval_runs = [run for run in history if run["trigger"] == "approval"]
        self.assertEqual(len(approval_runs), 1)
        self.assertEqual(trigger.requests, 1)
        self.assertEqual(
            messages.messages,
            [
                (
                    USER_ID,
                    """RunTasks approval recorded

Task: Approved adapter update
The exact stored plan is approved.
One-shot processing was requested.
The Telegram listener did not execute the handler.""",
                    None,
                )
            ],
        )
        self.assertEqual(
            messages.callback_answers,
            [
                (
                    "callback-approve-0",
                    "Decision approved. One-shot processing requested.",
                    False,
                ),
                (
                    "callback-approve-1",
                    "Decision was already approved.",
                    False,
                ),
                (
                    "callback-approve-2",
                    "Decision was already approved.",
                    False,
                ),
            ],
        )

    async def test_cli_and_telegram_approval_share_transition_and_idempotency_rules(self) -> None:
        messages = RecordingDecisionClient()
        await listen_for_decisions(
            RecordedUpdateSource([]),
            messages,
            self.settings,
            self.database_path,
            RecordingOneShotTrigger(),
            Redactor(),
            max_batches=0,
        )
        approved = self._run_cli(
            "decision", "approve", str(self.decision["id"]), "--json"
        )["decision"]
        callback = TelegramCallbackRecord(
            "callback-after-cli",
            USER_ID,
            USER_ID,
            "private",
            100,
            f"rt1:{str(self.decision['id'])[4:]}:a",
        )
        trigger = RecordingOneShotTrigger()

        await listen_for_decisions(
            RecordedUpdateSource([[TelegramUpdateRecord(225, None, callback)]]),
            messages,
            self.settings,
            self.database_path,
            trigger,
            Redactor(),
            max_batches=1,
        )

        shown = self._run_cli(
            "decision", "show", str(self.decision["id"]), "--json"
        )["decision"]
        history = self._run_cli(
            "history", str(self.task["id"]), "--json"
        )["runs"]
        self.assertEqual(shown, approved)
        self.assertEqual(len([run for run in history if run["trigger"] == "approval"]), 1)
        self.assertEqual(trigger.requests, 1)
        self.assertEqual(
            messages.callback_answers,
            [
                (
                    "callback-after-cli",
                    "Decision was already approved.",
                    False,
                )
            ],
        )

    async def test_expired_approval_after_task_removal_preserves_pending_state(self) -> None:
        messages = RecordingDecisionClient()
        await listen_for_decisions(
            RecordedUpdateSource([]),
            messages,
            self.settings,
            self.database_path,
            RecordingOneShotTrigger(),
            Redactor(),
            max_batches=0,
        )
        removed = run_cli(self.home, "task", "remove", str(self.task["id"]))
        self.assertEqual(removed.returncode, 0, removed.stderr)
        callback = TelegramCallbackRecord(
            "callback-expired-approval",
            USER_ID,
            USER_ID,
            "private",
            100,
            f"rt1:{str(self.decision['id'])[4:]}:a",
        )
        trigger = RecordingOneShotTrigger()

        await listen_for_decisions(
            RecordedUpdateSource([[TelegramUpdateRecord(228, None, callback)]]),
            messages,
            self.settings,
            self.database_path,
            trigger,
            Redactor(),
            max_batches=1,
        )

        shown = self._run_cli(
            "decision", "show", str(self.decision["id"]), "--json"
        )["decision"]
        self.assertEqual(shown["status"], "pending")
        self.assertIsNone(shown["response"])
        self.assertEqual(trigger.requests, 0)
        self.assertEqual(
            messages.callback_answers,
            [
                (
                    "callback-expired-approval",
                    "This Decision control has expired or is out of order; current state was preserved.",
                    True,
                )
            ],
        )

    async def test_out_of_order_callbacks_report_current_state_without_changing_it(self) -> None:
        decision_reference = str(self.decision["id"])[4:]
        actions = ("a", "r", "d")
        callbacks = [
            TelegramUpdateRecord(
                230 + index,
                None,
                TelegramCallbackRecord(
                    f"callback-{action}",
                    USER_ID,
                    USER_ID,
                    "private",
                    100,
                    f"rt1:{decision_reference}:{action}",
                ),
            )
            for index, action in enumerate(actions)
        ]
        messages = RecordingDecisionClient()
        trigger = RecordingOneShotTrigger()

        await listen_for_decisions(
            RecordedUpdateSource([callbacks]),
            messages,
            self.settings,
            self.database_path,
            trigger,
            Redactor(),
            max_batches=1,
        )

        shown = self._run_cli(
            "decision", "show", str(self.decision["id"]), "--json"
        )["decision"]
        self.assertEqual(shown["status"], "approved")
        self.assertEqual(trigger.requests, 1)
        self.assertEqual(len(messages.interactive_messages), 1)
        self.assertEqual(
            messages.callback_answers,
            [
                (
                    "callback-a",
                    "Decision approved. One-shot processing requested.",
                    False,
                ),
                (
                    "callback-r",
                    "Decision is already approved; rejection is out of order.",
                    True,
                ),
                ("callback-d", "Decision is already approved.", False),
            ],
        )

    async def test_trigger_failure_keeps_committed_approval_queued_and_redacted(self) -> None:
        decision_reference = str(self.decision["id"])[4:]
        callback = TelegramCallbackRecord(
            "callback-trigger-failure",
            USER_ID,
            USER_ID,
            "private",
            100,
            f"rt1:{decision_reference}:a",
        )
        messages = RecordingDecisionClient()
        trigger = FailingOneShotTrigger()

        await listen_for_decisions(
            RecordedUpdateSource([[TelegramUpdateRecord(240, None, callback)]]),
            messages,
            self.settings,
            self.database_path,
            trigger,
            Redactor.from_secret_values(["private systemctl diagnostics"]),
            max_batches=1,
        )

        shown = self._run_cli(
            "decision", "show", str(self.decision["id"]), "--json"
        )["decision"]
        self.assertEqual(shown["status"], "approved")
        self.assertEqual(trigger.requests, 1)
        self.assertEqual(
            messages.messages,
            [
                (
                    USER_ID,
                    f"""RunTasks approval recorded

Task: Approved adapter update
The exact stored plan is approved.
One-shot processing could not be requested automatically.
Approval Run {shown["approval_run_id"]} remains queued.
The Telegram listener did not execute the handler.""",
                    None,
                )
            ],
        )
        self.assertEqual(
            messages.callback_answers,
            [
                (
                    "callback-trigger-failure",
                    "Decision approved, but one-shot processing could not be requested.",
                    True,
                )
            ],
        )
        self.assertNotIn(
            "private systemctl diagnostics",
            repr(messages.messages) + repr(messages.callback_answers),
        )

        recovery_trigger = RecordingOneShotTrigger()
        await listen_for_decisions(
            RecordedUpdateSource([]),
            RecordingDecisionClient(),
            self.settings,
            self.database_path,
            recovery_trigger,
            Redactor(),
            max_batches=0,
        )
        already_requested_trigger = RecordingOneShotTrigger()
        await listen_for_decisions(
            RecordedUpdateSource([]),
            RecordingDecisionClient(),
            self.settings,
            self.database_path,
            already_requested_trigger,
            Redactor(),
            max_batches=0,
        )
        self.assertEqual(recovery_trigger.requests, 1)
        self.assertEqual(already_requested_trigger.requests, 0)

    async def test_repeated_rejection_closes_the_decision_without_execution(self) -> None:
        decision_reference = str(self.decision["id"])[4:]
        callbacks = [
            TelegramUpdateRecord(
                210 + index,
                message=None,
                callback=TelegramCallbackRecord(
                    callback_id=f"callback-reject-{index}",
                    user_id=USER_ID,
                    chat_id=USER_ID,
                    chat_type="private",
                    message_id=100,
                    data=f"rt1:{decision_reference}:r",
                ),
            )
            for index in range(2)
        ]
        messages = RecordingDecisionClient()
        trigger = RecordingOneShotTrigger()

        await listen_for_decisions(
            RecordedUpdateSource([callbacks]),
            messages,
            self.settings,
            self.database_path,
            trigger,
            Redactor(),
            max_batches=1,
        )

        shown = self._run_cli(
            "decision", "show", str(self.decision["id"]), "--json"
        )["decision"]
        history = self._run_cli(
            "history", str(self.task["id"]), "--json"
        )["runs"]
        self.assertEqual(shown["status"], "rejected")
        self.assertEqual(shown["response"]["channel"], "telegram")
        self.assertEqual(shown["response"]["responded_by"], str(USER_ID))
        self.assertEqual(len(history), 1)
        self.assertEqual(trigger.requests, 0)
        self.assertEqual(
            messages.messages,
            [
                (
                    USER_ID,
                    """RunTasks Decision rejected

Task: Approved adapter update
No execution was requested.""",
                    None,
                )
            ],
        )
        self.assertEqual(
            messages.callback_answers,
            [
                (
                    "callback-reject-0",
                    "Decision rejected. No execution was requested.",
                    False,
                ),
                (
                    "callback-reject-1",
                    "Decision was already rejected.",
                    False,
                ),
            ],
        )

    async def test_large_details_deliver_all_evidence_with_controls_within_telegram_limits(self) -> None:
        large_evidence = (
            "intervening release evidence "
            + ("x" * 6_000)
            + " terminal-evidence-marker"
        )
        plan = copy.deepcopy(self.decision["plan"])
        plan["evidence"] = {"all_intervening_releases": large_evidence}
        outcome = {
            "status": "decision-required",
            "summary": "Large release evidence requires approval.",
            "details": {"assessment": "large evidence"},
            "decision": {
                "plan": plan,
                "reason": self.decision["reason"],
                "validation_summary": self.decision["validation_summary"],
                "rollback_summary": self.decision["rollback_summary"],
            },
        }
        executed = self._run_cli(
            "run",
            str(self.task["id"]),
            "--json",
            extra_environment={
                "RUNTASKS_FIXTURE_HANDLER_OUTCOME": json.dumps(outcome)
            },
        )["run"]
        large_decision = next(
            decision
            for decision in self._run_cli("decisions", "--json")["decisions"]
            if decision["run_id"] == executed["id"]
        )
        messages = RecordingDecisionClient()
        await listen_for_decisions(
            RecordedUpdateSource([]),
            messages,
            self.settings,
            self.database_path,
            RecordingOneShotTrigger(),
            Redactor(),
            max_batches=0,
        )
        initial_message_count = len(messages.interactive_messages)
        large_initial = next(
            message
            for message in messages.interactive_messages
            if message[2][0].callback_data
            == f"rt1:{str(large_decision['id'])[4:]}:a"
        )
        callback = TelegramCallbackRecord(
            callback_id="callback-large-details",
            user_id=USER_ID,
            chat_id=USER_ID,
            chat_type="private",
            message_id=large_initial[4],
            data=f"rt1:{str(large_decision['id'])[4:]}:d",
        )

        await listen_for_decisions(
            RecordedUpdateSource([[TelegramUpdateRecord(260, None, callback)]]),
            messages,
            self.settings,
            self.database_path,
            RecordingOneShotTrigger(),
            Redactor(),
            max_batches=1,
        )

        detail_plain_chunks = [text for _, text, _ in messages.messages]
        detail_interactive_chunks = [
            message[1]
            for message in messages.interactive_messages[initial_message_count:]
        ]
        detail_chunks = [*detail_plain_chunks, *detail_interactive_chunks]
        self.assertGreater(len(detail_chunks), 1)
        self.assertTrue(
            all(len(chunk.encode("utf-16-le")) // 2 <= 3_800 for chunk in detail_chunks)
        )
        combined_details = "".join(detail_chunks)
        self.assertIn(large_evidence, combined_details)
        self.assertIn("terminal-evidence-marker", combined_details)
        self.assertEqual(
            messages.interactive_messages[-1][2],
            large_initial[2],
        )
        self.assertEqual(
            messages.callback_answers[-1],
            ("callback-large-details", "Decision details sent.", False),
        )

    async def test_details_send_expanded_redacted_evidence_and_repeat_controls(self) -> None:
        decision_reference = str(self.decision["id"])[4:]
        callback = TelegramCallbackRecord(
            callback_id="callback-details",
            user_id=USER_ID,
            chat_id=USER_ID,
            chat_type="private",
            message_id=100,
            data=f"rt1:{decision_reference}:d",
        )
        updates = RecordedUpdateSource(
            [[TelegramUpdateRecord(200, message=None, callback=callback)]]
        )
        messages = RecordingDecisionClient()

        await listen_for_decisions(
            updates,
            messages,
            self.settings,
            self.database_path,
            RecordingOneShotTrigger(),
            Redactor.from_secret_values(["private-plan-evidence"]),
            max_batches=1,
        )

        self.assertEqual(len(messages.interactive_messages), 2)
        _, details, detail_buttons, _, _ = messages.interactive_messages[1]
        self.assertEqual(
            details,
            f"""RunTasks Decision details

Task: Approved adapter update
Task ID: {self.task["id"]}
Run ID: {self.decision["run_id"]}
Installed version: 2.26.1
Proposed stable version: 2.27.0
Reason: OAuth credential handling needs a reviewed update.

Plan hash:
{self.decision["plan_hash"]}

Assessment:
{{
  "category": "credential-oauth",
  "confidence": "high",
  "importance": "important",
  "reason": "OAuth credential handling needs a reviewed update.",
  "recommendation": "Install the exact proposed version after approval."
}}

Exact operation:
install-exact-version via pi_mcp_adapter

Procedure:
{{
  "installed_version": "2.26.1",
  "restart_service": "pi-web.service",
  "target_version": "2.27.0"
}}

Intervening-release evidence:
{{
  "private_note": "[REDACTED]",
  "release": "Security and OAuth correction"
}}

Validation summary:
Require exact MCP_ADAPTER_OK validation.

Validation plan:
{{
  "expected_result": "MCP_ADAPTER_OK",
  "health_check": "Pi Web"
}}

Rollback summary:
Restore exact version 2.26.1 on failure.

Rollback plan:
{{
  "target_version": "2.26.1"
}}

Choose 1. APPROVE, 2. REJECT, or 3. DETAILS below.""",
        )
        self.assertEqual(detail_buttons, messages.interactive_messages[0][2])
        self.assertEqual(
            messages.callback_answers,
            [("callback-details", "Decision details sent.", False)],
        )
        self.assertNotIn("private-plan-evidence", details)


if __name__ == "__main__":
    unittest.main()
