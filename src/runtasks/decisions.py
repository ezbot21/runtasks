from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Iterator, cast
import uuid

from runtasks.database import LATEST_SCHEMA_VERSION, database_connection
from runtasks.handlers import DecisionRequest, HandlerOutcome
from runtasks.tasks import Task


DECISION_STATUSES = frozenset(
    {"pending", "approved", "rejected", "completed", "failed"}
)
_RESPONSE_TARGETS = {"approve": "approved", "reject": "rejected"}
_NOTIFICATION_SELECT_COLUMNS = """
    notification.status AS notification_status,
    notification.attempts AS notification_attempts,
    notification.last_attempt_at AS notification_last_attempt_at,
    notification.last_error AS notification_last_error,
    notification.delivered_at AS notification_delivered_at
"""
_EXECUTION_SELECT_COLUMNS = """
    execution.status AS execution_status,
    execution.completed_at AS execution_completed_at
"""


class DecisionError(RuntimeError):
    """Raised when a Decision cannot be recorded or inspected safely."""


class DecisionTransitionError(DecisionError):
    """Raised when a Decision lifecycle transition is forbidden."""


@dataclass(frozen=True)
class DecisionResponse:
    action: str
    channel: str
    responded_by: str
    responded_at: str

    def as_dict(self) -> dict[str, str]:
        return {
            "action": self.action,
            "channel": self.channel,
            "responded_at": self.responded_at,
            "responded_by": self.responded_by,
        }


@dataclass(frozen=True)
class DecisionTransitionResult:
    decision: Decision
    changed: bool


@dataclass(frozen=True)
class DecisionNotificationDelivery:
    status: str
    attempts: int
    last_attempt_at: str | None
    last_error: str | None
    delivered_at: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "attempts": self.attempts,
            "delivered_at": self.delivered_at,
            "last_attempt_at": self.last_attempt_at,
            "last_error": self.last_error,
            "status": self.status,
        }


@dataclass(frozen=True)
class Decision:
    id: str
    task_id: str
    run_id: str
    status: str
    plan: dict[str, object]
    plan_hash: str
    reason: str
    validation_summary: str
    rollback_summary: str
    notification_delivery: DecisionNotificationDelivery
    response: DecisionResponse | None
    approval_run_id: str | None
    execution_scheduled_at: str | None
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "approval_run_id": self.approval_run_id,
            "created_at": self.created_at,
            "execution_scheduled_at": self.execution_scheduled_at,
            "id": self.id,
            "notification_delivery": self.notification_delivery.as_dict(),
            "plan": self.plan,
            "plan_hash": self.plan_hash,
            "reason": self.reason,
            "response": None if self.response is None else self.response.as_dict(),
            "rollback_summary": self.rollback_summary,
            "run_id": self.run_id,
            "status": self.status,
            "task_id": self.task_id,
            "updated_at": self.updated_at,
            "validation_summary": self.validation_summary,
        }


def record_pending_decision(
    path: Path,
    run_id: str,
    task: Task,
    outcome: HandlerOutcome,
    request: DecisionRequest,
    *,
    finished_at: str,
) -> str:
    """Create a pending Decision and complete its requesting Run atomically."""
    decision_id = f"dcs_{uuid.uuid4().hex[:24]}"
    plan_json = canonical_plan_json(request.plan)
    plan_hash = hashlib.sha256(plan_json.encode("utf-8")).hexdigest()
    try:
        with _decision_connection(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, details_json FROM runs WHERE id = ? AND task_id = ?",
                (run_id, task.id),
            ).fetchone()
            if row is None:
                raise DecisionTransitionError("requesting Run does not exist")
            if str(row["status"]) != "running":
                raise DecisionTransitionError(
                    "pending Decision requires a running requesting Run"
                )
            stored_details_value = json.loads(str(row["details_json"]))
            if not isinstance(stored_details_value, dict):
                raise DecisionError("stored Run details are invalid")
            connection.execute(
                """
                INSERT INTO decisions(
                    id, task_id, run_id, status, plan_json, plan_hash, reason,
                    validation_summary, rollback_summary, created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    task.id,
                    run_id,
                    plan_json,
                    plan_hash,
                    request.reason,
                    request.validation_summary,
                    request.rollback_summary,
                    finished_at,
                    finished_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO decision_notification_deliveries(
                    decision_id, status, attempts
                ) VALUES (?, 'pending', 0)
                """,
                (decision_id,),
            )
            merged_details = dict(outcome.details)
            merged_details.update(cast(dict[str, object], stored_details_value))
            merged_details.update(
                {"decision_id": decision_id, "plan_hash": plan_hash}
            )
            cursor = connection.execute(
                """
                UPDATE runs SET
                    status = 'decision-required', finished_at = ?, summary = ?,
                    details_json = ?, external_log_ref = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    finished_at,
                    outcome.summary,
                    _canonical_json(merged_details),
                    outcome.external_log_ref,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise DecisionTransitionError(
                    "requesting Run lost a concurrent transition"
                )
            connection.commit()
    except DecisionError:
        raise
    except (json.JSONDecodeError, sqlite3.Error) as error:
        raise DecisionError("pending Decision could not be recorded") from error
    return decision_id


def get_decision(path: Path, decision_id: str) -> Decision:
    try:
        with _decision_connection(path) as connection:
            row = connection.execute(
                f"{_DECISION_SELECT} WHERE decisions.id = ?",
                (decision_id,),
            ).fetchone()
    except DecisionError:
        raise
    except sqlite3.Error as error:
        raise DecisionError("Decision could not be inspected") from error
    if row is None:
        raise DecisionError("Decision does not exist")
    return _decision_from_row(row)


def list_decisions(path: Path) -> list[Decision]:
    try:
        with _decision_connection(path) as connection:
            rows = connection.execute(
                f"{_DECISION_SELECT} ORDER BY decisions.created_at DESC, decisions.id DESC"
            ).fetchall()
    except DecisionError:
        raise
    except sqlite3.Error as error:
        raise DecisionError("Decisions could not be listed") from error
    return [_decision_from_row(row) for row in rows]


def transition_decision(
    path: Path,
    decision_id: str,
    action: str,
    *,
    channel: str = "cli",
    responded_by: str = "local-user",
) -> DecisionTransitionResult:
    target_status = _RESPONSE_TARGETS.get(action)
    if target_status is None:
        raise DecisionTransitionError("Decision response is invalid")
    if not channel.strip() or not responded_by.strip():
        raise DecisionTransitionError("Decision response metadata is invalid")
    try:
        with _decision_connection(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"""
                SELECT decisions.*, tasks.name AS task_name,
                       tasks.removed_at AS task_removed_at,
                       {_NOTIFICATION_SELECT_COLUMNS},
                       {_EXECUTION_SELECT_COLUMNS}
                FROM decisions
                JOIN tasks ON tasks.id = decisions.task_id
                JOIN decision_notification_deliveries AS notification
                  ON notification.decision_id = decisions.id
                LEFT JOIN decision_execution_outcomes AS execution
                  ON execution.decision_id = decisions.id
                WHERE decisions.id = ?
                """,
                (decision_id,),
            ).fetchone()
            if row is None:
                raise DecisionError("Decision does not exist")
            current_status = str(row["status"])
            stored_response = row["response_action"]
            if stored_response is not None:
                if str(stored_response) == action:
                    connection.commit()
                    return DecisionTransitionResult(
                        decision=_decision_from_row(row),
                        changed=False,
                    )
                raise DecisionTransitionError(
                    f"Decision cannot transition from {current_status} to {target_status}"
                )
            if current_status != "pending":
                raise DecisionTransitionError(
                    f"Decision cannot transition from {current_status} to {target_status}"
                )
            if action == "approve" and row["task_removed_at"] is not None:
                raise DecisionTransitionError(
                    "Decision cannot be approved because its Task is removed"
                )

            _verified_plan(row)
            responded_at = _response_timestamp(str(row["created_at"]))
            approval_run_id: str | None = None
            execution_scheduled_at: str | None = None
            if action == "approve":
                approval_run_id = f"run_{uuid.uuid4().hex[:24]}"
                execution_scheduled_at = responded_at
                connection.execute(
                    """
                    INSERT INTO runs(
                        id, task_id, task_name, trigger, status, created_at,
                        summary, details_json
                    ) VALUES (?, ?, ?, 'approval', 'claimed', ?, ?, ?)
                    """,
                    (
                        approval_run_id,
                        str(row["task_id"]),
                        str(row["task_name"]),
                        responded_at,
                        "Approved Decision is ready for separate execution.",
                        _canonical_json(
                            {
                                "decision_id": decision_id,
                                "mutation_performed": False,
                                "plan_hash": str(row["plan_hash"]),
                            }
                        ),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO approval_run_trigger_requests(
                        approval_run_id, decision_id, created_at
                    ) VALUES (?, ?, ?)
                    """,
                    (approval_run_id, decision_id, responded_at),
                )

            cursor = connection.execute(
                """
                UPDATE decisions SET
                    status = ?, response_action = ?, response_channel = ?,
                    responded_by = ?, responded_at = ?, approval_run_id = ?,
                    execution_scheduled_at = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (
                    target_status,
                    action,
                    channel,
                    responded_by,
                    responded_at,
                    approval_run_id,
                    execution_scheduled_at,
                    responded_at,
                    decision_id,
                ),
            )
            if cursor.rowcount != 1:
                raise DecisionTransitionError(
                    "Decision response lost a concurrent transition"
                )
            connection.commit()
    except DecisionError:
        raise
    except sqlite3.Error as error:
        raise DecisionError("Decision response could not be recorded") from error
    return DecisionTransitionResult(
        decision=get_decision(path, decision_id),
        changed=True,
    )


def respond_to_decision(
    path: Path,
    decision_id: str,
    action: str,
    *,
    channel: str = "cli",
    responded_by: str = "local-user",
) -> Decision:
    """Preserve the Decision-only interface used by CLI callers."""
    return transition_decision(
        path,
        decision_id,
        action,
        channel=channel,
        responded_by=responded_by,
    ).decision


def list_pending_approval_run_triggers(path: Path) -> list[str]:
    try:
        with _decision_connection(path) as connection:
            rows = connection.execute(
                """
                SELECT approval_run_id
                FROM approval_run_trigger_requests
                WHERE requested_at IS NULL
                ORDER BY created_at, approval_run_id
                """
            ).fetchall()
    except DecisionError:
        raise
    except sqlite3.Error as error:
        raise DecisionError(
            "approval Run trigger requests could not be listed"
        ) from error
    return [str(row["approval_run_id"]) for row in rows]


def mark_approval_run_trigger_requested(
    path: Path,
    approval_run_id: str,
) -> bool:
    try:
        with _decision_connection(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT created_at FROM approval_run_trigger_requests
                WHERE approval_run_id = ?
                """,
                (approval_run_id,),
            ).fetchone()
            if row is None:
                raise DecisionError("approval Run trigger request does not exist")
            requested_at = _response_timestamp(str(row["created_at"]))
            cursor = connection.execute(
                """
                UPDATE approval_run_trigger_requests
                SET requested_at = ?
                WHERE approval_run_id = ? AND requested_at IS NULL
                """,
                (requested_at, approval_run_id),
            )
            connection.commit()
    except DecisionError:
        raise
    except sqlite3.Error as error:
        raise DecisionError(
            "approval Run trigger request could not be recorded"
        ) from error
    return cursor.rowcount == 1


def search_decisions(path: Path, query: str) -> list[Decision]:
    normalized_query = query.strip()
    if not normalized_query:
        raise DecisionError("search query must not be empty")
    if len(normalized_query) > 500:
        raise DecisionError("search query must be at most 500 characters")
    fts_query = _literal_fts_query(normalized_query)
    try:
        with _decision_connection(path) as connection:
            rows = connection.execute(
                f"""
                SELECT decisions.*,
                       {_NOTIFICATION_SELECT_COLUMNS},
                       {_EXECUTION_SELECT_COLUMNS}
                FROM decision_fts
                JOIN decisions ON decisions.id = decision_fts.decision_id
                JOIN decision_notification_deliveries AS notification
                  ON notification.decision_id = decisions.id
                LEFT JOIN decision_execution_outcomes AS execution
                  ON execution.decision_id = decisions.id
                WHERE decision_fts MATCH ?
                ORDER BY bm25(decision_fts), decisions.created_at DESC
                """,
                (fts_query,),
            ).fetchall()
    except sqlite3.OperationalError as error:
        raise DecisionError("Decision search failed") from error
    except DecisionError:
        raise
    except sqlite3.Error as error:
        raise DecisionError("Decision search failed") from error
    return [_decision_from_row(row) for row in rows]


_DECISION_SELECT = f"""
    SELECT decisions.*,
           {_NOTIFICATION_SELECT_COLUMNS},
           {_EXECUTION_SELECT_COLUMNS}
    FROM decisions
    JOIN decision_notification_deliveries AS notification
      ON notification.decision_id = decisions.id
    LEFT JOIN decision_execution_outcomes AS execution
      ON execution.decision_id = decisions.id
"""


def canonical_plan_json(plan: dict[str, object]) -> str:
    """Return the exact deterministic representation covered by plan_hash."""
    return _canonical_json(plan)


def _decision_from_row(row: sqlite3.Row) -> Decision:
    plan = _verified_plan(row)
    stored_status = str(row["status"])
    execution_status = row["execution_status"]
    status = (
        stored_status
        if execution_status is None
        else str(execution_status)
    )
    if stored_status not in {"pending", "approved", "rejected"}:
        raise DecisionError("stored Decision status is invalid")
    if execution_status is not None and stored_status != "approved":
        raise DecisionError("stored Decision execution status is invalid")
    if status not in DECISION_STATUSES:
        raise DecisionError("stored Decision status is invalid")
    response: DecisionResponse | None = None
    if row["response_action"] is not None:
        if (
            row["response_channel"] is None
            or row["responded_by"] is None
            or row["responded_at"] is None
        ):
            raise DecisionError("stored Decision response is incomplete")
        response = DecisionResponse(
            action=str(row["response_action"]),
            channel=str(row["response_channel"]),
            responded_by=str(row["responded_by"]),
            responded_at=str(row["responded_at"]),
        )
    notification_status = str(row["notification_status"])
    if notification_status not in {
        "pending",
        "retryable-failure",
        "delivered",
    }:
        raise DecisionError("stored Decision notification status is invalid")
    notification = DecisionNotificationDelivery(
        status=notification_status,
        attempts=int(row["notification_attempts"]),
        last_attempt_at=(
            None
            if row["notification_last_attempt_at"] is None
            else str(row["notification_last_attempt_at"])
        ),
        last_error=(
            None
            if row["notification_last_error"] is None
            else str(row["notification_last_error"])
        ),
        delivered_at=(
            None
            if row["notification_delivered_at"] is None
            else str(row["notification_delivered_at"])
        ),
    )
    return Decision(
        id=str(row["id"]),
        task_id=str(row["task_id"]),
        run_id=str(row["run_id"]),
        status=status,
        plan=plan,
        plan_hash=str(row["plan_hash"]),
        reason=str(row["reason"]),
        validation_summary=str(row["validation_summary"]),
        rollback_summary=str(row["rollback_summary"]),
        notification_delivery=notification,
        response=response,
        approval_run_id=(
            None if row["approval_run_id"] is None else str(row["approval_run_id"])
        ),
        execution_scheduled_at=(
            None
            if row["execution_scheduled_at"] is None
            else str(row["execution_scheduled_at"])
        ),
        created_at=str(row["created_at"]),
        updated_at=(
            str(row["updated_at"])
            if row["execution_completed_at"] is None
            else str(row["execution_completed_at"])
        ),
    )


@contextmanager
def _decision_connection(path: Path) -> Iterator[sqlite3.Connection]:
    if not path.is_file():
        raise DecisionError("runtime is not initialized; run 'runtasks init' first")
    with database_connection(path, enable_wal=False) as connection:
        connection.row_factory = sqlite3.Row
        try:
            version_row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()
        except sqlite3.Error as error:
            raise DecisionError(
                "database schema is not current; run 'runtasks init'"
            ) from error
        version = 0 if version_row is None else int(version_row[0])
        if version != LATEST_SCHEMA_VERSION:
            raise DecisionError("database schema is not current; run 'runtasks init'")
        yield connection


def _verified_plan(row: sqlite3.Row) -> dict[str, object]:
    plan_json = str(row["plan_json"])
    try:
        plan_value = json.loads(plan_json)
    except json.JSONDecodeError as error:
        raise DecisionError("stored Decision plan is invalid") from error
    if not isinstance(plan_value, dict):
        raise DecisionError("stored Decision plan is invalid")
    plan = cast(dict[str, object], plan_value)
    canonical = canonical_plan_json(plan)
    computed_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if computed_hash != str(row["plan_hash"]):
        raise DecisionError("stored Decision plan hash does not match its plan")
    return plan


def _literal_fts_query(query: str) -> str:
    terms = re.findall(r"\w+", query, flags=re.UNICODE)
    if not terms:
        raise DecisionError("search query must contain searchable text")
    return " AND ".join(f'"{term}"' for term in terms)


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise DecisionError("Decision content is not JSON-compatible") from error


def _response_timestamp(created_at: str) -> str:
    now = datetime.now(timezone.utc)
    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    response_time = max(now, created)
    return response_time.isoformat(timespec="microseconds").replace("+00:00", "Z")
