from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Callable, Iterator, Mapping, cast
import uuid

from runtasks.adapters import ExternalAdapter
from runtasks.database import LATEST_SCHEMA_VERSION, database_connection
from runtasks.decisions import record_pending_decision
from runtasks.handlers import (
    DecisionRequest,
    HandlerContext,
    HandlerError,
    HandlerOutcome,
    HandlerRegistry,
)
from runtasks.redaction import Redactor
from runtasks.tasks import (
    Task,
    TaskError,
    _task_from_row,
    get_task,
    next_scheduled_occurrence,
)


RUN_STATUSES = frozenset(
    {
        "claimed",
        "running",
        "success",
        "no-change",
        "non-important",
        "decision-required",
        "failed",
        "rolled-back",
        "manual-action-due",
    }
)
_TERMINAL_STATUSES = frozenset(
    {
        "success",
        "no-change",
        "non-important",
        "decision-required",
        "failed",
        "rolled-back",
        "manual-action-due",
    }
)
_ALLOWED_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "claimed": frozenset({"running"}),
    "running": _TERMINAL_STATUSES,
    "success": frozenset(),
    "no-change": frozenset(),
    "non-important": frozenset(),
    "decision-required": frozenset(),
    "failed": frozenset(),
    "rolled-back": frozenset(),
    "manual-action-due": frozenset(),
}


class RunError(RuntimeError):
    """Raised when Run execution or history cannot be handled safely."""


class RunTransitionError(RunError):
    """Raised when a Run lifecycle transition is invalid."""


@dataclass(frozen=True)
class Run:
    id: str
    task_id: str
    task_name: str
    trigger: str
    status: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    summary: str
    details: dict[str, object]
    external_log_ref: str | None
    scheduled_for: str | None
    next_run_at: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "created_at": self.created_at,
            "details": self.details,
            "external_log_ref": self.external_log_ref,
            "finished_at": self.finished_at,
            "next_run_at": self.next_run_at,
            "id": self.id,
            "started_at": self.started_at,
            "scheduled_for": self.scheduled_for,
            "status": self.status,
            "summary": self.summary,
            "task_id": self.task_id,
            "task_name": self.task_name,
            "trigger": self.trigger,
        }


@dataclass(frozen=True)
class ScheduledClaim:
    run_id: str
    task: Task


def execute_manual_run(
    path: Path,
    task_id: str,
    external_adapter: ExternalAdapter,
    handler_registry: HandlerRegistry,
    redactor: Redactor,
) -> Run:
    task = get_task(path, task_id)
    run_id = _create_run(path, task, trigger="manual")
    return _execute_claimed_run(
        path,
        run_id,
        task,
        trigger="manual",
        external_adapter=external_adapter,
        handler_registry=handler_registry,
        redactor=redactor,
        timestamp_factory=_utc_now,
    )


def execute_scheduled_run(
    path: Path,
    run_id: str,
    task: Task,
    external_adapter: ExternalAdapter,
    handler_registry: HandlerRegistry,
    redactor: Redactor,
    timestamp_factory: Callable[[], str],
) -> Run:
    return _execute_claimed_run(
        path,
        run_id,
        task,
        trigger="scheduled",
        external_adapter=external_adapter,
        handler_registry=handler_registry,
        redactor=redactor,
        timestamp_factory=timestamp_factory,
    )


def _execute_claimed_run(
    path: Path,
    run_id: str,
    task: Task,
    *,
    trigger: str,
    external_adapter: ExternalAdapter,
    handler_registry: HandlerRegistry,
    redactor: Redactor,
    timestamp_factory: Callable[[], str],
) -> Run:
    _transition_run(path, run_id, "running", started_at=timestamp_factory())

    try:
        handler = handler_registry.get(task.handler)
        outcome = handler.execute(
            HandlerContext(run_id=run_id, task=task, trigger=trigger),
            external_adapter,
        )
        safe_outcome = _normalize_handler_outcome(outcome, redactor, task)
    except Exception as error:
        safe_outcome = HandlerOutcome(
            status="failed",
            summary=redactor.text(f"Named handler failed: {error}"),
            details={
                "handler": task.handler,
                "mutation_performed": False,
                "validation": redactor.text(str(error)),
            },
        )

    finished_at = timestamp_factory()
    if safe_outcome.decision is not None:
        record_pending_decision(
            path,
            run_id,
            task,
            safe_outcome,
            safe_outcome.decision,
            finished_at=finished_at,
        )
    else:
        _transition_run(
            path,
            run_id,
            safe_outcome.status,
            finished_at=finished_at,
            summary=safe_outcome.summary,
            details=safe_outcome.details,
            external_log_ref=safe_outcome.external_log_ref,
        )
    return get_run(path, run_id)


def claim_scheduled_run(
    path: Path,
    task_id: str,
    *,
    claimed_at: str,
) -> ScheduledClaim | None:
    run_id = f"run_{uuid.uuid4().hex[:24]}"
    current_time = datetime.fromisoformat(claimed_at.replace("Z", "+00:00"))
    try:
        with _run_connection(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM tasks
                WHERE id = ?
                  AND enabled = 1
                  AND removed_at IS NULL
                  AND next_run_at <= ?
                """,
                (task_id, claimed_at),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            task = _task_from_row(row)
            occurrence = next_scheduled_occurrence(task, current_time)
            scheduling_details: dict[str, object] = {
                "scheduling": {
                    "missed_occurrences_skipped": (
                        occurrence.missed_occurrences_skipped
                    ),
                    "next_run_at": occurrence.next_run_at,
                    "scheduled_for": occurrence.scheduled_for,
                }
            }
            cursor = connection.execute(
                """
                UPDATE tasks SET next_run_at = ?, updated_at = ?
                WHERE id = ? AND next_run_at = ?
                """,
                (
                    occurrence.next_run_at,
                    claimed_at,
                    task.id,
                    occurrence.scheduled_for,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            connection.execute(
                """
                INSERT INTO runs(
                    id, task_id, task_name, trigger, status, created_at,
                    summary, details_json, scheduled_for, next_run_at
                ) VALUES (?, ?, ?, 'scheduled', 'claimed', ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    task.id,
                    task.name,
                    claimed_at,
                    "Scheduled occurrence claimed.",
                    _canonical_details(scheduling_details),
                    occurrence.scheduled_for,
                    occurrence.next_run_at,
                ),
            )
            connection.commit()
    except RunError:
        raise
    except sqlite3.Error as error:
        raise RunError("scheduled Run could not be claimed") from error
    return ScheduledClaim(run_id=run_id, task=task)


def get_run(path: Path, run_id: str) -> Run:
    try:
        with _run_connection(path) as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
    except RunError:
        raise
    except sqlite3.Error as error:
        raise RunError("Run could not be inspected") from error
    if row is None:
        raise RunError("Run does not exist")
    return _run_from_row(row)


def list_runs(path: Path, *, task_id: str | None = None) -> list[Run]:
    if task_id is not None:
        try:
            get_task(path, task_id, include_removed=True)
        except TaskError as error:
            raise RunError(str(error)) from error
    where = "" if task_id is None else "WHERE task_id = ?"
    parameters: tuple[object, ...] = () if task_id is None else (task_id,)
    try:
        with _run_connection(path) as connection:
            rows = connection.execute(
                f"SELECT * FROM runs {where} ORDER BY created_at DESC, id DESC",
                parameters,
            ).fetchall()
    except RunError:
        raise
    except sqlite3.Error as error:
        raise RunError("Run history could not be listed") from error
    return [_run_from_row(row) for row in rows]


def search_runs(path: Path, query: str) -> list[Run]:
    normalized_query = query.strip()
    if not normalized_query:
        raise RunError("search query must not be empty")
    fts_query = _literal_fts_query(normalized_query)
    try:
        with _run_connection(path) as connection:
            rows = connection.execute(
                """
                SELECT runs.*
                FROM run_fts
                JOIN runs ON runs.id = run_fts.run_id
                WHERE run_fts MATCH ?
                ORDER BY bm25(run_fts), runs.created_at DESC
                """,
                (fts_query,),
            ).fetchall()
    except sqlite3.OperationalError as error:
        raise RunError("Run search failed") from error
    except RunError:
        raise
    except sqlite3.Error as error:
        raise RunError("Run search failed") from error
    return [_run_from_row(row) for row in rows]


def _create_run(path: Path, task: Task, *, trigger: str) -> str:
    run_id = f"run_{uuid.uuid4().hex[:24]}"
    created_at = _utc_now()
    try:
        with _run_connection(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO runs(
                    id, task_id, task_name, trigger, status, created_at,
                    summary, details_json
                ) VALUES (?, ?, ?, ?, 'claimed', ?, '', '{}')
                """,
                (run_id, task.id, task.name, trigger, created_at),
            )
            connection.commit()
    except RunError:
        raise
    except sqlite3.Error as error:
        raise RunError("Run could not be created") from error
    return run_id


def _transition_run(
    path: Path,
    run_id: str,
    target_status: str,
    *,
    started_at: str | None = None,
    finished_at: str | None = None,
    summary: str | None = None,
    details: dict[str, object] | None = None,
    external_log_ref: str | None = None,
) -> None:
    if target_status not in RUN_STATUSES:
        raise RunTransitionError("Run target status is invalid")
    try:
        with _run_connection(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, details_json FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise RunTransitionError("Run does not exist")
            current_status = str(row["status"])
            if target_status not in _ALLOWED_TRANSITIONS.get(
                current_status, frozenset()
            ):
                raise RunTransitionError(
                    f"Run cannot transition from {current_status} to {target_status}"
                )
            if target_status == "running":
                if started_at is None:
                    raise RunTransitionError("running Run requires a start timestamp")
                cursor = connection.execute(
                    """
                    UPDATE runs SET status = ?, started_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (target_status, started_at, run_id, current_status),
                )
            else:
                if finished_at is None or summary is None or details is None:
                    raise RunTransitionError(
                        "terminal Run requires completion details"
                    )
                stored_details_value = json.loads(str(row["details_json"]))
                if not isinstance(stored_details_value, dict):
                    raise RunTransitionError("stored Run details are invalid")
                merged_details = dict(details)
                merged_details.update(cast(dict[str, object], stored_details_value))
                details_json = _canonical_details(merged_details)
                cursor = connection.execute(
                    """
                    UPDATE runs SET
                        status = ?, finished_at = ?, summary = ?,
                        details_json = ?, external_log_ref = ?
                    WHERE id = ? AND status = ?
                    """,
                    (
                        target_status,
                        finished_at,
                        summary,
                        details_json,
                        external_log_ref,
                        run_id,
                        current_status,
                    ),
                )
            if cursor.rowcount != 1:
                raise RunTransitionError("Run transition lost a concurrent update")
            connection.commit()
    except RunError:
        raise
    except sqlite3.Error as error:
        raise RunError("Run transition failed") from error


def _normalize_handler_outcome(
    outcome: HandlerOutcome,
    redactor: Redactor,
    task: Task,
) -> HandlerOutcome:
    if outcome.status not in _TERMINAL_STATUSES:
        raise HandlerError("handler returned an invalid Run status")
    if not isinstance(outcome.summary, str) or not outcome.summary.strip():
        raise HandlerError("handler returned an invalid summary")
    if len(outcome.summary) > 8_000:
        raise HandlerError("handler summary is too long")
    safe_details = redactor.value(outcome.details)
    if not isinstance(safe_details, dict):
        raise HandlerError("handler returned invalid details")
    details = cast(dict[str, object], safe_details)
    _canonical_details(details)
    decision = _normalize_decision_request(outcome.decision, redactor, task)
    if outcome.status == "decision-required" and decision is None:
        raise HandlerError("handler omitted the required Decision request")
    if outcome.status != "decision-required" and decision is not None:
        raise HandlerError("handler requested a Decision with an incompatible status")
    log_reference = outcome.external_log_ref
    if log_reference is not None and (
        not isinstance(log_reference, str)
        or not log_reference.strip()
        or len(log_reference) > 2_000
    ):
        raise HandlerError("handler returned an invalid log reference")
    return HandlerOutcome(
        status=outcome.status,
        summary=redactor.text(outcome.summary.strip()),
        details=details,
        external_log_ref=(
            None
            if log_reference is None
            else redactor.text(log_reference.strip())
        ),
        decision=decision,
    )


def _normalize_decision_request(
    request: DecisionRequest | None,
    redactor: Redactor,
    task: Task,
) -> DecisionRequest | None:
    if request is None:
        return None
    if task.action_mode != "approved-procedure":
        raise HandlerError("only an approved-procedure handler can request a Decision")
    if not request.plan or not all(
        isinstance(key, str) for key in request.plan
    ):
        raise HandlerError("handler returned an invalid Decision plan")
    required_fields = {
        "handler",
        "operation",
        "parameters",
        "rollback",
        "validation",
    }
    if not required_fields.issubset(request.plan):
        raise HandlerError(
            "handler Decision plan is missing authorization fields"
        )
    operation = request.plan.get("operation")
    parameters = request.plan.get("parameters")
    validation = request.plan.get("validation")
    rollback = request.plan.get("rollback")
    if not isinstance(operation, str) or not operation.strip():
        raise HandlerError("handler Decision operation is invalid")
    if not isinstance(parameters, dict):
        raise HandlerError("handler Decision parameters are invalid")
    if not isinstance(validation, dict) or not validation:
        raise HandlerError("handler Decision validation is invalid")
    if not isinstance(rollback, dict) or not rollback:
        raise HandlerError("handler Decision rollback is invalid")
    operation_plan = {
        key: value for key, value in request.plan.items() if key != "evidence"
    }
    safe_operation_plan = redactor.value(operation_plan)
    if safe_operation_plan != operation_plan:
        raise HandlerError(
            "handler Decision operation contains secret-bearing values"
        )
    plan = dict(operation_plan)
    if "evidence" in request.plan:
        plan["evidence"] = redactor.value(request.plan["evidence"])
    if plan.get("handler") != task.handler:
        raise HandlerError(
            "handler Decision plan must name the requesting Task handler"
        )
    plan_json = _canonical_details(plan)
    if len(plan_json.encode("utf-8")) > 65_536:
        raise HandlerError("handler Decision plan is too large")
    summaries = (
        ("reason", request.reason),
        ("validation summary", request.validation_summary),
        ("rollback summary", request.rollback_summary),
    )
    normalized: list[str] = []
    for name, value in summaries:
        if not isinstance(value, str) or not value.strip():
            raise HandlerError(f"handler Decision {name} is invalid")
        if len(value) > 8_000:
            raise HandlerError(f"handler Decision {name} is too long")
        normalized.append(redactor.text(value.strip()))
    return DecisionRequest(
        plan=plan,
        reason=normalized[0],
        validation_summary=normalized[1],
        rollback_summary=normalized[2],
    )


def _canonical_details(details: dict[str, object]) -> str:
    try:
        return json.dumps(
            details,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise HandlerError("handler details are not JSON-compatible") from error


def _run_from_row(row: sqlite3.Row) -> Run:
    details_value = json.loads(str(row["details_json"]))
    if not isinstance(details_value, dict):
        raise RunError("stored Run details are invalid")
    return Run(
        id=str(row["id"]),
        task_id=str(row["task_id"]),
        task_name=str(row["task_name"]),
        trigger=str(row["trigger"]),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        started_at=None if row["started_at"] is None else str(row["started_at"]),
        finished_at=(
            None if row["finished_at"] is None else str(row["finished_at"])
        ),
        summary=str(row["summary"]),
        details=cast(dict[str, object], details_value),
        external_log_ref=(
            None
            if row["external_log_ref"] is None
            else str(row["external_log_ref"])
        ),
        scheduled_for=(
            None if row["scheduled_for"] is None else str(row["scheduled_for"])
        ),
        next_run_at=(
            None if row["next_run_at"] is None else str(row["next_run_at"])
        ),
    )


@contextmanager
def _run_connection(path: Path) -> Iterator[sqlite3.Connection]:
    if not path.is_file():
        raise RunError("runtime is not initialized; run 'runtasks init' first")
    with database_connection(path, enable_wal=False) as connection:
        connection.row_factory = sqlite3.Row
        try:
            version_row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()
        except sqlite3.Error as error:
            raise RunError(
                "database schema is not current; run 'runtasks init'"
            ) from error
        version = 0 if version_row is None else int(version_row[0])
        if version != LATEST_SCHEMA_VERSION:
            raise RunError("database schema is not current; run 'runtasks init'")
        yield connection


def _literal_fts_query(query: str) -> str:
    terms = re.findall(r"\w+", query, flags=re.UNICODE)
    if not terms:
        raise RunError("search query must contain searchable text")
    return " AND ".join(f'"{term}"' for term in terms)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
