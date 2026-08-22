from __future__ import annotations

import argparse
import asyncio
from typing import Any, Iterable, Mapping, Sequence

from runtasks.cli_output import print_json, print_text
from runtasks.notifications import (
    NotificationClient,
    NotificationDestinationError,
)
from runtasks.paths import RuntimePaths
from runtasks.redaction import install_redacting_log_filter
from runtasks.telegram import (
    SetupCandidate,
    TelegramConfigurationError,
    TelegramPoller,
    TelegramSettings,
    build_telegram_notification_client,
    listen_for_authorization_checks,
    load_telegram_settings,
    send_test_notification,
)
from runtasks.telegram_transport import PythonTelegramBotClient


EXIT_VALIDATION_ERROR = 2


def add_telegram_parser(
    subparsers: argparse._SubParsersAction[Any],
) -> None:
    telegram_parser = subparsers.add_parser(
        "telegram", help="configure and verify Telegram"
    )
    telegram_actions = telegram_parser.add_subparsers(
        dest="telegram_command", required=True
    )
    setup_parser = telegram_actions.add_parser(
        "setup", help="discover numeric IDs from a new /start update"
    )
    setup_parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="long-poll timeout in seconds (1-60)",
    )
    _add_output_json_flag(setup_parser)
    test_parser = telegram_actions.add_parser(
        "test", help="send a harmless test notification"
    )
    _add_output_json_flag(test_parser)
    telegram_actions.add_parser(
        "listen", help="listen for guarded authorization checks"
    )


def run_telegram_command(
    paths: RuntimePaths,
    secret_values: Mapping[str, str],
    process_redaction_values: Iterable[str],
    options: argparse.Namespace,
) -> int:
    settings = load_telegram_settings(
        secret_values,
        require_destination=options.telegram_command != "setup",
    )
    all_redaction_values = (
        *secret_values.values(),
        *process_redaction_values,
        *settings.redaction_values,
    )
    install_redacting_log_filter(sensitive_values=all_redaction_values)
    raw_client = PythonTelegramBotClient(settings.bot_token)

    if options.telegram_command == "setup":
        return _run_setup(paths, settings, raw_client, options)
    if options.telegram_command == "test":
        client = _configured_notification_client(
            settings,
            raw_client,
            all_redaction_values,
        )
        return _run_test(client, options.as_json)
    if options.telegram_command == "listen":
        if options.as_json:
            raise TelegramConfigurationError(
                "--json is not supported by the long-running Telegram listener"
            )
        return _run_listener(paths, settings, raw_client)
    raise TelegramConfigurationError("unknown Telegram command")


def _run_setup(
    paths: RuntimePaths,
    settings: TelegramSettings,
    raw_client: PythonTelegramBotClient,
    options: argparse.Namespace,
) -> int:
    poller = TelegramPoller(
        paths.telegram_poller_lock_file(settings.bot_token),
        raw_client,
    )
    discovery = asyncio.run(
        poller.discover_setup_candidates(
            timeout_seconds=options.timeout,
            on_ready=lambda: print_text(
                "Send /start to the bot now; waiting for a new update...",
                error=True,
                flush=True,
            ),
            candidate_filter=(
                (
                    lambda candidate: settings.authorizes(
                        candidate.authorization_context
                    )
                )
                if settings.has_authorization_configuration
                else None
            ),
        )
    )
    if not discovery.candidates:
        _render_setup_result(
            discovery.authorization_mismatches,
            settings,
            status="authorization-mismatch",
            as_json=options.as_json,
        )
        return EXIT_VALIDATION_ERROR
    if settings.destination is not None:
        try:
            asyncio.run(raw_client.verify_destination(settings))
        except NotificationDestinationError:
            _render_setup_result(
                discovery.candidates,
                settings,
                status="destination-invalid",
                as_json=options.as_json,
            )
            return EXIT_VALIDATION_ERROR
    _render_setup_result(
        discovery.candidates,
        settings,
        status="ok",
        as_json=options.as_json,
    )
    return 0


def _configured_notification_client(
    settings: TelegramSettings,
    raw_client: PythonTelegramBotClient,
    redaction_values: Iterable[str],
) -> NotificationClient:
    if settings.destination is None:
        raise TelegramConfigurationError(
            "Telegram notification chat ID is missing"
        )
    asyncio.run(raw_client.verify_destination(settings))
    return build_telegram_notification_client(
        raw_client,
        settings.destination,
        sensitive_values=redaction_values,
    )


def _run_test(client: NotificationClient, as_json: bool) -> int:
    asyncio.run(send_test_notification(client))
    if as_json:
        print_json({"status": "sent", "transport": "telegram"})
    else:
        print_text("Telegram test notification sent.")
    return 0


def _run_listener(
    paths: RuntimePaths,
    settings: TelegramSettings,
    raw_client: PythonTelegramBotClient,
) -> int:
    asyncio.run(raw_client.verify_destination(settings))
    poller = TelegramPoller(
        paths.telegram_poller_lock_file(settings.bot_token),
        raw_client,
    )

    async def listen() -> None:
        async with poller.session() as session:
            await listen_for_authorization_checks(
                session,
                settings,
                on_ready=lambda: print_text(
                    "RunTasks Telegram authorization listener started.",
                    flush=True,
                ),
                on_authorized=lambda: print_text(
                    "Authorized Telegram /start received.",
                    flush=True,
                ),
            )

    asyncio.run(listen())
    return 0


def _render_setup_result(
    candidates: Sequence[SetupCandidate],
    settings: TelegramSettings,
    *,
    status: str,
    as_json: bool,
) -> None:
    if as_json:
        print_json(
            {
                "candidates": [
                    _candidate_payload(candidate, settings)
                    for candidate in candidates
                ],
                "mode": "long-polling",
                "status": status,
            },
            public_values=(
                value
                for candidate in candidates
                for value in (candidate.user_id, candidate.chat_id)
            ),
        )
        return
    error_output = status != "ok"
    if error_output:
        heading = (
            "Telegram authorization mismatch:"
            if status == "authorization-mismatch"
            else "Telegram notification destination is invalid:"
        )
        print_text(heading, error=True)
    else:
        print_text(
            "Telegram /start updates found through official Bot API long polling:"
        )
    for candidate in candidates:
        payload = _candidate_payload(candidate, settings)
        print_text(
            f"User ID: {payload['user_id']}  "
            f"Chat ID: {payload['chat_id']}  "
            f"Chat type: {candidate.chat_type}",
            error=error_output,
            public_values=(candidate.user_id, candidate.chat_id),
        )
        if settings.has_authorization_configuration:
            verification = settings.verify_authorization(
                candidate.authorization_context
            )
            result = "verified" if verification.authorized else "mismatch"
            print_text(f"Authorization: {result}", error=error_output)


def _candidate_payload(
    candidate: SetupCandidate,
    settings: TelegramSettings,
) -> dict[str, object]:
    payload = candidate.as_dict()
    if settings.has_authorization_configuration:
        payload["verification"] = settings.verify_authorization(
            candidate.authorization_context
        ).as_dict()
    return payload


def _add_output_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        default=argparse.SUPPRESS,
    )
