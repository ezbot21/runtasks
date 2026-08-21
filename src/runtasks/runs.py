from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Iterator, Mapping, cast
import uuid

from runtasks.adapters import ExternalAdapter
from runtasks.database import LATEST_SCHEMA_VERSION, database_connection
from runtasks.handlers import (
    HandlerContext,
    HandlerError,
    HandlerOutcome,
    HandlerRegistry,
)
from runtasks.redaction import Redactor
from runtasks.tasks import Task, TaskError, get_task


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

    def as_dict(self) -> dict[str, object]:
        return {
            "created_at": self.created_at,
            "details": self.details,
            "external_log_ref": self.external_log_ref,
            "finished_at": self.finished_at,
            "id": self.id,
            "started_at": self.started_at,
            "status": self.status,
            "summary": self.summary,
            "task_id": self.task_id,
            "task_name": self.task_name,
            "trigger": self.trigger,
        }


def execute_manual_run(
    path: Path,
    task_id: str,
    external_adapter: ExternalAdapter,
    handler_registry: HandlerRegistry,
    redactor: Redactor,
) -> Run:
    task = get_task(path, task_id)
    run_id = _create_run(path, task, trigger="manual")
    _transition_run(path, run_id, "running", started_at=_utc_now())

    try:
        handler = handler_registry.get(task.handler)
        outcome = handler.execute(
            HandlerContext(run_id=run_id, task=task, trigger="manual"),
            external_adapter,
        )
        safe_outcome = _normalize_handler_outcome(outcome, redactor)
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

    _transition_run(
        path,
        run_id,
        safe_outcome.status,
        finished_at=_utc_now(),
        summary=safe_outcome.summary,
        details=safe_outcome.details,
        external_log_ref=safe_outcome.external_log_ref,
    )
    return get_run(path, run_id)


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
                "SELECT status FROM runs WHERE id = ?",
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
                details_json = _canonical_details(details)
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
    )


def _canonical_details(details: dict[str, object]) -> str:
    try:
        return json.dumps(
            details,
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
