from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from typing import Iterator, Mapping, cast

from tests.cli_test_support import PROJECT_ROOT, run_cli as run_cli_process
from tests.recorded_telegram import load_recorded_updates


SUBPROCESS_FAKES = Path(__file__).parent / "subprocess_fakes"
TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"


@contextmanager
def telegram_sandbox() -> Iterator[tuple[Path, Path]]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        yield root / "runtime-home", root / "telegram-state.json"


class TelegramCliTests(unittest.TestCase):
    def run_cli(
        self,
        home: Path,
        *arguments: str,
        environment: Mapping[str, str],
        telegram_state: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        values = dict(environment)
        if telegram_state is not None:
            existing_python_path = os.environ.get("PYTHONPATH")
            python_paths = [str(SUBPROCESS_FAKES), str(PROJECT_ROOT)]
            if existing_python_path:
                python_paths.append(existing_python_path)
            values["PYTHONPATH"] = os.pathsep.join(python_paths)
            values["TELEGRAM_FAKE_STATE"] = str(telegram_state)
            values["TELEGRAM_FAKE_USER_HOME"] = str(home.parent)
        return run_cli_process(
            home,
            *arguments,
            extra_environment=values,
        )

    def write_state(self, path: Path, **overrides: object) -> None:
        state: dict[str, object] = {
            "chat_type": "private",
            "fail_send": False,
            "invalid_thread_ids": [],
            "is_forum": False,
            "member_statuses": {},
            "sent_messages": [],
            "update_batches": [],
            "webhook_url": "",
        }
        state.update(overrides)
        path.write_text(json.dumps(state), encoding="utf-8")

    def read_state(self, path: Path) -> dict[str, object]:
        value: object = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            self.fail("fake Telegram state must be an object")
        return cast(dict[str, object], value)

    def test_setup_discovers_numeric_ids_before_authorization_is_configured(self) -> None:
        updates = load_recorded_updates()
        with telegram_sandbox() as (home, state):
            self.write_state(state, update_batches=[[], [updates[1]]])
            result = self.run_cli(
                home,
                "telegram",
                "setup",
                "--json",
                environment={"RUNTASKS_TELEGRAM_BOT_TOKEN": TOKEN},
                telegram_state=state,
            )

        candidate = json.loads(result.stdout)["candidates"][0]
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(candidate["user_id"], 998877665)
        self.assertEqual(candidate["chat_id"], 998877665)

    def test_setup_reports_ids_and_verifies_matching_authorization_as_json(self) -> None:
        updates = load_recorded_updates()
        with telegram_sandbox() as (home, state):
            self.write_state(state, update_batches=[[], [updates[2], updates[1]]])

            result = self.run_cli(
                home,
                "--json",
                "telegram",
                "setup",
                "--timeout",
                "5",
                environment={
                    "RUNTASKS_TELEGRAM_BOT_TOKEN": TOKEN,
                    "RUNTASKS_TELEGRAM_ALLOWED_USER_IDS": "998877665",
                    "RUNTASKS_TELEGRAM_NOTIFICATION_CHAT_ID": "998877665",
                },
                telegram_state=state,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "candidates": [
                    {
                        "verification": {
                            "authorized": True,
                            "chat_matches": True,
                            "user_allowed": True,
                        },
                        "chat_id": "[configured]",
                        "chat_type": "private",
                        "user_id": "[configured]",
                    }
                ],
                "mode": "long-polling",
                "status": "ok",
            },
        )
        self.assertIn("Send /start", result.stderr)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)

    def test_setup_rejects_an_authorization_mismatch(self) -> None:
        wrong_private_user = {
            "update_id": 9200,
            "message": {
                "message_id": 18,
                "date": 1787302803,
                "chat": {"id": 112233445, "type": "private"},
                "from": {
                    "id": 112233445,
                    "is_bot": False,
                    "first_name": "Other operator",
                },
                "text": "/start",
            },
        }
        with telegram_sandbox() as (home, state):
            self.write_state(state, update_batches=[[], [wrong_private_user]])

            result = self.run_cli(
                home,
                "telegram",
                "setup",
                "--json",
                environment={
                    "RUNTASKS_TELEGRAM_BOT_TOKEN": TOKEN,
                    "RUNTASKS_TELEGRAM_ALLOWED_USER_IDS": "998877665",
                    "RUNTASKS_TELEGRAM_NOTIFICATION_CHAT_ID": "998877665",
                },
                telegram_state=state,
            )

        self.assertEqual(result.returncode, 2)
        mismatch_payload = json.loads(result.stdout)
        self.assertEqual(mismatch_payload["status"], "authorization-mismatch")
        self.assertEqual(mismatch_payload["candidates"][0]["user_id"], 112233445)
        self.assertFalse(
            mismatch_payload["candidates"][0]["verification"]["authorized"]
        )
        self.assertNotIn(TOKEN, result.stdout + result.stderr)

    def test_test_command_reports_success_and_failure_with_exit_status_and_json(self) -> None:
        configuration = {
            "RUNTASKS_TELEGRAM_BOT_TOKEN": TOKEN,
            "RUNTASKS_TELEGRAM_ALLOWED_USER_IDS": "998877665",
            "RUNTASKS_TELEGRAM_NOTIFICATION_CHAT_ID": "998877665",
        }
        with telegram_sandbox() as (home, state):
            self.write_state(state)

            success = self.run_cli(
                home,
                "telegram",
                "test",
                "--json",
                environment=configuration,
                telegram_state=state,
            )

            self.assertEqual(success.returncode, 0, success.stderr)
            self.assertEqual(
                json.loads(success.stdout),
                {"status": "sent", "transport": "telegram"},
            )
            successful_state = self.read_state(state)
            raw_sent_messages = successful_state["sent_messages"]
            self.assertIsInstance(raw_sent_messages, list)
            sent_messages = cast(list[object], raw_sent_messages)
            self.assertEqual(len(sent_messages), 1)
            outgoing_text = str(sent_messages[0])
            self.assertNotIn(TOKEN, success.stdout + success.stderr + outgoing_text)

            self.write_state(state, fail_send=True)
            failure = self.run_cli(
                home,
                "--json",
                "telegram",
                "test",
                environment=configuration,
                telegram_state=state,
            )

        self.assertEqual(failure.returncode, 1)
        self.assertEqual(json.loads(failure.stdout)["status"], "error")
        self.assertNotIn(TOKEN, failure.stdout + failure.stderr)
        self.assertNotIn("sendMessage", failure.stdout + failure.stderr)

    def test_group_thread_destination_requires_an_allowed_administrator(self) -> None:
        configuration = {
            "RUNTASKS_TELEGRAM_BOT_TOKEN": TOKEN,
            "RUNTASKS_TELEGRAM_ALLOWED_USER_IDS": "998877665",
            "RUNTASKS_TELEGRAM_NOTIFICATION_CHAT_ID": "-1002233445566",
            "RUNTASKS_TELEGRAM_THREAD_ID": "77",
        }
        with telegram_sandbox() as (home, state):
            self.write_state(
                state,
                chat_type="supergroup",
                is_forum=True,
                member_statuses={"998877665": "administrator"},
            )
            result = self.run_cli(
                home,
                "telegram",
                "test",
                "--json",
                environment=configuration,
                telegram_state=state,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            sent_messages = cast(
                list[dict[str, object]],
                self.read_state(state)["sent_messages"],
            )
            self.assertEqual(sent_messages[0]["thread_id"], 77)

            self.write_state(
                state,
                chat_type="supergroup",
                is_forum=True,
                member_statuses={"998877665": "member"},
            )
            unsafe = self.run_cli(
                home,
                "telegram",
                "test",
                "--json",
                environment=configuration,
                telegram_state=state,
            )

        self.assertEqual(unsafe.returncode, 2)
        self.assertEqual(json.loads(unsafe.stdout)["status"], "error")

    def test_setup_reports_an_invalid_configured_thread_after_reading_start(self) -> None:
        updates = load_recorded_updates()
        configuration = {
            "RUNTASKS_TELEGRAM_BOT_TOKEN": TOKEN,
            "RUNTASKS_TELEGRAM_ALLOWED_USER_IDS": "112233445",
            "RUNTASKS_TELEGRAM_NOTIFICATION_CHAT_ID": "-1002233445566",
            "RUNTASKS_TELEGRAM_THREAD_ID": "77",
        }
        with telegram_sandbox() as (home, state):
            self.write_state(
                state,
                chat_type="supergroup",
                invalid_thread_ids=[77],
                is_forum=True,
                member_statuses={"112233445": "administrator"},
                update_batches=[[], [updates[2]]],
            )
            result = self.run_cli(
                home,
                "telegram",
                "setup",
                "--json",
                environment=configuration,
                telegram_state=state,
            )

        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["status"], "destination-invalid")
        self.assertEqual(payload["candidates"][0]["chat_id"], "[configured]")

    def test_init_never_stores_private_telegram_configuration_in_sqlite(self) -> None:
        configuration = {
            "RUNTASKS_TELEGRAM_BOT_TOKEN": TOKEN,
            "RUNTASKS_TELEGRAM_ALLOWED_USER_IDS": "998877665",
            "RUNTASKS_TELEGRAM_NOTIFICATION_CHAT_ID": "998877665",
            "RUNTASKS_TELEGRAM_THREAD_ID": "77112233",
        }
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"

            result = self.run_cli(home, "init", environment=configuration)

            self.assertEqual(result.returncode, 0, result.stderr)
            database = (home / "var" / "data" / "runtasks.sqlite3").read_bytes()
            for private_value in configuration.values():
                self.assertNotIn(private_value.encode("utf-8"), database)

    def test_listener_rejects_unsafe_destination_before_polling(self) -> None:
        configuration = {
            "RUNTASKS_TELEGRAM_BOT_TOKEN": TOKEN,
            "RUNTASKS_TELEGRAM_ALLOWED_USER_IDS": "112233445",
            "RUNTASKS_TELEGRAM_NOTIFICATION_CHAT_ID": "-1002233445566",
        }
        with telegram_sandbox() as (home, state):
            self.write_state(
                state,
                chat_type="supergroup",
                member_statuses={"112233445": "member"},
            )
            result = self.run_cli(
                home,
                "telegram",
                "listen",
                environment=configuration,
                telegram_state=state,
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("allowed user as administrator", result.stderr)

    def test_long_running_listener_rejects_json_mode_before_network_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"
            result = self.run_cli(
                home,
                "--json",
                "telegram",
                "listen",
                environment={
                    "RUNTASKS_TELEGRAM_BOT_TOKEN": TOKEN,
                    "RUNTASKS_TELEGRAM_ALLOWED_USER_IDS": "998877665",
                    "RUNTASKS_TELEGRAM_NOTIFICATION_CHAT_ID": "998877665",
                },
            )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "error")

    def test_invalid_private_configuration_is_nonzero_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime-home"

            result = self.run_cli(
                home,
                "--json",
                "telegram",
                "test",
                environment={
                    "RUNTASKS_TELEGRAM_BOT_TOKEN": TOKEN,
                    "RUNTASKS_TELEGRAM_ALLOWED_USER_IDS": "private-username",
                    "RUNTASKS_TELEGRAM_NOTIFICATION_CHAT_ID": "private-chat-value",
                },
            )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "error")
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        self.assertNotIn("private-username", result.stdout + result.stderr)
        self.assertNotIn("private-chat-value", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
