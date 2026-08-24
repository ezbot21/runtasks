from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import json
from pathlib import Path
import re
import sqlite3
from typing import Callable, Iterator, Protocol, Sequence

from runtasks.database import LATEST_SCHEMA_VERSION, database_connection
from runtasks.decisions import (
    Decision,
    DecisionError,
    DecisionTransitionError,
    get_decision,
    list_decisions,
    list_pending_approval_run_triggers,
    mark_approval_run_trigger_requested,
    transition_decision,
)
from runtasks.one_shot import OneShotRunTrigger, OneShotRunTriggerError
from runtasks.redaction import Redactor
from runtasks.tasks import get_task
from runtasks.telegram_config import TelegramSettings
from runtasks.telegram_errors import TelegramConfigurationError
from runtasks.telegram_updates import (
    TelegramCallbackRecord,
    TelegramUpdateRecord,
    setup_candidates_from_updates,
    verify_long_polling,
)


_CALLBACK_PREFIX = "rt1"
_CALLBACK_PATTERN = re.compile(r"rt1:(?P<reference>[0-9a-f]{24}):(?P<action>[ard])\Z")
class TelegramDecisionAction(Enum):
    APPROVE = "a"
    REJECT = "r"
    DETAILS = "d"


@dataclass(frozen=True)
class _DecisionResponseMetadata:
    domain_action: str
    target_status: str
    noun: str


_RESPONSE_METADATA = {
    TelegramDecisionAction.APPROVE: _DecisionResponseMetadata(
        domain_action="approve",
        target_status="approved",
        noun="approval",
    ),
    TelegramDecisionAction.REJECT: _DecisionResponseMetadata(
        domain_action="reject",
        target_status="rejected",
        noun="rejection",
    ),
}


_ACTION_BUTTONS = (
    ("1. APPROVE", TelegramDecisionAction.APPROVE),
    ("2. REJECT", TelegramDecisionAction.REJECT),
    ("3. DETAILS", TelegramDecisionAction.DETAILS),
)


class TelegramDecisionError(RuntimeError):
    """Raised when Telegram Decision state cannot be handled safely."""


@dataclass(frozen=True)
class TelegramDecisionButton:
    text: str
    callback_data: str


class TelegramDecisionUpdateSource(Protocol):
    async def get_bot_username(self) -> str: ...

    async def get_webhook_url(self) -> str: ...

    async def get_updates(
        self,
        *,
        timeout_seconds: int,
        offset: int | None = None,
    ) -> Sequence[TelegramUpdateRecord]: ...


class TelegramDecisionClient(Protocol):
    async def send_message(
        self,
        *,
        destination: int,
        text: str,
        thread_id: int | None = None,
    ) -> None: ...

    async def send_interactive_message(
        self,
        *,
        destination: int,
        text: str,
        buttons: Sequence[TelegramDecisionButton],
        thread_id: int | None = None,
    ) -> int: ...

    async def answer_callback(
        self,
        *,
        callback_id: str,
        text: str,
        show_alert: bool = False,
    ) -> None: ...


async def listen_for_decisions(
    updates: TelegramDecisionUpdateSource,
    client: TelegramDecisionClient,
    settings: TelegramSettings,
    database_path: Path,
    one_shot_trigger: OneShotRunTrigger,
    redactor: Redactor,
    *,
    on_ready: Callable[[], None] | None = None,
    on_authorized: Callable[[], None] | None = None,
    max_batches: int | None = None,
) -> None:
    """Send pending Decisions and consume callbacks without executing handlers."""
    destination = settings.destination
    if destination is None:
        raise TelegramConfigurationError(
            "Telegram notification chat ID is missing"
        )
    await verify_long_polling(updates)
    bot_username = await updates.get_bot_username()
    await _request_pending_approval_runs(
        database_path,
        one_shot_trigger,
    )
    await _send_unmapped_pending_decisions(
        client,
        database_path,
        destination.chat_id,
        destination.thread_id,
        redactor,
    )
    if on_ready is not None:
        on_ready()

    offset: int | None = None
    batches = 0
    while max_batches is None or batches < max_batches:
        batch = await updates.get_updates(timeout_seconds=30, offset=offset)
        await _request_pending_approval_runs(
            database_path,
            one_shot_trigger,
        )
        if on_authorized is not None and any(
            _is_authorized_start(update, settings, bot_username)
            for update in batch
        ):
            on_authorized()
        for update in batch:
            if update.callback is not None:
                await _handle_callback(
                    update.callback,
                    client,
                    settings,
                    database_path,
                    destination.thread_id,
                    one_shot_trigger,
                    redactor,
                )
        if batch:
            offset = max(update.update_id for update in batch) + 1
        await _send_unmapped_pending_decisions(
            client,
            database_path,
            destination.chat_id,
            destination.thread_id,
            redactor,
        )
        batches += 1


def _is_authorized_start(
    update: TelegramUpdateRecord,
    settings: TelegramSettings,
    bot_username: str,
) -> bool:
    return any(
        settings.authorizes(candidate.authorization_context)
        for candidate in setup_candidates_from_updates(
            (update,),
            bot_username=bot_username,
        )
    )


async def _handle_callback(
    callback: TelegramCallbackRecord,
    client: TelegramDecisionClient,
    settings: TelegramSettings,
    database_path: Path,
    thread_id: int | None,
    one_shot_trigger: OneShotRunTrigger,
    redactor: Redactor,
) -> None:
    context = callback.authorization_context
    if context is None or not settings.authorizes(context):
        await client.answer_callback(
            callback_id=callback.callback_id,
            text="You are not authorized to respond to this Decision.",
            show_alert=True,
        )
        return
    parsed = _parse_callback_data(callback.data)
    if parsed is None:
        await client.answer_callback(
            callback_id=callback.callback_id,
            text="This RunTasks control is invalid.",
            show_alert=True,
        )
        return
    reference, action = parsed
    decision = _decision_for_callback(
        database_path,
        reference,
        chat_id=context.chat_id,
        message_id=callback.message_id,
    )
    if decision is None:
        await client.answer_callback(
            callback_id=callback.callback_id,
            text="This Decision is unknown or expired.",
            show_alert=True,
        )
        return
    if action in {
        TelegramDecisionAction.APPROVE,
        TelegramDecisionAction.REJECT,
    }:
        await _handle_response(
            callback,
            context.chat_id,
            client,
            database_path,
            decision,
            action,
            thread_id,
            one_shot_trigger,
            redactor,
        )
        return
    if decision.status != "pending":
        await client.answer_callback(
            callback_id=callback.callback_id,
            text=f"Decision is already {decision.status}.",
        )
        return
    task = get_task(database_path, decision.task_id, include_removed=True)
    message_id = await client.send_interactive_message(
        destination=context.chat_id,
        text=redactor.text(_decision_details(decision, task.name)),
        buttons=_decision_buttons(decision.id),
        thread_id=thread_id,
    )
    _record_message(
        database_path,
        decision.id,
        chat_id=context.chat_id,
        message_id=message_id,
        message_kind="details",
    )
    await client.answer_callback(
        callback_id=callback.callback_id,
        text="Decision details sent.",
    )


async def _handle_response(
    callback: TelegramCallbackRecord,
    chat_id: int,
    client: TelegramDecisionClient,
    database_path: Path,
    decision: Decision,
    action: TelegramDecisionAction,
    thread_id: int | None,
    one_shot_trigger: OneShotRunTrigger,
    redactor: Redactor,
) -> None:
    metadata = _RESPONSE_METADATA[action]
    if decision.status != "pending":
        if decision.status == metadata.target_status:
            await client.answer_callback(
                callback_id=callback.callback_id,
                text=f"Decision was already {decision.status}.",
            )
        else:
            await client.answer_callback(
                callback_id=callback.callback_id,
                text=(
                    f"Decision is already {decision.status}; "
                    f"{metadata.noun} is out of order."
                ),
                show_alert=True,
            )
        return
    try:
        result = transition_decision(
            database_path,
            decision.id,
            metadata.domain_action,
            channel="telegram",
            responded_by=str(callback.user_id),
        )
    except DecisionTransitionError:
        await client.answer_callback(
            callback_id=callback.callback_id,
            text=(
                "This Decision control has expired or is out of order; "
                "current state was preserved."
            ),
            show_alert=True,
        )
        return
    if not result.changed:
        await client.answer_callback(
            callback_id=callback.callback_id,
            text=f"Decision was already {result.decision.status}.",
        )
        return
    task = get_task(
        database_path,
        result.decision.task_id,
        include_removed=True,
    )
    if action is TelegramDecisionAction.REJECT:
        await client.send_message(
            destination=chat_id,
            text=redactor.text(_rejection_message(task.name)),
            thread_id=thread_id,
        )
        await client.answer_callback(
            callback_id=callback.callback_id,
            text="Decision rejected. No execution was requested.",
        )
        return
    approval_run_id = result.decision.approval_run_id
    if approval_run_id is None:
        raise TelegramDecisionError(
            "approved Decision is missing its separate approval Run"
        )
    if not await _request_approval_run(
        database_path,
        one_shot_trigger,
        approval_run_id,
    ):
        await client.send_message(
            destination=chat_id,
            text=redactor.text(
                _queued_approval_message(
                    task.name,
                    approval_run_id,
                )
            ),
            thread_id=thread_id,
        )
        await client.answer_callback(
            callback_id=callback.callback_id,
            text=(
                "Decision approved, but one-shot processing "
                "could not be requested."
            ),
            show_alert=True,
        )
        return
    await client.send_message(
        destination=chat_id,
        text=redactor.text(_approval_message(task.name)),
        thread_id=thread_id,
    )
    await client.answer_callback(
        callback_id=callback.callback_id,
        text="Decision approved. One-shot processing requested.",
    )


async def _request_pending_approval_runs(
    database_path: Path,
    one_shot_trigger: OneShotRunTrigger,
) -> None:
    for approval_run_id in list_pending_approval_run_triggers(database_path):
        await _request_approval_run(
            database_path,
            one_shot_trigger,
            approval_run_id,
        )


async def _request_approval_run(
    database_path: Path,
    one_shot_trigger: OneShotRunTrigger,
    approval_run_id: str,
) -> bool:
    try:
        await one_shot_trigger.request()
    except OneShotRunTriggerError:
        return False
    mark_approval_run_trigger_requested(database_path, approval_run_id)
    return True


async def _send_unmapped_pending_decisions(
    client: TelegramDecisionClient,
    database_path: Path,
    chat_id: int,
    thread_id: int | None,
    redactor: Redactor,
) -> None:
    for decision in list_decisions(database_path):
        if decision.status != "pending" or _has_initial_message(
            database_path, decision.id
        ):
            continue
        task = get_task(database_path, decision.task_id, include_removed=True)
        message_id = await client.send_interactive_message(
            destination=chat_id,
            text=redactor.text(_decision_summary(decision, task.name)),
            buttons=_decision_buttons(decision.id),
            thread_id=thread_id,
        )
        _record_message(
            database_path,
            decision.id,
            chat_id=chat_id,
            message_id=message_id,
            message_kind="decision",
        )


def _parse_callback_data(
    data: str | None,
) -> tuple[str, TelegramDecisionAction] | None:
    if data is None or len(data.encode("utf-8")) > 64:
        return None
    match = _CALLBACK_PATTERN.fullmatch(data)
    if match is None:
        return None
    return (
        match.group("reference"),
        TelegramDecisionAction(match.group("action")),
    )


def _decision_for_callback(
    path: Path,
    reference: str,
    *,
    chat_id: int,
    message_id: int | None,
) -> Decision | None:
    if message_id is None:
        return None
    decision_id = f"dcs_{reference}"
    try:
        with _telegram_decision_connection(path) as connection:
            row = connection.execute(
                """
                SELECT 1 FROM telegram_decision_messages
                WHERE decision_id = ? AND chat_id = ? AND message_id = ?
                """,
                (decision_id, chat_id, message_id),
            ).fetchone()
    except sqlite3.Error as error:
        raise TelegramDecisionError(
            "Telegram Decision mapping could not be inspected"
        ) from error
    if row is None:
        return None
    try:
        return get_decision(path, decision_id)
    except DecisionError:
        return None


def _decision_buttons(decision_id: str) -> tuple[TelegramDecisionButton, ...]:
    reference = _decision_reference(decision_id)
    buttons = tuple(
        TelegramDecisionButton(
            text,
            f"{_CALLBACK_PREFIX}:{reference}:{action.value}",
        )
        for text, action in _ACTION_BUTTONS
    )
    if any(len(button.callback_data.encode("utf-8")) > 64 for button in buttons):
        raise TelegramDecisionError("Telegram Decision callback data is too long")
    return buttons


def _decision_reference(decision_id: str) -> str:
    if not decision_id.startswith("dcs_") or len(decision_id) != 28:
        raise TelegramDecisionError("Decision ID cannot be mapped to Telegram")
    reference = decision_id[4:]
    if any(character not in "0123456789abcdef" for character in reference):
        raise TelegramDecisionError("Decision ID cannot be mapped to Telegram")
    return reference


def _decision_summary(decision: Decision, task_name: str) -> str:
    operation, handler = _plan_operation(decision)
    parameters = decision.plan.get("parameters", {})
    parameter_lines = _summary_parameter_lines(parameters)
    return "\n".join(
        (
            "RunTasks needs your decision",
            "",
            f"Task: {task_name}",
            f"Reason: {decision.reason}",
            "",
            "Proposed operation:",
            f"{operation} via {handler}",
            "",
            "Parameters:",
            *parameter_lines,
            "",
            "Validation:",
            decision.validation_summary,
            "",
            "Rollback:",
            decision.rollback_summary,
            "",
            "Approval authorizes only this exact stored plan.",
        )
    )


def _approval_message(task_name: str) -> str:
    return "\n".join(
        (
            "RunTasks approval recorded",
            "",
            f"Task: {task_name}",
            "The exact stored plan is approved.",
            "One-shot processing was requested.",
            "The Telegram listener did not execute the handler.",
        )
    )


def _queued_approval_message(task_name: str, approval_run_id: str) -> str:
    return "\n".join(
        (
            "RunTasks approval recorded",
            "",
            f"Task: {task_name}",
            "The exact stored plan is approved.",
            "One-shot processing could not be requested automatically.",
            f"Approval Run {approval_run_id} remains queued.",
            "The Telegram listener did not execute the handler.",
        )
    )


def _rejection_message(task_name: str) -> str:
    return "\n".join(
        (
            "RunTasks Decision rejected",
            "",
            f"Task: {task_name}",
            "No execution was requested.",
        )
    )


def _decision_details(decision: Decision, task_name: str) -> str:
    operation, handler = _plan_operation(decision)
    return "\n".join(
        (
            "RunTasks Decision details",
            "",
            f"Task: {task_name}",
            f"Reason: {decision.reason}",
            "",
            "Plan hash:",
            decision.plan_hash,
            "",
            "Operation:",
            f"{operation} via {handler}",
            "",
            "Parameters:",
            _pretty_json(decision.plan.get("parameters", {})),
            "",
            "Evidence:",
            _pretty_json(decision.plan.get("evidence", {})),
            "",
            "Validation summary:",
            decision.validation_summary,
            "",
            "Validation plan:",
            _pretty_json(decision.plan.get("validation", {})),
            "",
            "Rollback summary:",
            decision.rollback_summary,
            "",
            "Rollback plan:",
            _pretty_json(decision.plan.get("rollback", {})),
        )
    )


def _plan_operation(decision: Decision) -> tuple[str, str]:
    return (
        str(decision.plan.get("operation", "unknown-operation")),
        str(decision.plan.get("handler", "unknown-handler")),
    )


def _pretty_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _summary_parameter_lines(value: object) -> tuple[str, ...]:
    if not isinstance(value, dict) or not value:
        return ("- none",)
    lines: list[str] = []
    for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
        rendered = (
            str(item)
            if isinstance(item, (str, int, float, bool)) or item is None
            else json.dumps(item, ensure_ascii=False, sort_keys=True)
        )
        lines.append(f"- {key}: {rendered}")
    return tuple(lines)


def _has_initial_message(path: Path, decision_id: str) -> bool:
    try:
        with _telegram_decision_connection(path) as connection:
            row = connection.execute(
                """
                SELECT 1 FROM telegram_decision_messages
                WHERE decision_id = ? AND message_kind = 'decision'
                """,
                (decision_id,),
            ).fetchone()
    except sqlite3.Error as error:
        raise TelegramDecisionError(
            "Telegram Decision mapping could not be inspected"
        ) from error
    return row is not None


def _record_message(
    path: Path,
    decision_id: str,
    *,
    chat_id: int,
    message_id: int,
    message_kind: str,
) -> None:
    sent_at = datetime.now(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")
    try:
        with _telegram_decision_connection(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO telegram_decision_messages(
                    decision_id, chat_id, message_id, message_kind, sent_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (decision_id, chat_id, message_id, message_kind, sent_at),
            )
            connection.commit()
    except sqlite3.Error as error:
        raise TelegramDecisionError(
            "Telegram Decision mapping could not be recorded"
        ) from error


@contextmanager
def _telegram_decision_connection(
    path: Path,
) -> Iterator[sqlite3.Connection]:
    if not path.is_file():
        raise TelegramDecisionError(
            "runtime is not initialized; run 'runtasks init' first"
        )
    with database_connection(path, enable_wal=False) as connection:
        connection.row_factory = sqlite3.Row
        try:
            version_row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()
        except sqlite3.Error as error:
            raise TelegramDecisionError(
                "database schema is not current; run 'runtasks init'"
            ) from error
        version = 0 if version_row is None else int(version_row[0])
        if version != LATEST_SCHEMA_VERSION:
            raise TelegramDecisionError(
                "database schema is not current; run 'runtasks init'"
            )
        yield connection
