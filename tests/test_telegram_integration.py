from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from runtasks.notifications import (
    NotificationDeliveryError,
    RedactingNotificationClient,
)
from runtasks.telegram import (
    SetupCandidate,
    TelegramConfigurationError,
    TelegramDestination,
    TelegramPoller,
    build_telegram_notification_client,
    listen_for_authorization_checks,
    load_telegram_settings,
    send_test_notification,
)
from tests.recorded_telegram import load_recorded_updates
from tests.telegram_fakes import FakeNotificationClient, FakeTelegramClient


TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"


class TelegramIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.lock_path = (
            Path(self._temporary_directory.name) / "telegram-poller.lock"
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_setup_reads_recorded_official_updates_and_returns_numeric_ids(self) -> None:
        updates = load_recorded_updates()
        client = FakeTelegramClient(
            update_batches=[[], [updates[0]], updates[1:]]
        )
        readiness: list[str] = []

        discovery = await TelegramPoller(
            self.lock_path, client
        ).discover_setup_candidates(
            timeout_seconds=30,
            on_ready=lambda: readiness.append("ready"),
        )

        self.assertEqual(readiness, ["ready"])
        self.assertEqual(
            client.poll_requests,
            [(0, -1), (30, None), (30, 9101)],
        )
        self.assertEqual(
            discovery.candidates,
            (
                SetupCandidate(
                    user_id=998877665,
                    chat_id=998877665,
                    chat_type="private",
                ),
                SetupCandidate(
                    user_id=112233445,
                    chat_id=-1002233445566,
                    chat_type="supergroup",
                ),
            ),
        )

    async def test_setup_discards_stale_pending_start_updates_before_waiting(self) -> None:
        stale = {
            "update_id": 40,
            "message": {
                "text": "/start",
                "from": {"id": 112233445},
                "chat": {"id": 112233445, "type": "private"},
            },
        }
        current = {
            "update_id": 41,
            "message": {
                "text": "/start",
                "from": {"id": 998877665},
                "chat": {"id": 998877665, "type": "private"},
            },
        }
        client = FakeTelegramClient(update_batches=[[stale], [current]])

        discovery = await TelegramPoller(
            self.lock_path, client
        ).discover_setup_candidates(timeout_seconds=30)

        self.assertEqual(client.poll_requests, [(0, -1), (30, 41)])
        self.assertEqual(
            discovery.candidates,
            (SetupCandidate(998877665, 998877665, "private"),),
        )

    async def test_setup_ignores_start_commands_addressed_to_another_bot(self) -> None:
        wrong_bot = {
            "update_id": 50,
            "message": {
                "text": "/start@another_bot",
                "from": {"id": 112233445},
                "chat": {"id": -1002233445566, "type": "supergroup"},
            },
        }
        configured_bot = {
            "update_id": 51,
            "message": {
                "text": "/start@runtasks_bot",
                "from": {"id": 998877665},
                "chat": {"id": 998877665, "type": "private"},
            },
        }
        client = FakeTelegramClient(
            update_batches=[[], [wrong_bot], [configured_bot]]
        )

        discovery = await TelegramPoller(
            self.lock_path, client
        ).discover_setup_candidates(timeout_seconds=30)

        self.assertEqual(
            discovery.candidates,
            (SetupCandidate(998877665, 998877665, "private"),),
        )

    async def test_setup_rejects_webhook_mode_instead_of_opening_an_inbound_endpoint(self) -> None:
        client = FakeTelegramClient(webhook_url="https://private.example/hook?token=secret")

        with self.assertRaises(TelegramConfigurationError) as caught:
            await TelegramPoller(
                self.lock_path, client
            ).discover_setup_candidates(timeout_seconds=30)

        self.assertNotIn("private.example", str(caught.exception))
        self.assertEqual(client.poll_requests, [])

    async def test_guarded_listener_handles_only_authorized_start_checks(self) -> None:
        authorized_start = {
            "update_id": 60,
            "message": {
                "text": "/start",
                "from": {"id": 998877665},
                "chat": {"id": 998877665, "type": "private"},
            },
        }
        settings = load_telegram_settings(
            {
                "RUNTASKS_TELEGRAM_BOT_TOKEN": TOKEN,
                "RUNTASKS_TELEGRAM_ALLOWED_USER_IDS": "998877665",
                "RUNTASKS_TELEGRAM_NOTIFICATION_CHAT_ID": "998877665",
            },
            require_destination=True,
        )
        update_client = FakeTelegramClient(update_batches=[[authorized_start]])
        authorized_events: list[str] = []
        poller = TelegramPoller(self.lock_path, update_client)

        async with poller.session() as session:
            await listen_for_authorization_checks(
                session,
                settings,
                on_authorized=lambda: authorized_events.append("authorized"),
                max_batches=1,
            )

        self.assertEqual(authorized_events, ["authorized"])

    async def test_test_notification_uses_a_fake_application_client_and_redacts_text(self) -> None:
        raw_client = FakeNotificationClient()
        client = RedactingNotificationClient(
            raw_client,
            sensitive_values=(TOKEN, "private-environment-value"),
        )

        await send_test_notification(client)
        await client.send(
            text=(
                "Failure used "
                f"https://api.telegram.org/bot{TOKEN}/sendMessage "
                "with RUNTASKS_PRIVATE=private-environment-value"
            )
        )

        self.assertEqual(
            raw_client.messages[0],
            "RunTasks Telegram test succeeded. No credentials or task data were sent.",
        )
        leaked_message = raw_client.messages[1]
        self.assertNotIn(TOKEN, leaked_message)
        self.assertNotIn("private-environment-value", leaked_message)
        self.assertNotIn("/sendMessage", leaked_message)
        self.assertIn("[REDACTED]", leaked_message)

    async def test_notification_interface_contains_transport_exception_secrets(self) -> None:
        client = RedactingNotificationClient(
            FakeNotificationClient(
                failure=RuntimeError(
                    f"failed at https://api.telegram.org/bot{TOKEN}/sendMessage"
                )
            ),
            sensitive_values=(TOKEN,),
        )

        with self.assertRaises(NotificationDeliveryError) as caught:
            await client.send(text="safe message")

        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

        preclassified = RedactingNotificationClient(
            FakeNotificationClient(
                failure=NotificationDeliveryError(
                    f"failed at https://api.telegram.org/bot{TOKEN}/sendMessage"
                )
            ),
            sensitive_values=(TOKEN,),
        )
        with self.assertRaises(NotificationDeliveryError) as classified:
            await preclassified.send(text="safe message")
        self.assertNotIn(TOKEN, str(classified.exception))
        self.assertNotIn("api.telegram.org", str(classified.exception))

    async def test_telegram_notification_adapter_binds_transport_addressing(self) -> None:
        raw_client = FakeTelegramClient()
        client = build_telegram_notification_client(
            raw_client,
            TelegramDestination(998877665, None),
            sensitive_values=(TOKEN,),
        )

        await client.send(
            text=f"safe message; leaked URL https://api.telegram.org/bot{TOKEN}/send"
        )

        self.assertEqual(raw_client.sent[0][0], 998877665)
        self.assertNotIn(TOKEN, raw_client.sent[0][1])
        self.assertNotIn("api.telegram.org", raw_client.sent[0][1])
        self.assertIsNone(raw_client.sent[0][2])


if __name__ == "__main__":
    unittest.main()
