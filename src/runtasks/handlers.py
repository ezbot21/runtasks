from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Mapping, Protocol, cast

from runtasks.adapters import ExternalAdapter, ExternalRequest
from runtasks.handler_contracts import HANDLER_ACTION_MODES
from runtasks.redaction import Redactor
from runtasks.tasks import Task


class HandlerError(RuntimeError):
    """Raised when a named handler cannot execute safely."""


@dataclass(frozen=True)
class HandlerContext:
    run_id: str
    task: Task
    trigger: str


@dataclass(frozen=True)
class DecisionRequest:
    plan: dict[str, object]
    reason: str
    validation_summary: str
    rollback_summary: str


@dataclass(frozen=True)
class HandlerOutcome:
    status: str
    summary: str
    details: dict[str, object]
    external_log_ref: str | None = None
    decision: DecisionRequest | None = None


class Handler(Protocol):
    def execute(
        self,
        context: HandlerContext,
        external_adapter: ExternalAdapter,
    ) -> HandlerOutcome:
        """Execute bounded named work and return a structured outcome."""


@dataclass(frozen=True)
class HandlerRegistry:
    handlers: Mapping[str, Handler]

    def get(self, name: str) -> Handler:
        handler = self.handlers.get(name)
        if handler is None:
            raise HandlerError("handler is not registered")
        return handler


class ManualNotificationHandler:
    def execute(
        self,
        context: HandlerContext,
        external_adapter: ExternalAdapter,
    ) -> HandlerOutcome:
        del external_adapter
        return HandlerOutcome(
            status="manual-action-due",
            summary=f"Manual action is due for {context.task.name}.",
            details={
                "action_mode": "notify",
                "handler": "manual_notification",
                "mutation_performed": False,
            },
        )


class PiMcpAdapterHandler:
    def execute(
        self,
        context: HandlerContext,
        external_adapter: ExternalAdapter,
    ) -> HandlerOutcome:
        outcome = external_adapter.perform(
            ExternalRequest(
                operation="pi_mcp_adapter.inspect",
                parameters={
                    "run_id": context.run_id,
                    "task_id": context.task.id,
                },
            )
        )
        details = dict(outcome.details)
        details.update(
            {
                "action_mode": context.task.action_mode,
                "handler": "pi_mcp_adapter",
                "mutation_performed": False,
            }
        )
        return HandlerOutcome(
            status="success" if outcome.status == "success" else "failed",
            summary=outcome.summary,
            details=details,
            external_log_ref=outcome.external_log_ref,
        )


class FixtureHandler:
    """Deterministic subprocess-test handler selected only by explicit settings."""

    def __init__(
        self,
        outcome_json: str,
        request_log: Path | None,
        redactor: Redactor,
        delay_seconds: float = 0,
    ) -> None:
        self._outcome_json = outcome_json
        self._request_log = request_log
        self._redactor = redactor
        self._delay_seconds = delay_seconds

    def execute(
        self,
        context: HandlerContext,
        external_adapter: ExternalAdapter,
    ) -> HandlerOutcome:
        del external_adapter
        if self._request_log is not None:
            self._request_log.parent.mkdir(parents=True, exist_ok=True)
            with self._request_log.open("a", encoding="utf-8") as stream:
                request = {
                    "handler": context.task.handler,
                    "run_id": context.run_id,
                    "task_id": context.task.id,
                    "trigger": context.trigger,
                }
                stream.write(
                    json.dumps(self._redactor.value(request), sort_keys=True) + "\n"
                )
        if self._delay_seconds:
            time.sleep(self._delay_seconds)
        try:
            raw_outcome: object = json.loads(self._outcome_json)
        except (json.JSONDecodeError, UnicodeError) as error:
            raise HandlerError("fixture handler outcome is invalid") from error
        if not isinstance(raw_outcome, dict):
            raise HandlerError("fixture handler outcome must be an object")
        outcome = cast(dict[object, object], raw_outcome)
        if set(outcome) - {
            "status",
            "summary",
            "details",
            "external_log_ref",
            "decision",
        }:
            raise HandlerError("fixture handler outcome contains unsupported fields")
        status = outcome.get("status")
        summary = outcome.get("summary")
        details = outcome.get("details")
        log_reference = outcome.get("external_log_ref")
        decision_value = outcome.get("decision")
        if not isinstance(status, str) or not isinstance(summary, str):
            raise HandlerError("fixture handler outcome is invalid")
        if not isinstance(details, dict):
            raise HandlerError("fixture handler details must be an object")
        if log_reference is not None and not isinstance(log_reference, str):
            raise HandlerError("fixture handler log reference is invalid")
        safe_details = self._redactor.value(details)
        if not isinstance(safe_details, dict):
            raise HandlerError("fixture handler details could not be normalized")
        decision = self._parse_decision_request(decision_value)
        return HandlerOutcome(
            status=status,
            summary=self._redactor.text(summary),
            details=cast(dict[str, object], safe_details),
            external_log_ref=(
                None
                if log_reference is None
                else self._redactor.text(log_reference)
            ),
            decision=decision,
        )

    def _parse_decision_request(self, value: object) -> DecisionRequest | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise HandlerError("fixture handler Decision request must be an object")
        request = cast(dict[object, object], value)
        if set(request) != {
            "plan",
            "reason",
            "validation_summary",
            "rollback_summary",
        }:
            raise HandlerError("fixture handler Decision request is invalid")
        plan = request.get("plan")
        reason = request.get("reason")
        validation_summary = request.get("validation_summary")
        rollback_summary = request.get("rollback_summary")
        if not isinstance(plan, dict):
            raise HandlerError("fixture handler Decision plan must be an object")
        if not all(
            isinstance(text, str)
            for text in (reason, validation_summary, rollback_summary)
        ):
            raise HandlerError("fixture handler Decision summaries are invalid")
        return DecisionRequest(
            plan=cast(dict[str, object], plan),
            reason=self._redactor.text(cast(str, reason)),
            validation_summary=self._redactor.text(cast(str, validation_summary)),
            rollback_summary=self._redactor.text(cast(str, rollback_summary)),
        )


_PRODUCTION_HANDLERS: Mapping[str, Handler] = {
    "manual_notification": ManualNotificationHandler(),
    "pi_mcp_adapter": PiMcpAdapterHandler(),
}
if _PRODUCTION_HANDLERS.keys() != HANDLER_ACTION_MODES.keys():
    raise RuntimeError("handler implementations do not match handler contracts")


def build_handler_registry(
    settings: Mapping[str, str],
    redactor: Redactor,
) -> HandlerRegistry:
    fixture_outcome = settings.get("RUNTASKS_FIXTURE_HANDLER_OUTCOME")
    if fixture_outcome is None:
        return HandlerRegistry(_PRODUCTION_HANDLERS)
    raw_log_path = settings.get("RUNTASKS_FIXTURE_HANDLER_REQUEST_LOG")
    log_path = None if raw_log_path is None else Path(raw_log_path)
    raw_delay = settings.get("RUNTASKS_FIXTURE_HANDLER_DELAY_SECONDS", "0")
    try:
        delay_seconds = float(raw_delay)
    except ValueError as error:
        raise HandlerError("fixture handler delay is invalid") from error
    if not 0 <= delay_seconds <= 60:
        raise HandlerError("fixture handler delay is invalid")
    fixture_handler = FixtureHandler(
        fixture_outcome,
        log_path,
        redactor,
        delay_seconds,
    )
    return HandlerRegistry(
        {name: fixture_handler for name in _PRODUCTION_HANDLERS}
    )
