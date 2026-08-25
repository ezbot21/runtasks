from __future__ import annotations

import unittest

from runtasks.telegram import (
    TelegramAuthorizationContext,
    TelegramConfigurationError,
    load_telegram_settings,
)


TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"  # release-check: allow-fake-secret


class TelegramConfigurationTests(unittest.TestCase):
    def test_complete_private_dm_configuration_is_loaded_without_persistence(self) -> None:
        settings = load_telegram_settings(
            {
                "RUNTASKS_TELEGRAM_BOT_TOKEN": TOKEN,
                "RUNTASKS_TELEGRAM_ALLOWED_USER_IDS": "998877665,112233445",
                "RUNTASKS_TELEGRAM_NOTIFICATION_CHAT_ID": "998877665",
                "RUNTASKS_TELEGRAM_THREAD_ID": "",
            },
            require_destination=True,
        )

        self.assertEqual(settings.allowed_user_ids, (112233445, 998877665))
        self.assertIsNotNone(settings.destination)
        if settings.destination is None:
            self.fail("private destination must be loaded")
        self.assertEqual(settings.destination.chat_id, 998877665)
        self.assertIsNone(settings.destination.thread_id)
        self.assertNotIn(TOKEN, repr(settings))

    def test_private_authorization_requires_the_configured_chat_id(self) -> None:
        settings = load_telegram_settings(
            {
                "RUNTASKS_TELEGRAM_BOT_TOKEN": TOKEN,
                "RUNTASKS_TELEGRAM_ALLOWED_USER_IDS": "998877665,112233445",
                "RUNTASKS_TELEGRAM_NOTIFICATION_CHAT_ID": "998877665",
            },
            require_destination=True,
        )

        verification = settings.verify_authorization(
            TelegramAuthorizationContext(
                user_id=112233445,
                chat_id=112233445,
                chat_type="private",
            )
        )

        self.assertTrue(verification.user_allowed)
        self.assertFalse(verification.chat_matches)
        self.assertFalse(verification.authorized)

    def test_group_destination_can_load_an_optional_thread_id(self) -> None:
        settings = load_telegram_settings(
            {
                "RUNTASKS_TELEGRAM_BOT_TOKEN": TOKEN,
                "RUNTASKS_TELEGRAM_ALLOWED_USER_IDS": "998877665",
                "RUNTASKS_TELEGRAM_NOTIFICATION_CHAT_ID": "-1002233445566",
                "RUNTASKS_TELEGRAM_THREAD_ID": "77",
            },
            require_destination=True,
        )

        self.assertIsNotNone(settings.destination)
        if settings.destination is None:
            self.fail("group destination must be loaded")
        self.assertEqual(settings.destination.chat_id, -1002233445566)
        self.assertEqual(settings.destination.thread_id, 77)

    def test_setup_requires_only_a_valid_token(self) -> None:
        settings = load_telegram_settings(
            {"RUNTASKS_TELEGRAM_BOT_TOKEN": TOKEN},
            require_destination=False,
        )

        self.assertEqual(settings.allowed_user_ids, ())
        self.assertIsNone(settings.destination)

    def test_missing_token_malformed_allowlist_and_unsafe_destinations_are_rejected(self) -> None:
        cases: tuple[tuple[dict[str, str], bool], ...] = (
            ({}, False),
            (
                {
                    "RUNTASKS_TELEGRAM_BOT_TOKEN": TOKEN,
                    "RUNTASKS_TELEGRAM_THREAD_ID": "77",
                },
                False,
            ),
            (
                {
                    "RUNTASKS_TELEGRAM_BOT_TOKEN": TOKEN,
                    "RUNTASKS_TELEGRAM_NOTIFICATION_CHAT_ID": "998877665",
                },
                False,
            ),
            (
                {
                    "RUNTASKS_TELEGRAM_BOT_TOKEN": TOKEN,
                    "RUNTASKS_TELEGRAM_ALLOWED_USER_IDS": "998877665,username",
                    "RUNTASKS_TELEGRAM_NOTIFICATION_CHAT_ID": "998877665",
                },
                True,
            ),
            (
                {
                    "RUNTASKS_TELEGRAM_BOT_TOKEN": TOKEN,
                    "RUNTASKS_TELEGRAM_ALLOWED_USER_IDS": "112233445",
                    "RUNTASKS_TELEGRAM_NOTIFICATION_CHAT_ID": "998877665",
                },
                True,
            ),
            (
                {
                    "RUNTASKS_TELEGRAM_BOT_TOKEN": TOKEN,
                    "RUNTASKS_TELEGRAM_ALLOWED_USER_IDS": "998877665",
                    "RUNTASKS_TELEGRAM_NOTIFICATION_CHAT_ID": "998877665",
                    "RUNTASKS_TELEGRAM_THREAD_ID": "77",
                },
                True,
            ),
        )

        for values, require_destination in cases:
            with self.subTest(values=values):
                with self.assertRaises(TelegramConfigurationError) as caught:
                    load_telegram_settings(
                        values,
                        require_destination=require_destination,
                    )
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertNotIn("username", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
