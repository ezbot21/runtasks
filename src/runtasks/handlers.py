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
                    "importance_context": _pi_mcp_importance_context(context.task),
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
        if outcome.status != "success":
            return HandlerOutcome(
                status="failed",
                summary=outcome.summary,
                details=details,
                external_log_ref=outcome.external_log_ref,
            )
        if details.get("contract") != "pi-mcp-release-check/v1":
            return HandlerOutcome(
                status="success",
                summary=outcome.summary,
                details=details,
                external_log_ref=outcome.external_log_ref,
            )
        return _pi_mcp_release_handler_outcome(
            context,
            outcome.summary,
            details,
            outcome.external_log_ref,
        )


def _pi_mcp_importance_context(task: Task) -> dict[str, object]:
    context: dict[str, object] = {}
    for key in ("active_mcp_servers", "important_conditions"):
        value = task.policy.get(key)
        if isinstance(value, list) and all(
            isinstance(item, str) and item.strip() for item in value
        ):
            context[key] = [cast(str, item).strip() for item in value]
    return context


def _pi_mcp_release_handler_outcome(
    context: HandlerContext,
    adapter_summary: str,
    details: dict[str, object],
    external_log_ref: str | None,
) -> HandlerOutcome:
    result = details.get("outcome")
    if result not in {"no-change", "non-important", "decision-required"}:
        raise HandlerError("Pi MCP release-check outcome is invalid")
    installed_version = details.get("installed_version")
    available_version = details.get("available_version")
    if installed_version is not None and not isinstance(installed_version, str):
        raise HandlerError("Pi MCP installed version is invalid")
    if available_version is not None and not isinstance(available_version, str):
        raise HandlerError("Pi MCP available version is invalid")
    evidence = details.get("evidence")
    source_failures = details.get("source_failures")
    if not isinstance(evidence, list) or not isinstance(source_failures, list):
        raise HandlerError("Pi MCP release evidence is invalid")

    if result == "no-change":
        if installed_version is None or installed_version != available_version:
            raise HandlerError("Pi MCP no-change versions are inconsistent")
        return HandlerOutcome(
            status="no-change",
            summary=f"Pi MCP adapter remains at stable version {installed_version}.",
            details=details,
            external_log_ref=external_log_ref,
        )

    assessment = details.get("assessment")
    if not isinstance(assessment, dict):
        raise HandlerError("Pi MCP importance assessment is invalid")
    assessment_values = cast(dict[object, object], assessment)
    required_assessment = {
        "importance",
        "category",
        "reason",
        "recommendation",
        "confidence",
    }
    if set(assessment_values) != required_assessment or not all(
        isinstance(assessment_values.get(field), str)
        and cast(str, assessment_values[field]).strip()
        for field in required_assessment
    ):
        raise HandlerError("Pi MCP importance assessment is invalid")
    importance = cast(str, assessment_values["importance"])
    category = cast(str, assessment_values["category"])
    confidence = cast(str, assessment_values["confidence"])
    reason = cast(str, assessment_values["reason"]).strip()
    if result == "non-important":
        if (
            importance != "non-important"
            or category != "routine"
            or confidence != "high"
        ):
            raise HandlerError(
                "Pi MCP non-important assessment lacks high confidence"
            )
        return HandlerOutcome(
            status="non-important",
            summary=reason,
            details=details,
            external_log_ref=external_log_ref,
        )

    if importance not in {"important", "uncertain"}:
        raise HandlerError("Pi MCP Decision assessment is invalid")
    if importance == "important" and category in {"routine", "uncertain"}:
        raise HandlerError("Pi MCP important assessment category is invalid")
    if importance == "uncertain" and category != "uncertain":
        raise HandlerError("Pi MCP uncertain assessment category is invalid")
    plan = _pi_mcp_decision_plan(
        context,
        installed_version=installed_version,
        available_version=available_version,
        assessment=cast(dict[str, object], assessment),
        evidence=cast(list[object], evidence),
        source_failures=cast(list[object], source_failures),
    )
    validation_summary = (
        "Install only the exact approved version, verify package metadata, restart "
        "pi-web.service, confirm Pi Web health, and require exact MCP_ADAPTER_OK."
        if installed_version is not None and available_version is not None
        else "This is a manual review only; no package mutation is authorized."
    )
    rollback_summary = (
        f"If post-install validation fails, restore exact version {installed_version}, "
        "restart Pi Web, and repeat health and MCP validation."
        if installed_version is not None and available_version is not None
        else "No rollback is needed because this plan authorizes no mutation."
    )
    return HandlerOutcome(
        status="decision-required",
        summary=adapter_summary,
        details=details,
        external_log_ref=external_log_ref,
        decision=DecisionRequest(
            plan=plan,
            reason=reason,
            validation_summary=validation_summary,
            rollback_summary=rollback_summary,
        ),
    )


def _pi_mcp_decision_plan(
    context: HandlerContext,
    *,
    installed_version: str | None,
    available_version: str | None,
    assessment: dict[str, object],
    evidence: list[object],
    source_failures: list[object],
) -> dict[str, object]:
    common_parameters: dict[str, object] = {
        "available_version": available_version,
        "installed_version": installed_version,
        "package": "pi-mcp-adapter",
        "run_id": context.run_id,
        "task_id": context.task.id,
    }
    if installed_version is None or available_version is None:
        return {
            "assessment": assessment,
            "evidence": {
                "releases": evidence,
                "source_failures": source_failures,
            },
            "handler": "pi_mcp_adapter",
            "operation": "manual-review-only",
            "parameters": common_parameters,
            "rollback": {"mutation_authorized": False},
            "validation": {"mutation_authorized": False},
        }
    return {
        "assessment": assessment,
        "evidence": {
            "releases": evidence,
            "source_failures": source_failures,
        },
        "handler": "pi_mcp_adapter",
        "operation": "install-exact-version",
        "parameters": {
            **common_parameters,
            "package_spec": f"npm:pi-mcp-adapter@{available_version}",
            "target_version": available_version,
        },
        "rollback": {
            "package_spec": f"npm:pi-mcp-adapter@{installed_version}",
            "restart_service": "pi-web.service",
            "target_version": installed_version,
            "validate_mcp_result": "MCP_ADAPTER_OK",
        },
        "validation": {
            "expected_installed_version": available_version,
            "expected_mcp_result": "MCP_ADAPTER_OK",
            "health_check": "Pi Web healthy",
            "restart_service": "pi-web.service",
        },
    }


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
