from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Mapping, Protocol, cast

from runtasks.redaction import Redactor


class ExternalAdapterError(RuntimeError):
    """Raised when an external adapter cannot return a safe structured outcome."""


@dataclass(frozen=True)
class ExternalRequest:
    operation: str
    parameters: Mapping[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "parameters": dict(self.parameters),
        }


@dataclass(frozen=True)
class ExternalOutcome:
    status: str
    summary: str
    details: dict[str, object]
    external_log_ref: str | None = None


class ExternalAdapter(Protocol):
    def perform(self, request: ExternalRequest) -> ExternalOutcome:
        """Perform one bounded operation and return only normalized data."""


def normalize_external_outcome(
    value: object,
    redactor: Redactor,
) -> ExternalOutcome:
    if not isinstance(value, dict):
        raise ExternalAdapterError("external adapter outcome must be an object")
    outcome = cast(dict[object, object], value)
    if set(outcome) - {"status", "summary", "details", "external_log_ref"}:
        raise ExternalAdapterError("external adapter outcome contains unsupported fields")
    status = outcome.get("status")
    if status not in {"success", "failure"}:
        raise ExternalAdapterError(
            "external adapter status must be success or failure"
        )
    summary = outcome.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ExternalAdapterError("external adapter summary must be non-empty text")
    if len(summary) > 8_000:
        raise ExternalAdapterError("external adapter summary is too long")
    details = outcome.get("details", {})
    if not isinstance(details, dict):
        raise ExternalAdapterError("external adapter details must be an object")
    log_reference = outcome.get("external_log_ref")
    if log_reference is not None and (
        not isinstance(log_reference, str)
        or not log_reference.strip()
        or len(log_reference) > 2_000
    ):
        raise ExternalAdapterError(
            "external adapter log reference must be null or non-empty text"
        )
    safe_details = redactor.value(details)
    if not isinstance(safe_details, dict):
        raise ExternalAdapterError("external adapter details could not be normalized")
    return ExternalOutcome(
        status=str(status),
        summary=redactor.text(summary.strip()),
        details=cast(dict[str, object], safe_details),
        external_log_ref=(
            None
            if log_reference is None
            else redactor.text(log_reference.strip())
        ),
    )


class LocalInspectionAdapter:
    def __init__(self, redactor: Redactor) -> None:
        self._redactor = redactor

    def perform(self, request: ExternalRequest) -> ExternalOutcome:
        if request.operation != "pi_mcp_adapter.inspect":
            raise ExternalAdapterError("external operation is not registered")
        return normalize_external_outcome(
            {
                "status": "failure",
                "summary": "Pi MCP adapter inspection is not configured yet.",
                "details": {
                    "operation": "read-only-inspection",
                    "validation": (
                        "The bounded production adapter is unavailable; no external "
                        "command was executed."
                    ),
                },
            },
            self._redactor,
        )


class FixtureExternalAdapter:
    """Deterministic subprocess-test adapter selected only by explicit settings."""

    def __init__(
        self,
        outcome_json: str,
        request_log: Path | None,
        redactor: Redactor,
    ) -> None:
        self._outcome_json = outcome_json
        self._request_log = request_log
        self._redactor = redactor

    def perform(self, request: ExternalRequest) -> ExternalOutcome:
        if self._request_log is not None:
            self._request_log.parent.mkdir(parents=True, exist_ok=True)
            with self._request_log.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        self._redactor.value(request.as_dict()),
                        sort_keys=True,
                    )
                    + "\n"
                )
        try:
            raw_outcome: object = json.loads(self._outcome_json)
        except (json.JSONDecodeError, UnicodeError) as error:
            raise ExternalAdapterError(
                "fixture external adapter outcome is invalid"
            ) from error
        return normalize_external_outcome(raw_outcome, self._redactor)


def build_external_adapter(
    settings: Mapping[str, str],
    redactor: Redactor,
) -> ExternalAdapter:
    adapter_name = settings.get("RUNTASKS_EXTERNAL_ADAPTER", "local")
    if adapter_name == "local":
        return LocalInspectionAdapter(redactor)
    if adapter_name == "fixture":
        outcome_json = settings.get("RUNTASKS_FIXTURE_EXTERNAL_OUTCOME")
        if outcome_json is None:
            raise ExternalAdapterError("fixture external adapter outcome is required")
        raw_log_path = settings.get("RUNTASKS_FIXTURE_REQUEST_LOG")
        log_path = None if raw_log_path is None else Path(raw_log_path)
        return FixtureExternalAdapter(outcome_json, log_path, redactor)
    raise ExternalAdapterError("external adapter is not registered")
