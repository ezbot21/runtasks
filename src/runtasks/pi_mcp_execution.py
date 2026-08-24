from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Callable, Iterator, Mapping, Protocol, cast

from runtasks.database import LATEST_SCHEMA_VERSION, database_connection
from runtasks.decisions import canonical_plan_json
from runtasks.redaction import Redactor
from runtasks.runs import Run, get_run


MAX_PLAN_BYTES = 65_536
_NOTIFICATION_CLAIM_TIMEOUT = timedelta(minutes=5)
_STABLE_SEMVER = re.compile(
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:\+[0-9A-Za-z.-]+)?\Z"
)


class PiMcpExecutionError(RuntimeError):
    """Raised when an approved Pi MCP update cannot execute safely."""


class PiMcpExecutionAdapterError(PiMcpExecutionError):
    """Raised when an external execution adapter reports an unsafe result."""


def is_exact_stable_version(value: str) -> bool:
    return _STABLE_SEMVER.fullmatch(value) is not None


class PackageAdapter(Protocol):
    def installed_version(self) -> str: ...

    def install_exact(self, version: str) -> None: ...


class ServiceAdapter(Protocol):
    def restart(self, service_name: str) -> None: ...


class HealthAdapter(Protocol):
    def check(self, service_name: str) -> str: ...


class McpValidationAdapter(Protocol):
    def validate_mcp(self, expected_result: str) -> str: ...


class ExecutionNotificationAdapter(Protocol):
    def send(self, text: str) -> None: ...


@dataclass(frozen=True)
class PiMcpExecutionAdapters:
    package: PackageAdapter
    service: ServiceAdapter
    health: HealthAdapter
    mcp_validation: McpValidationAdapter
    notification: ExecutionNotificationAdapter


@dataclass(frozen=True)
class ApprovedPiMcpPlan:
    decision_id: str
    approval_run_id: str
    task_id: str
    task_name: str
    plan_hash: str
    old_version: str
    target_version: str
    service_name: str
    expected_mcp_result: str


@dataclass(frozen=True)
class _ApprovalClaimResult:
    plan: ApprovedPiMcpPlan | None
    completed_run_id: str | None


@dataclass(frozen=True)
class _ExecutionOutcome:
    status: str
    summary: str
    details: dict[str, object]


@dataclass(frozen=True)
class _PendingSuccessNotification:
    decision_id: str
    approval_run_id: str
    task_name: str
    old_version: str
    target_version: str
    expected_mcp_result: str


def execute_approved_pi_mcp_runs(
    path: Path,
    adapters: PiMcpExecutionAdapters,
    redactor: Redactor,
    timestamp_factory: Callable[[], str] | None = None,
) -> tuple[Run, ...]:
    timestamps = _utc_now if timestamp_factory is None else timestamp_factory
    _deliver_pending_success_notifications(
        path,
        adapters.notification,
        redactor,
        timestamps,
    )
    completed: list[Run] = []
    while True:
        claim_result = _claim_next_approved_plan(
            path,
            timestamps(),
            redactor,
        )
        if claim_result is None:
            break
        if claim_result.plan is None:
            if claim_result.completed_run_id is None:
                raise PiMcpExecutionError(
                    "invalid approval claim result is incomplete"
                )
            completed.append(get_run(path, claim_result.completed_run_id))
            continue
        plan = claim_result.plan
        outcome = _execute_plan(plan, adapters, redactor)
        _complete_execution(path, plan, outcome, timestamps())
        if outcome.status == "success":
            _deliver_pending_success_notifications(
                path,
                adapters.notification,
                redactor,
                timestamps,
                decision_id=plan.decision_id,
            )
        completed.append(get_run(path, plan.approval_run_id))
    return tuple(completed)


def _execute_plan(
    plan: ApprovedPiMcpPlan,
    adapters: PiMcpExecutionAdapters,
    redactor: Redactor,
) -> _ExecutionOutcome:
    steps: list[dict[str, object]] = [
        {
            "name": "plan-verification",
            "status": "success",
            "summary": "Approved plan hash and registered handler verified.",
        }
    ]
    mutation_performed = False
    try:
        installed_before = adapters.package.installed_version()
        _require_exact_version(installed_before, "installed adapter version")
        if installed_before != plan.old_version:
            raise PiMcpExecutionAdapterError(
                "installed adapter version no longer matches the approved old version"
            )
        steps.append(
            {
                "name": "old-version-precondition",
                "status": "success",
                "summary": f"Installed version {installed_before} matches the approved plan.",
            }
        )

        adapters.package.install_exact(plan.target_version)
        mutation_performed = True
        steps.append(
            {
                "name": "install-exact-version",
                "status": "success",
                "summary": f"Installed exact approved version {plan.target_version}.",
            }
        )

        installed_after = adapters.package.installed_version()
        _require_exact_version(installed_after, "installed adapter version")
        if installed_after != plan.target_version:
            raise PiMcpExecutionAdapterError(
                "installed package metadata does not match the approved target version"
            )
        steps.append(
            {
                "name": "target-metadata-verification",
                "status": "success",
                "summary": f"Package metadata reports exact version {installed_after}.",
            }
        )

        adapters.service.restart(plan.service_name)
        steps.append(
            {
                "name": "pi-web-restart",
                "status": "success",
                "summary": "Restarted pi-web.service through the service adapter.",
            }
        )
        health = adapters.health.check(plan.service_name)
        if health != "healthy":
            raise PiMcpExecutionAdapterError(
                "Pi Web health result is ambiguous"
            )
        steps.append(
            {
                "name": "pi-web-health",
                "status": "success",
                "summary": "Pi Web is healthy.",
            }
        )
        validation = adapters.mcp_validation.validate_mcp(
            plan.expected_mcp_result
        )
        if validation != plan.expected_mcp_result:
            raise PiMcpExecutionAdapterError(
                "MCP validation result is ambiguous"
            )
        steps.append(
            {
                "name": "mcp-validation",
                "status": "success",
                "summary": f"Fresh Pi validation returned exact {validation}.",
            }
        )
    except Exception as error:
        safe_error = redactor.text(str(error))
        steps.append(
            {
                "name": "execution-failure",
                "status": "failed",
                "summary": safe_error,
            }
        )
        return _ExecutionOutcome(
            status="failed",
            summary=f"Approved Pi MCP adapter update failed: {safe_error}",
            details=_execution_details(
                plan,
                mutation_performed=mutation_performed,
                steps=steps,
            ),
        )

    return _ExecutionOutcome(
        status="success",
        summary=(
            f"Pi MCP adapter updated from {plan.old_version} to "
            f"{plan.target_version}; Pi Web is healthy and MCP_ADAPTER_OK was verified. "
            "Rollback was not required."
        ),
        details={
            **_execution_details(
                plan,
                mutation_performed=True,
                steps=steps,
            ),
            "mcp_validation": plan.expected_mcp_result,
            "pi_web_health": "healthy",
            "rollback": {"required": False, "status": "not-required"},
        },
    )


def _claim_next_approved_plan(
    path: Path,
    started_at: str,
    redactor: Redactor,
) -> _ApprovalClaimResult | None:
    try:
        with _execution_connection(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT runs.id AS approval_run_id,
                       runs.task_id,
                       runs.task_name,
                       decisions.id AS decision_id,
                       decisions.run_id AS requesting_run_id,
                       decisions.status AS decision_status,
                       decisions.response_action,
                       decisions.plan_json,
                       decisions.plan_hash,
                       tasks.action_mode,
                       tasks.handler,
                       tasks.removed_at
                FROM runs
                LEFT JOIN decisions
                  ON decisions.approval_run_id = runs.id
                JOIN tasks ON tasks.id = runs.task_id
                WHERE runs.trigger = 'approval'
                  AND runs.status = 'claimed'
                  AND tasks.handler = 'pi_mcp_adapter'
                  AND tasks.action_mode = 'approved-procedure'
                ORDER BY runs.created_at, runs.id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            try:
                plan = _approved_plan_from_row(row)
            except PiMcpExecutionError as error:
                run_id = str(row["approval_run_id"])
                _record_invalid_claim(
                    connection,
                    row,
                    run_id=run_id,
                    finished_at=started_at,
                    error=redactor.text(str(error)),
                )
                connection.commit()
                return _ApprovalClaimResult(
                    plan=None,
                    completed_run_id=run_id,
                )
            cursor = connection.execute(
                """
                UPDATE runs SET status = 'running', started_at = ?
                WHERE id = ? AND status = 'claimed'
                """,
                (started_at, plan.approval_run_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            connection.commit()
            return _ApprovalClaimResult(
                plan=plan,
                completed_run_id=None,
            )
    except PiMcpExecutionError:
        raise
    except sqlite3.Error as error:
        raise PiMcpExecutionError(
            "approved execution Run could not be claimed"
        ) from error


def _approved_plan_from_row(row: sqlite3.Row) -> ApprovedPiMcpPlan:
    if (
        row["decision_id"] is None
        or str(row["decision_status"]) != "approved"
        or str(row["response_action"]) != "approve"
    ):
        raise PiMcpExecutionError(
            "approval Run is not backed by an approved Decision"
        )
    if (
        str(row["handler"]) != "pi_mcp_adapter"
        or str(row["action_mode"]) != "approved-procedure"
        or row["removed_at"] is not None
    ):
        raise PiMcpExecutionError(
            "approval Run Task is not registered for Pi MCP execution"
        )
    plan_json = str(row["plan_json"])
    if len(plan_json.encode("utf-8")) > MAX_PLAN_BYTES:
        raise PiMcpExecutionError("approved Decision plan is too large")
    try:
        plan_value: object = json.loads(plan_json)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise PiMcpExecutionError("approved Decision plan is malformed") from error
    if not isinstance(plan_value, dict):
        raise PiMcpExecutionError("approved Decision plan is malformed")
    plan = cast(dict[str, object], plan_value)
    canonical = canonical_plan_json(plan)
    computed_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    stored_hash = str(row["plan_hash"])
    if computed_hash != stored_hash:
        raise PiMcpExecutionError(
            "approved Decision plan hash does not match its plan"
        )
    if plan.get("handler") != "pi_mcp_adapter":
        raise PiMcpExecutionError("approved Decision handler is invalid")
    if plan.get("operation") != "install-exact-version":
        raise PiMcpExecutionError("approved Decision operation is invalid")
    parameters = _plan_mapping(plan, "parameters")
    validation = _plan_mapping(plan, "validation")
    rollback = _plan_mapping(plan, "rollback")
    old_version = _plan_text(parameters, "installed_version")
    target_version = _plan_text(parameters, "target_version")
    _require_exact_version(old_version, "approved old version")
    _require_exact_version(target_version, "approved target version")
    if old_version == target_version:
        raise PiMcpExecutionError("approved target version must differ from old version")
    if parameters.get("package") != "pi-mcp-adapter":
        raise PiMcpExecutionError("approved package identity is invalid")
    if parameters.get("package_spec") != f"npm:pi-mcp-adapter@{target_version}":
        raise PiMcpExecutionError("approved package pin is not exact")
    if parameters.get("task_id") != str(row["task_id"]):
        raise PiMcpExecutionError("approved plan Task identity is invalid")
    if parameters.get("run_id") != str(row["requesting_run_id"]):
        raise PiMcpExecutionError("approved plan Run identity is invalid")
    if rollback.get("target_version") != old_version:
        raise PiMcpExecutionError("approved rollback version is invalid")
    service_name = _plan_text(validation, "restart_service")
    if service_name != "pi-web.service":
        raise PiMcpExecutionError("approved restart service is invalid")
    if validation.get("expected_installed_version") != target_version:
        raise PiMcpExecutionError(
            "approved metadata validation target is invalid"
        )
    expected_mcp_result = _plan_text(validation, "expected_mcp_result")
    if expected_mcp_result != "MCP_ADAPTER_OK":
        raise PiMcpExecutionError("approved MCP validation result is invalid")
    return ApprovedPiMcpPlan(
        decision_id=str(row["decision_id"]),
        approval_run_id=str(row["approval_run_id"]),
        task_id=str(row["task_id"]),
        task_name=str(row["task_name"]),
        plan_hash=stored_hash,
        old_version=old_version,
        target_version=target_version,
        service_name=service_name,
        expected_mcp_result=expected_mcp_result,
    )


def _record_invalid_claim(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    run_id: str,
    finished_at: str,
    error: str,
) -> None:
    decision_id = (
        None if row["decision_id"] is None else str(row["decision_id"])
    )
    details = {
        "decision_id": decision_id,
        "handler": "pi_mcp_adapter",
        "mutation_performed": False,
        "plan_hash": (
            None if row["plan_hash"] is None else str(row["plan_hash"])
        ),
        "steps": [
            {
                "name": "plan-verification",
                "status": "failed",
                "summary": error,
            }
        ],
    }
    summary = f"Approved Pi MCP adapter update was rejected: {error}"
    cursor = connection.execute(
        """
        UPDATE runs SET status = 'failed', started_at = ?, finished_at = ?,
                        summary = ?, details_json = ?, external_log_ref = NULL
        WHERE id = ? AND status = 'claimed'
        """,
        (
            finished_at,
            finished_at,
            summary,
            _canonical_json(details),
            run_id,
        ),
    )
    if cursor.rowcount != 1:
        raise PiMcpExecutionError(
            "invalid approval Run lost a concurrent transition"
        )
    if (
        decision_id is not None
        and str(row["decision_status"]) == "approved"
        and str(row["response_action"]) == "approve"
    ):
        connection.execute(
            """
            INSERT INTO decision_execution_outcomes(
                decision_id, approval_run_id, status, summary,
                details_json, completed_at, notification_status,
                notification_attempts
            ) VALUES (?, ?, 'failed', ?, ?, ?, 'not-required', 0)
            """,
            (
                decision_id,
                run_id,
                summary,
                _canonical_json(details),
                finished_at,
            ),
        )


def _complete_execution(
    path: Path,
    plan: ApprovedPiMcpPlan,
    outcome: _ExecutionOutcome,
    finished_at: str,
) -> None:
    safe_details = _canonical_json(outcome.details)
    decision_status = "completed" if outcome.status == "success" else "failed"
    try:
        with _execution_connection(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE runs SET status = ?, finished_at = ?, summary = ?,
                                details_json = ?, external_log_ref = NULL
                WHERE id = ? AND status = 'running'
                """,
                (
                    outcome.status,
                    finished_at,
                    outcome.summary,
                    safe_details,
                    plan.approval_run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise PiMcpExecutionError(
                    "approval Run completion lost a concurrent transition"
                )
            connection.execute(
                """
                INSERT INTO decision_execution_outcomes(
                    decision_id, approval_run_id, status, summary,
                    details_json, completed_at, notification_status,
                    notification_attempts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    plan.decision_id,
                    plan.approval_run_id,
                    decision_status,
                    outcome.summary,
                    safe_details,
                    finished_at,
                    (
                        "pending"
                        if outcome.status == "success"
                        else "not-required"
                    ),
                ),
            )
            connection.commit()
    except PiMcpExecutionError:
        raise
    except sqlite3.IntegrityError as error:
        raise PiMcpExecutionError(
            "approved execution was already completed"
        ) from error
    except sqlite3.Error as error:
        raise PiMcpExecutionError(
            "approved execution outcome could not be recorded"
        ) from error


def _execution_details(
    plan: ApprovedPiMcpPlan,
    *,
    mutation_performed: bool,
    steps: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "decision_id": plan.decision_id,
        "handler": "pi_mcp_adapter",
        "mutation_performed": mutation_performed,
        "new_version": plan.target_version,
        "old_version": plan.old_version,
        "plan_hash": plan.plan_hash,
        "steps": steps,
    }


def _deliver_pending_success_notifications(
    path: Path,
    notification_adapter: ExecutionNotificationAdapter,
    redactor: Redactor,
    timestamp_factory: Callable[[], str],
    *,
    decision_id: str | None = None,
) -> None:
    while True:
        attempted_at = timestamp_factory()
        pending = _claim_success_notification(
            path,
            claimed_at=attempted_at,
            decision_id=decision_id,
        )
        if pending is None:
            return
        try:
            notification_adapter.send(_success_notification(pending))
        except Exception as error:
            _record_success_notification(
                path,
                pending,
                delivered=False,
                attempted_at=attempted_at,
                error=redactor.text(str(error)),
            )
            return
        _record_success_notification(
            path,
            pending,
            delivered=True,
            attempted_at=attempted_at,
            error=None,
        )
        if decision_id is not None:
            return


def _release_expired_notification_claims(
    connection: sqlite3.Connection,
    *,
    claimed_at: str,
) -> None:
    try:
        current = datetime.fromisoformat(claimed_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise PiMcpExecutionError(
            "notification claim timestamp is invalid"
        ) from error
    if current.tzinfo is None or current.utcoffset() is None:
        raise PiMcpExecutionError(
            "notification claim timestamp is invalid"
        )
    cutoff = (current - _NOTIFICATION_CLAIM_TIMEOUT).astimezone(timezone.utc)
    cutoff_text = cutoff.isoformat(timespec="microseconds").replace("+00:00", "Z")
    connection.execute(
        """
        UPDATE decision_execution_outcomes SET
            notification_status = 'retryable-failure',
            notification_attempts = notification_attempts + 1,
            notification_claimed_at = NULL,
            notification_last_attempt_at = ?,
            notification_last_error = ?,
            notification_delivered_at = NULL
        WHERE notification_status = 'sending'
          AND notification_claimed_at <= ?
        """,
        (
            claimed_at,
            "previous success notification delivery was interrupted",
            cutoff_text,
        ),
    )


def _claim_success_notification(
    path: Path,
    *,
    claimed_at: str,
    decision_id: str | None,
) -> _PendingSuccessNotification | None:
    where_decision = "" if decision_id is None else "AND execution.decision_id = ?"
    parameters: tuple[object, ...] = () if decision_id is None else (decision_id,)
    try:
        with _execution_connection(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            _release_expired_notification_claims(
                connection,
                claimed_at=claimed_at,
            )
            row = connection.execute(
                f"""
                SELECT execution.decision_id, execution.approval_run_id,
                       execution.details_json, tasks.name AS task_name
                FROM decision_execution_outcomes AS execution
                JOIN decisions ON decisions.id = execution.decision_id
                JOIN tasks ON tasks.id = decisions.task_id
                WHERE execution.status = 'completed'
                  AND execution.notification_status IN (
                      'pending', 'retryable-failure'
                  )
                  {where_decision}
                ORDER BY execution.completed_at, execution.decision_id
                LIMIT 1
                """,
                parameters,
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            cursor = connection.execute(
                """
                UPDATE decision_execution_outcomes
                SET notification_status = 'sending',
                    notification_claimed_at = ?
                WHERE decision_id = ?
                  AND notification_status IN ('pending', 'retryable-failure')
                """,
                (claimed_at, str(row["decision_id"])),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            details = _stored_execution_details(str(row["details_json"]))
            pending = _PendingSuccessNotification(
                decision_id=str(row["decision_id"]),
                approval_run_id=str(row["approval_run_id"]),
                task_name=str(row["task_name"]),
                old_version=_stored_detail_text(details, "old_version"),
                target_version=_stored_detail_text(details, "new_version"),
                expected_mcp_result=_stored_detail_text(
                    details,
                    "mcp_validation",
                ),
            )
            connection.commit()
            return pending
    except PiMcpExecutionError:
        raise
    except sqlite3.Error as error:
        raise PiMcpExecutionError(
            "success notification could not be claimed"
        ) from error


def _record_success_notification(
    path: Path,
    pending: _PendingSuccessNotification,
    *,
    delivered: bool,
    attempted_at: str,
    error: str | None,
) -> None:
    status = "delivered" if delivered else "retryable-failure"
    notification_details = {
        "attempted_at": attempted_at,
        "status": status,
    }
    if error is not None:
        notification_details["error"] = error
    try:
        with _execution_connection(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT details_json, notification_attempts
                FROM decision_execution_outcomes
                WHERE decision_id = ? AND notification_status = 'sending'
                """,
                (pending.decision_id,),
            ).fetchone()
            if row is None:
                raise PiMcpExecutionError(
                    "success notification claim is no longer current"
                )
            details = _stored_execution_details(str(row["details_json"]))
            details["notification"] = notification_details
            steps = details.get("steps")
            if not isinstance(steps, list):
                raise PiMcpExecutionError(
                    "stored execution steps are invalid"
                )
            steps.append(
                {
                    "name": "success-notification",
                    "status": "success" if delivered else "failed",
                    "summary": (
                        "Sent the redacted update success notification."
                        if delivered
                        else f"Success notification will be retried: {error}"
                    ),
                }
            )
            details_json = _canonical_json(details)
            attempts = int(row["notification_attempts"]) + 1
            connection.execute(
                """
                UPDATE decision_execution_outcomes SET
                    details_json = ?, notification_status = ?,
                    notification_attempts = ?, notification_claimed_at = NULL,
                    notification_last_attempt_at = ?,
                    notification_last_error = ?, notification_delivered_at = ?
                WHERE decision_id = ? AND notification_status = 'sending'
                """,
                (
                    details_json,
                    status,
                    attempts,
                    attempted_at,
                    error,
                    attempted_at if delivered else None,
                    pending.decision_id,
                ),
            )
            connection.execute(
                """
                UPDATE runs SET details_json = ?
                WHERE id = ? AND status = 'success'
                """,
                (details_json, pending.approval_run_id),
            )
            connection.commit()
    except PiMcpExecutionError:
        raise
    except sqlite3.Error as sql_error:
        raise PiMcpExecutionError(
            "success notification outcome could not be recorded"
        ) from sql_error


def _stored_execution_details(value: str) -> dict[str, object]:
    try:
        details: object = json.loads(value)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise PiMcpExecutionError(
            "stored execution details are invalid"
        ) from error
    if not isinstance(details, dict):
        raise PiMcpExecutionError("stored execution details are invalid")
    return cast(dict[str, object], details)


def _stored_detail_text(details: Mapping[str, object], name: str) -> str:
    value = details.get(name)
    if not isinstance(value, str) or not value:
        raise PiMcpExecutionError(
            f"stored execution {name} is invalid"
        )
    return value


def _success_notification(pending: _PendingSuccessNotification) -> str:
    return f"""RunTasks update completed successfully

Task: {pending.task_name}
Updated: {pending.old_version} → {pending.target_version}
Pi Web: Healthy
Validation: {pending.expected_mcp_result}
Rollback: Not required

Open a fresh terminal Pi session and reopen existing terminal Pi sessions so they load the new adapter version."""


def _require_exact_version(value: str, name: str) -> None:
    if not is_exact_stable_version(value):
        raise PiMcpExecutionError(f"{name} is not an exact stable version")


def _plan_mapping(plan: Mapping[str, object], name: str) -> dict[str, object]:
    value = plan.get(name)
    if not isinstance(value, dict):
        raise PiMcpExecutionError(f"approved Decision {name} is invalid")
    return cast(dict[str, object], value)


def _plan_text(plan: Mapping[str, object], name: str) -> str:
    value = plan.get(name)
    if not isinstance(value, str) or not value:
        raise PiMcpExecutionError(f"approved Decision {name} is invalid")
    return value


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
        raise PiMcpExecutionError(
            "approved execution outcome is not JSON-compatible"
        ) from error


@contextmanager
def _execution_connection(path: Path) -> Iterator[sqlite3.Connection]:
    if not path.is_file():
        raise PiMcpExecutionError(
            "runtime is not initialized; run 'runtasks init' first"
        )
    with database_connection(path, enable_wal=False) as connection:
        connection.row_factory = sqlite3.Row
        try:
            version_row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()
        except sqlite3.Error as error:
            raise PiMcpExecutionError(
                "database schema is not current; run 'runtasks init'"
            ) from error
        version = 0 if version_row is None else int(version_row[0])
        if version != LATEST_SCHEMA_VERSION:
            raise PiMcpExecutionError(
                "database schema is not current; run 'runtasks init'"
            )
        yield connection


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
