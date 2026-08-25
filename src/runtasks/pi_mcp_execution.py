from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import os
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, BinaryIO, Callable, Iterator, Mapping, Protocol, cast

from runtasks.database import LATEST_SCHEMA_VERSION, database_connection
from runtasks.decisions import canonical_plan_json
from runtasks.redaction import Redactor
from runtasks.runs import Run, get_run


MAX_PLAN_BYTES = 65_536
_NOTIFICATION_CLAIM_TIMEOUT = timedelta(minutes=5)
_LOCK_MODULE: Any = importlib.import_module(
    "msvcrt" if os.name == "nt" else "fcntl"
)
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
    lock_path: Path


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
    decision_status: str
    summary: str
    details: dict[str, object]
    notification_required: bool = False
    fresh_check_required: bool = False


@dataclass(frozen=True)
class _RecoveryState:
    phase: str
    failed_step: str | None
    failure_summary: str | None
    pending_outcome_json: str | None


@dataclass(frozen=True)
class _PendingExecutionNotification:
    decision_id: str
    approval_run_id: str
    task_name: str
    outcome_status: str
    old_version: str
    target_version: str
    expected_mcp_result: str | None
    failed_step: str | None
    rollback: dict[str, object] | None


def execute_approved_pi_mcp_runs(
    path: Path,
    adapters: PiMcpExecutionAdapters,
    redactor: Redactor,
    timestamp_factory: Callable[[], str] | None = None,
    *,
    fresh_check_at: str | None = None,
    execution_lock_held: bool = False,
) -> tuple[Run, ...]:
    if execution_lock_held:
        return _execute_approved_pi_mcp_runs_locked(
            path,
            adapters,
            redactor,
            timestamp_factory,
            fresh_check_at=fresh_check_at,
        )
    with pi_mcp_execution_guard(adapters.lock_path) as acquired:
        if not acquired:
            return ()
        return _execute_approved_pi_mcp_runs_locked(
            path,
            adapters,
            redactor,
            timestamp_factory,
            fresh_check_at=fresh_check_at,
        )


def _execute_approved_pi_mcp_runs_locked(
    path: Path,
    adapters: PiMcpExecutionAdapters,
    redactor: Redactor,
    timestamp_factory: Callable[[], str] | None,
    *,
    fresh_check_at: str | None,
) -> tuple[Run, ...]:
    timestamps = _utc_now if timestamp_factory is None else timestamp_factory
    _deliver_pending_execution_notifications(
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
        outcome = _execute_plan(path, plan, adapters, redactor, timestamps)
        _complete_execution(
            path,
            plan,
            outcome,
            timestamps(),
            fresh_check_at=fresh_check_at,
        )
        if outcome.notification_required:
            _deliver_pending_execution_notifications(
                path,
                adapters.notification,
                redactor,
                timestamps,
                decision_id=plan.decision_id,
            )
        completed.append(get_run(path, plan.approval_run_id))
    return tuple(completed)


def _execute_plan(
    path: Path,
    plan: ApprovedPiMcpPlan,
    adapters: PiMcpExecutionAdapters,
    redactor: Redactor,
    timestamp_factory: Callable[[], str],
) -> _ExecutionOutcome:
    recovery = _get_recovery_state(path, plan.decision_id)
    if recovery.pending_outcome_json is not None:
        return _stored_pending_outcome(recovery.pending_outcome_json)
    outcome = _execute_plan_uncheckpointed(
        path,
        plan,
        adapters,
        redactor,
        timestamp_factory,
        recovery,
    )
    _checkpoint_execution_outcome(
        path,
        plan.decision_id,
        outcome,
        timestamp_factory(),
    )
    return outcome


def _execute_plan_uncheckpointed(
    path: Path,
    plan: ApprovedPiMcpPlan,
    adapters: PiMcpExecutionAdapters,
    redactor: Redactor,
    timestamp_factory: Callable[[], str],
    recovery: _RecoveryState,
) -> _ExecutionOutcome:
    steps: list[dict[str, object]] = [
        {
            "name": "plan-verification",
            "status": "success",
            "summary": "Approved plan hash and registered handler verified.",
        }
    ]
    if recovery.phase == "rollback-required":
        return _start_rollback_install(
            path,
            plan,
            adapters,
            redactor,
            timestamp_factory,
            recovery.failed_step or "unknown-update-step",
            recovery.failure_summary or "approved update failed",
            steps,
        )
    if recovery.phase == "rollback-install-started":
        return _resume_interrupted_rollback(
            plan,
            adapters,
            redactor,
            recovery,
            steps,
        )
    if recovery.phase == "rollback-installed":
        return _finish_rollback(
            plan,
            adapters,
            redactor,
            recovery.failed_step or "unknown-update-step",
            recovery.failure_summary or "approved update was interrupted",
            steps,
        )

    if recovery.phase == "target-install-started":
        try:
            observed = adapters.package.installed_version()
            _require_exact_version(observed, "installed adapter version")
        except Exception as error:
            return _begin_rollback(
                path,
                plan,
                adapters,
                redactor,
                timestamp_factory,
                "install-exact-version",
                redactor.text(str(error)),
                steps,
            )
        if observed == plan.target_version:
            _set_recovery_phase(
                path,
                plan.decision_id,
                "target-installed",
                timestamp_factory(),
            )
            recovery = _RecoveryState("target-installed", None, None, None)
            steps.append(
                {
                    "name": "install-exact-version",
                    "status": "success",
                    "summary": (
                        f"Recovered exact approved version {plan.target_version} "
                        "after an interrupted execution."
                    ),
                }
            )
        elif observed == plan.old_version:
            safe_error = (
                "target installation start was interrupted before mutation could be "
                "distinguished from a partial install; no package operation was repeated"
            )
            steps.append(_failed_step("install-exact-version", safe_error))
            return _execution_ambiguous_outcome(
                plan,
                "install-exact-version",
                safe_error,
                steps,
                observed_version=observed,
            )
        else:
            return _begin_rollback(
                path,
                plan,
                adapters,
                redactor,
                timestamp_factory,
                "install-exact-version",
                (
                    "interrupted target installation left an unexpected exact "
                    f"version {observed}"
                ),
                steps,
            )

    if recovery.phase == "execution-started":
        try:
            installed_before = adapters.package.installed_version()
            _require_exact_version(installed_before, "installed adapter version")
        except Exception as error:
            safe_error = redactor.text(str(error))
            steps.append(_failed_step("old-version-precondition", safe_error))
            return _failed_update_outcome(plan, safe_error, steps)
        if installed_before != plan.old_version:
            summary = (
                "Approved Pi MCP adapter plan is stale: installed version "
                f"{installed_before} no longer matches approved old version "
                f"{plan.old_version}. A fresh check is required."
            )
            steps.append(_failed_step("old-version-precondition", summary))
            return _ExecutionOutcome(
                status="failed",
                decision_status="superseded",
                summary=summary,
                details={
                    **_execution_details(
                        plan,
                        mutation_performed=False,
                        steps=steps,
                    ),
                    "observed_version": installed_before,
                    "outcome": "stale-plan",
                    "rollback": {
                        "attempted": False,
                        "mcp_validation": "not-checked",
                        "pi_web_health": "not-checked",
                        "required": False,
                        "restored_version": None,
                        "status": "not-required",
                        "target_version": plan.old_version,
                    },
                },
                fresh_check_required=True,
            )
        steps.append(
            {
                "name": "old-version-precondition",
                "status": "success",
                "summary": f"Installed version {installed_before} matches the approved plan.",
            }
        )
        _set_recovery_phase(
            path,
            plan.decision_id,
            "target-install-started",
            timestamp_factory(),
        )
        try:
            adapters.package.install_exact(plan.target_version)
        except Exception as error:
            safe_error = redactor.text(str(error))
            steps.append(_failed_step("install-exact-version", safe_error))
            try:
                observed_after_failure = adapters.package.installed_version()
                _require_exact_version(
                    observed_after_failure,
                    "installed adapter version",
                )
            except Exception:
                observed_after_failure = None
            if observed_after_failure in {
                plan.target_version,
            } or (
                observed_after_failure is not None
                and observed_after_failure != plan.old_version
            ):
                return _begin_rollback(
                    path,
                    plan,
                    adapters,
                    redactor,
                    timestamp_factory,
                    "install-exact-version",
                    safe_error,
                    steps,
                )
            return _execution_ambiguous_outcome(
                plan,
                "install-exact-version",
                safe_error,
                steps,
                observed_version=observed_after_failure,
            )
        _set_recovery_phase(
            path,
            plan.decision_id,
            "target-installed",
            timestamp_factory(),
        )
        steps.append(
            {
                "name": "install-exact-version",
                "status": "success",
                "summary": f"Installed exact approved version {plan.target_version}.",
            }
        )

    update_steps: tuple[tuple[str, Callable[[], str], str], ...] = (
        (
            "target-metadata-verification",
            lambda: _verify_installed_version(adapters, plan.target_version),
            f"Package metadata reports exact version {plan.target_version}.",
        ),
        (
            "pi-web-restart",
            lambda: _restart_service(adapters, plan.service_name),
            "Restarted pi-web.service through the service adapter.",
        ),
        (
            "pi-web-health",
            lambda: _check_health(adapters, plan.service_name),
            "Pi Web is healthy.",
        ),
        (
            "mcp-validation",
            lambda: _validate_mcp(adapters, plan.expected_mcp_result),
            f"Fresh Pi validation returned exact {plan.expected_mcp_result}.",
        ),
    )
    for step_name, operation, success_summary in update_steps:
        try:
            operation()
        except Exception as error:
            safe_error = redactor.text(str(error))
            steps.append(_failed_step(step_name, safe_error))
            return _begin_rollback(
                path,
                plan,
                adapters,
                redactor,
                timestamp_factory,
                step_name,
                safe_error,
                steps,
            )
        steps.append(
            {"name": step_name, "status": "success", "summary": success_summary}
        )

    return _ExecutionOutcome(
        status="success",
        decision_status="completed",
        summary=(
            f"Pi MCP adapter updated from {plan.old_version} to "
            f"{plan.target_version}; Pi Web is healthy and MCP_ADAPTER_OK was verified. "
            "Rollback was not required."
        ),
        details={
            **_execution_details(plan, mutation_performed=True, steps=steps),
            "mcp_validation": plan.expected_mcp_result,
            "pi_web_health": "healthy",
            "rollback": {"required": False, "status": "not-required"},
        },
        notification_required=True,
    )


def _begin_rollback(
    path: Path,
    plan: ApprovedPiMcpPlan,
    adapters: PiMcpExecutionAdapters,
    redactor: Redactor,
    timestamp_factory: Callable[[], str],
    failed_step: str,
    failure_summary: str,
    steps: list[dict[str, object]],
) -> _ExecutionOutcome:
    _set_recovery_phase(
        path,
        plan.decision_id,
        "rollback-required",
        timestamp_factory(),
        failed_step=failed_step,
        failure_summary=failure_summary,
    )
    return _start_rollback_install(
        path,
        plan,
        adapters,
        redactor,
        timestamp_factory,
        failed_step,
        failure_summary,
        steps,
    )


def _start_rollback_install(
    path: Path,
    plan: ApprovedPiMcpPlan,
    adapters: PiMcpExecutionAdapters,
    redactor: Redactor,
    timestamp_factory: Callable[[], str],
    failed_step: str,
    failure_summary: str,
    steps: list[dict[str, object]],
) -> _ExecutionOutcome:
    _set_recovery_phase(
        path,
        plan.decision_id,
        "rollback-install-started",
        timestamp_factory(),
        failed_step=failed_step,
        failure_summary=failure_summary,
    )
    steps.append(
        {
            "name": "rollback-install-exact-version",
            "status": "started",
            "summary": f"Attempting exact rollback to {plan.old_version}.",
        }
    )
    try:
        adapters.package.install_exact(plan.old_version)
    except Exception as error:
        safe_error = redactor.text(str(error))
        try:
            observed_version = adapters.package.installed_version()
            _require_exact_version(observed_version, "installed adapter version")
        except Exception:
            observed_version = None
        rollback_status = (
            "failed"
            if observed_version is not None
            and observed_version != plan.old_version
            else "ambiguous"
        )
        steps.append(_failed_step("rollback-install-exact-version", safe_error))
        return _rollback_failed_outcome(
            plan,
            failed_step,
            failure_summary,
            rollback_status,
            steps,
            restored_version=observed_version,
            rollback_failure=safe_error,
        )
    _set_recovery_phase(
        path,
        plan.decision_id,
        "rollback-installed",
        timestamp_factory(),
        failed_step=failed_step,
        failure_summary=failure_summary,
    )
    steps.append(
        {
            "name": "rollback-install-exact-version",
            "status": "success",
            "summary": f"Reinstalled exact prior version {plan.old_version}.",
        }
    )
    return _finish_rollback(
        plan,
        adapters,
        redactor,
        failed_step,
        failure_summary,
        steps,
    )


def _resume_interrupted_rollback(
    plan: ApprovedPiMcpPlan,
    adapters: PiMcpExecutionAdapters,
    redactor: Redactor,
    recovery: _RecoveryState,
    steps: list[dict[str, object]],
) -> _ExecutionOutcome:
    failed_step = recovery.failed_step or "unknown-update-step"
    failure_summary = recovery.failure_summary or "approved update was interrupted"
    try:
        observed = adapters.package.installed_version()
        _require_exact_version(observed, "installed adapter version")
    except Exception as error:
        safe_error = redactor.text(str(error))
        steps.append(_failed_step("rollback-recovery-check", safe_error))
        return _rollback_failed_outcome(
            plan,
            failed_step,
            failure_summary,
            "ambiguous",
            steps,
            rollback_failure=safe_error,
        )
    safe_error = (
        "rollback installation start was interrupted, so package metadata cannot "
        "prove that the authorized reinstall completed; installation was not repeated"
    )
    steps.append(_failed_step("rollback-recovery-check", safe_error))
    return _rollback_failed_outcome(
        plan,
        failed_step,
        failure_summary,
        "ambiguous",
        steps,
        restored_version=observed,
        rollback_failure=safe_error,
    )


def _finish_rollback(
    plan: ApprovedPiMcpPlan,
    adapters: PiMcpExecutionAdapters,
    redactor: Redactor,
    failed_step: str,
    failure_summary: str,
    steps: list[dict[str, object]],
) -> _ExecutionOutcome:
    health_result = "not-checked"
    validation_result = "not-checked"
    try:
        restored_version = adapters.package.installed_version()
        _require_exact_version(restored_version, "installed adapter version")
    except Exception as error:
        safe_error = redactor.text(str(error))
        steps.append(_failed_step("rollback-metadata-verification", safe_error))
        return _rollback_failed_outcome(
            plan,
            failed_step,
            failure_summary,
            "failed",
            steps,
            rollback_failure=safe_error,
        )
    if restored_version != plan.old_version:
        safe_error = (
            "installed package metadata does not match the exact rollback version"
        )
        steps.append(_failed_step("rollback-metadata-verification", safe_error))
        return _rollback_failed_outcome(
            plan,
            failed_step,
            failure_summary,
            "failed",
            steps,
            restored_version=restored_version,
            rollback_failure=safe_error,
        )
    steps.append(
        {
            "name": "rollback-metadata-verification",
            "status": "success",
            "summary": f"Package metadata reports restored version {plan.old_version}.",
        }
    )
    rollback_steps: tuple[tuple[str, Callable[[], str], str], ...] = (
        (
            "rollback-pi-web-restart",
            lambda: _restart_service(adapters, plan.service_name),
            "Restarted pi-web.service after rollback.",
        ),
        (
            "rollback-pi-web-health",
            lambda: _check_health(adapters, plan.service_name),
            "Pi Web is healthy after rollback.",
        ),
        (
            "rollback-mcp-validation",
            lambda: _validate_mcp(adapters, plan.expected_mcp_result),
            f"Rollback validation returned exact {plan.expected_mcp_result}.",
        ),
    )
    for step_name, operation, success_summary in rollback_steps:
        try:
            result = operation()
        except Exception as error:
            safe_error = redactor.text(str(error))
            if step_name == "rollback-pi-web-restart":
                health_result = "restart-failed"
            elif step_name == "rollback-pi-web-health":
                health_result = "failed"
            elif step_name == "rollback-mcp-validation":
                validation_result = "failed"
            steps.append(_failed_step(step_name, safe_error))
            return _rollback_failed_outcome(
                plan,
                failed_step,
                failure_summary,
                "failed",
                steps,
                restored_version=restored_version,
                pi_web_health=health_result,
                mcp_validation=validation_result,
                rollback_failure=safe_error,
            )
        if step_name == "rollback-pi-web-health":
            health_result = result
        elif step_name == "rollback-mcp-validation":
            validation_result = result
        steps.append(
            {"name": step_name, "status": "success", "summary": success_summary}
        )
    rollback = {
        "attempted": True,
        "mcp_validation": validation_result,
        "pi_web_health": health_result,
        "required": True,
        "restored_version": plan.old_version,
        "status": "verified",
        "target_version": plan.old_version,
    }
    return _ExecutionOutcome(
        status="rolled-back",
        decision_status="rolled-back",
        summary=(
            f"Approved Pi MCP adapter update failed at {failed_step}: "
            f"{failure_summary}; rollback verified restored exact version "
            f"{plan.old_version}, healthy Pi Web, and {plan.expected_mcp_result}."
        ),
        details={
            **_execution_details(plan, mutation_performed=True, steps=steps),
            "failed_step": failed_step,
            "failure": failure_summary,
            "rollback": rollback,
        },
        notification_required=True,
    )


def _execution_ambiguous_outcome(
    plan: ApprovedPiMcpPlan,
    failed_step: str,
    failure_summary: str,
    steps: list[dict[str, object]],
    *,
    observed_version: str | None,
) -> _ExecutionOutcome:
    rollback = {
        "attempted": False,
        "failure": failure_summary,
        "mcp_validation": "not-checked",
        "pi_web_health": "not-checked",
        "required": "ambiguous",
        "restored_version": observed_version,
        "status": "ambiguous",
        "target_version": plan.old_version,
    }
    return _ExecutionOutcome(
        status="failed",
        decision_status="rollback-failed",
        summary=(
            f"CRITICAL: Pi MCP adapter execution is ambiguous at {failed_step}: "
            f"{failure_summary}"
        ),
        details={
            **_execution_details(plan, mutation_performed=None, steps=steps),
            "failed_step": failed_step,
            "failure": failure_summary,
            "outcome": "critical-execution-ambiguity",
            "rollback": rollback,
        },
        notification_required=True,
    )


def _rollback_failed_outcome(
    plan: ApprovedPiMcpPlan,
    failed_step: str,
    failure_summary: str,
    rollback_status: str,
    steps: list[dict[str, object]],
    *,
    restored_version: str | None = None,
    pi_web_health: str = "not-checked",
    mcp_validation: str = "not-checked",
    rollback_failure: str,
) -> _ExecutionOutcome:
    rollback = {
        "attempted": True,
        "failure": rollback_failure,
        "mcp_validation": mcp_validation,
        "pi_web_health": pi_web_health,
        "required": True,
        "restored_version": restored_version,
        "status": rollback_status,
        "target_version": plan.old_version,
    }
    return _ExecutionOutcome(
        status="failed",
        decision_status="rollback-failed",
        summary=(
            f"CRITICAL: Pi MCP adapter update failed at {failed_step}: "
            f"{failure_summary}; rollback is {rollback_status}: {rollback_failure}"
        ),
        details={
            **_execution_details(plan, mutation_performed=True, steps=steps),
            "failed_step": failed_step,
            "failure": failure_summary,
            "outcome": "critical-rollback-failure",
            "rollback": rollback,
        },
        notification_required=True,
    )


def _failed_update_outcome(
    plan: ApprovedPiMcpPlan,
    failure_summary: str,
    steps: list[dict[str, object]],
) -> _ExecutionOutcome:
    failed_step = str(steps[-1]["name"])
    return _ExecutionOutcome(
        status="failed",
        decision_status="failed",
        summary=f"Approved Pi MCP adapter update failed at {failed_step}: {failure_summary}",
        details={
            **_execution_details(plan, mutation_performed=False, steps=steps),
            "failed_step": failed_step,
            "failure": failure_summary,
            "rollback": {
                "attempted": False,
                "mcp_validation": "not-checked",
                "pi_web_health": "not-checked",
                "required": False,
                "restored_version": None,
                "status": "not-required",
                "target_version": plan.old_version,
            },
        },
    )


def _failed_step(name: str, summary: str) -> dict[str, object]:
    return {"name": name, "status": "failed", "summary": summary}


def _verify_installed_version(
    adapters: PiMcpExecutionAdapters,
    expected_version: str,
) -> str:
    installed = adapters.package.installed_version()
    _require_exact_version(installed, "installed adapter version")
    if installed != expected_version:
        raise PiMcpExecutionAdapterError(
            "installed package metadata does not match the expected exact version"
        )
    return installed


def _restart_service(
    adapters: PiMcpExecutionAdapters,
    service_name: str,
) -> str:
    adapters.service.restart(service_name)
    return "restarted"


def _check_health(
    adapters: PiMcpExecutionAdapters,
    service_name: str,
) -> str:
    health = adapters.health.check(service_name)
    if health != "healthy":
        raise PiMcpExecutionAdapterError("Pi Web health result is ambiguous")
    return health


def _validate_mcp(
    adapters: PiMcpExecutionAdapters,
    expected_result: str,
) -> str:
    validation = adapters.mcp_validation.validate_mcp(expected_result)
    if validation != expected_result:
        raise PiMcpExecutionAdapterError("MCP validation result is ambiguous")
    return validation


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
                       runs.status AS run_status,
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
                       tasks.removed_at,
                       recovery.phase AS recovery_phase,
                       recovery.updated_at AS recovery_updated_at
                FROM runs
                LEFT JOIN decisions
                  ON decisions.approval_run_id = runs.id
                JOIN tasks ON tasks.id = runs.task_id
                LEFT JOIN pi_mcp_execution_recovery AS recovery
                  ON recovery.decision_id = decisions.id
                WHERE runs.trigger = 'approval'
                  AND (
                      (
                          runs.status = 'claimed'
                          AND tasks.handler = 'pi_mcp_adapter'
                          AND tasks.action_mode = 'approved-procedure'
                      )
                      OR
                      (
                          runs.status = 'running'
                          AND recovery.decision_id IS NOT NULL
                      )
                  )
                ORDER BY runs.created_at, runs.id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            recovering = (
                str(row["run_status"]) == "running"
                and row["recovery_phase"] is not None
            )
            try:
                plan = _approved_plan_from_row(
                    row,
                    require_current_registration=not recovering,
                )
            except PiMcpExecutionError as error:
                if recovering:
                    connection.rollback()
                    raise PiMcpExecutionError(
                        "running approval recovery has an invalid immutable plan"
                    ) from error
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
            if str(row["run_status"]) == "claimed":
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
                connection.execute(
                    """
                    INSERT INTO pi_mcp_execution_recovery(
                        decision_id, approval_run_id, phase, updated_at
                    ) VALUES (?, ?, 'execution-started', ?)
                    """,
                    (plan.decision_id, plan.approval_run_id, started_at),
                )
            elif row["recovery_phase"] is None:
                raise PiMcpExecutionError(
                    "running approval Run has no recoverable execution state"
                )
            else:
                recovery_updated_at = str(row["recovery_updated_at"])
                cursor = connection.execute(
                    """
                    UPDATE pi_mcp_execution_recovery SET updated_at = ?
                    WHERE decision_id = ? AND updated_at = ?
                    """,
                    (started_at, plan.decision_id, recovery_updated_at),
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


def _approved_plan_from_row(
    row: sqlite3.Row,
    *,
    require_current_registration: bool,
) -> ApprovedPiMcpPlan:
    if (
        row["decision_id"] is None
        or str(row["decision_status"]) != "approved"
        or str(row["response_action"]) != "approve"
    ):
        raise PiMcpExecutionError(
            "approval Run is not backed by an approved Decision"
        )
    if require_current_registration and (
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
        UPDATE runs SET status = 'failed',
                        started_at = COALESCE(started_at, ?), finished_at = ?,
                        summary = ?, details_json = ?, external_log_ref = NULL
        WHERE id = ? AND status IN ('claimed', 'running')
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
    connection.execute(
        "DELETE FROM pi_mcp_execution_recovery WHERE approval_run_id = ?",
        (run_id,),
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


def _get_recovery_state(path: Path, decision_id: str) -> _RecoveryState:
    try:
        with _execution_connection(path) as connection:
            row = connection.execute(
                """
                SELECT phase, failed_step, failure_summary,
                       pending_outcome_json
                FROM pi_mcp_execution_recovery
                WHERE decision_id = ?
                """,
                (decision_id,),
            ).fetchone()
    except sqlite3.Error as error:
        raise PiMcpExecutionError(
            "approved execution recovery state could not be inspected"
        ) from error
    if row is None:
        raise PiMcpExecutionError("approved execution recovery state is missing")
    return _RecoveryState(
        phase=str(row["phase"]),
        failed_step=(
            None if row["failed_step"] is None else str(row["failed_step"])
        ),
        failure_summary=(
            None
            if row["failure_summary"] is None
            else str(row["failure_summary"])
        ),
        pending_outcome_json=(
            None
            if row["pending_outcome_json"] is None
            else str(row["pending_outcome_json"])
        ),
    )


def _set_recovery_phase(
    path: Path,
    decision_id: str,
    phase: str,
    updated_at: str,
    *,
    failed_step: str | None = None,
    failure_summary: str | None = None,
) -> None:
    try:
        with _execution_connection(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE pi_mcp_execution_recovery SET
                    phase = ?, failed_step = ?, failure_summary = ?, updated_at = ?
                WHERE decision_id = ?
                """,
                (
                    phase,
                    failed_step,
                    failure_summary,
                    updated_at,
                    decision_id,
                ),
            )
            if cursor.rowcount != 1:
                raise PiMcpExecutionError(
                    "approved execution recovery state lost a concurrent transition"
                )
            connection.commit()
    except PiMcpExecutionError:
        raise
    except sqlite3.Error as error:
        raise PiMcpExecutionError(
            "approved execution recovery state could not be recorded"
        ) from error


def _checkpoint_execution_outcome(
    path: Path,
    decision_id: str,
    outcome: _ExecutionOutcome,
    updated_at: str,
) -> None:
    payload = _canonical_json(
        {
            "decision_status": outcome.decision_status,
            "details": outcome.details,
            "fresh_check_required": outcome.fresh_check_required,
            "notification_required": outcome.notification_required,
            "status": outcome.status,
            "summary": outcome.summary,
        }
    )
    try:
        with _execution_connection(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE pi_mcp_execution_recovery SET
                    pending_outcome_json = ?, updated_at = ?
                WHERE decision_id = ? AND pending_outcome_json IS NULL
                """,
                (payload, updated_at, decision_id),
            )
            if cursor.rowcount != 1:
                raise PiMcpExecutionError(
                    "approved execution outcome checkpoint lost a concurrent transition"
                )
            connection.commit()
    except PiMcpExecutionError:
        raise
    except sqlite3.Error as error:
        raise PiMcpExecutionError(
            "approved execution outcome could not be checkpointed"
        ) from error


def _stored_pending_outcome(value: str) -> _ExecutionOutcome:
    try:
        payload: object = json.loads(value)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise PiMcpExecutionError(
            "stored pending execution outcome is invalid"
        ) from error
    if not isinstance(payload, dict):
        raise PiMcpExecutionError("stored pending execution outcome is invalid")
    stored = cast(dict[str, object], payload)
    details = stored.get("details")
    fields = {
        name: stored.get(name)
        for name in ("decision_status", "status", "summary")
    }
    if not isinstance(details, dict) or not all(
        isinstance(field, str) and field
        for field in fields.values()
    ):
        raise PiMcpExecutionError("stored pending execution outcome is invalid")
    notification_required = stored.get("notification_required")
    fresh_check_required = stored.get("fresh_check_required")
    if not isinstance(notification_required, bool) or not isinstance(
        fresh_check_required,
        bool,
    ):
        raise PiMcpExecutionError("stored pending execution outcome is invalid")
    return _ExecutionOutcome(
        status=cast(str, fields["status"]),
        decision_status=cast(str, fields["decision_status"]),
        summary=cast(str, fields["summary"]),
        details=cast(dict[str, object], details),
        notification_required=notification_required,
        fresh_check_required=fresh_check_required,
    )


def _complete_execution(
    path: Path,
    plan: ApprovedPiMcpPlan,
    outcome: _ExecutionOutcome,
    finished_at: str,
    *,
    fresh_check_at: str | None,
) -> None:
    safe_details = _canonical_json(outcome.details)
    decision_status = outcome.decision_status
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
                        if outcome.notification_required
                        else "not-required"
                    ),
                ),
            )
            if outcome.fresh_check_required:
                connection.execute(
                    """
                    UPDATE tasks SET next_run_at = ?, updated_at = ?
                    WHERE id = ? AND removed_at IS NULL
                    """,
                    (
                        fresh_check_at or finished_at,
                        finished_at,
                        plan.task_id,
                    ),
                )
            connection.execute(
                "DELETE FROM pi_mcp_execution_recovery WHERE decision_id = ?",
                (plan.decision_id,),
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
    mutation_performed: bool | None,
    steps: list[dict[str, object]],
) -> dict[str, object]:
    mutation_status = (
        "unknown"
        if mutation_performed is None
        else "performed" if mutation_performed else "none"
    )
    return {
        "decision_id": plan.decision_id,
        "handler": "pi_mcp_adapter",
        "mutation_performed": mutation_performed,
        "mutation_status": mutation_status,
        "new_version": plan.target_version,
        "old_version": plan.old_version,
        "plan_hash": plan.plan_hash,
        "steps": steps,
    }


def _deliver_pending_execution_notifications(
    path: Path,
    notification_adapter: ExecutionNotificationAdapter,
    redactor: Redactor,
    timestamp_factory: Callable[[], str],
    *,
    decision_id: str | None = None,
) -> None:
    attempted_decision_ids: set[str] = set()
    while True:
        attempted_at = timestamp_factory()
        pending = _claim_execution_notification(
            path,
            claimed_at=attempted_at,
            decision_id=decision_id,
            excluded_decision_ids=attempted_decision_ids,
        )
        if pending is None:
            return
        attempted_decision_ids.add(pending.decision_id)
        try:
            notification_adapter.send(
                redactor.text(_execution_notification(pending))
            )
        except Exception as error:
            _record_execution_notification(
                path,
                pending,
                delivered=False,
                attempted_at=attempted_at,
                error=redactor.text(str(error)),
            )
            if decision_id is not None:
                return
            continue
        _record_execution_notification(
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
            "previous outcome notification delivery was interrupted",
            cutoff_text,
        ),
    )


def _claim_execution_notification(
    path: Path,
    *,
    claimed_at: str,
    decision_id: str | None,
    excluded_decision_ids: set[str],
) -> _PendingExecutionNotification | None:
    conditions: list[str] = []
    parameters: list[object] = []
    if decision_id is not None:
        conditions.append("execution.decision_id = ?")
        parameters.append(decision_id)
    if excluded_decision_ids:
        placeholders = ", ".join("?" for _ in excluded_decision_ids)
        conditions.append(f"execution.decision_id NOT IN ({placeholders})")
        parameters.extend(sorted(excluded_decision_ids))
    additional_conditions = (
        "" if not conditions else "AND " + " AND ".join(conditions)
    )
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
                       execution.status AS outcome_status,
                       execution.details_json, tasks.name AS task_name
                FROM decision_execution_outcomes AS execution
                JOIN decisions ON decisions.id = execution.decision_id
                JOIN tasks ON tasks.id = decisions.task_id
                WHERE execution.status IN (
                    'completed', 'rolled-back', 'rollback-failed'
                )
                  AND execution.notification_status IN (
                      'pending', 'retryable-failure'
                  )
                  {additional_conditions}
                ORDER BY CASE execution.status
                             WHEN 'rollback-failed' THEN 0
                             WHEN 'rolled-back' THEN 1
                             ELSE 2
                         END,
                         execution.completed_at,
                         execution.decision_id
                LIMIT 1
                """,
                tuple(parameters),
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
            rollback_value = details.get("rollback")
            rollback = (
                cast(dict[str, object], rollback_value)
                if isinstance(rollback_value, dict)
                else None
            )
            expected_result_value = details.get("mcp_validation")
            if expected_result_value is None and rollback is not None:
                expected_result_value = rollback.get("mcp_validation")
            failed_step_value = details.get("failed_step")
            pending = _PendingExecutionNotification(
                decision_id=str(row["decision_id"]),
                approval_run_id=str(row["approval_run_id"]),
                task_name=str(row["task_name"]),
                outcome_status=str(row["outcome_status"]),
                old_version=_stored_detail_text(details, "old_version"),
                target_version=_stored_detail_text(details, "new_version"),
                expected_mcp_result=(
                    expected_result_value
                    if isinstance(expected_result_value, str)
                    else None
                ),
                failed_step=(
                    failed_step_value
                    if isinstance(failed_step_value, str)
                    else None
                ),
                rollback=rollback,
            )
            connection.commit()
            return pending
    except PiMcpExecutionError:
        raise
    except sqlite3.Error as error:
        raise PiMcpExecutionError(
            "outcome notification could not be claimed"
        ) from error


def _record_execution_notification(
    path: Path,
    pending: _PendingExecutionNotification,
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
                    "outcome notification claim is no longer current"
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
                    "name": "outcome-notification",
                    "status": "success" if delivered else "failed",
                    "summary": (
                        "Sent the redacted update outcome notification."
                        if delivered
                        else f"Update outcome notification will be retried: {error}"
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
                WHERE id = ? AND status IN ('success', 'rolled-back', 'failed')
                """,
                (details_json, pending.approval_run_id),
            )
            connection.commit()
    except PiMcpExecutionError:
        raise
    except sqlite3.Error as sql_error:
        raise PiMcpExecutionError(
            "outcome notification result could not be recorded"
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


def _execution_notification(pending: _PendingExecutionNotification) -> str:
    if pending.outcome_status == "completed":
        return f"""RunTasks update completed successfully

Task: {pending.task_name}
Updated: {pending.old_version} → {pending.target_version}
Pi Web: Healthy
Validation: {pending.expected_mcp_result}
Rollback: Not required

Open a fresh terminal Pi session and reopen existing terminal Pi sessions so they load the new adapter version."""
    rollback = pending.rollback or {}
    restored_version = rollback.get("restored_version")
    health = rollback.get("pi_web_health", "unknown")
    validation = rollback.get("mcp_validation", "unknown")
    if pending.outcome_status == "rolled-back":
        return f"""URGENT — RunTasks update failed; rollback verified

Task: {pending.task_name}
Attempted: {pending.old_version} → {pending.target_version}
Failed step: {pending.failed_step}
Rollback: Restored exact {restored_version}
Pi Web: {str(health).title()}
Validation: {validation}

The approved update failed, but the exact prior pin and service health were verified."""
    rollback_status = rollback.get("status", "ambiguous")
    rollback_failure = rollback.get("failure", "recovery state is unresolved")
    return f"""URGENT — CRITICAL RunTasks rollback failure

Task: {pending.task_name}
Attempted: {pending.old_version} → {pending.target_version}
Failed step: {pending.failed_step}
Rollback: {str(rollback_status).upper()}
Observed/restored version: {restored_version or 'unknown'}
Pi Web: {health}
Validation: {validation}
Recovery error: {rollback_failure}

Immediate operator investigation is required. RunTasks will not repeat the package installation automatically."""


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
def pi_mcp_execution_guard(lock_path: Path) -> Iterator[bool]:
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, "r+b", buffering=0) as stream:
            try:
                _acquire_execution_lock(stream)
            except OSError as error:
                raise PiMcpExecutionError(
                    "approved execution lock could not be acquired"
                ) from error
            try:
                yield True
            finally:
                try:
                    _release_execution_lock(stream)
                except OSError as error:
                    raise PiMcpExecutionError(
                        "approved execution lock could not be released"
                    ) from error
    except OSError as error:
        raise PiMcpExecutionError(
            "approved execution lock could not be acquired"
        ) from error


def _acquire_execution_lock(stream: BinaryIO) -> None:
    if os.name == "nt":
        stream.seek(0)
        if stream.read(1) == b"":
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        _LOCK_MODULE.locking(
            stream.fileno(),
            _LOCK_MODULE.LK_LOCK,
            1,
        )
    else:
        _LOCK_MODULE.flock(
            stream.fileno(),
            _LOCK_MODULE.LOCK_EX,
        )


def _release_execution_lock(stream: BinaryIO) -> None:
    if os.name == "nt":
        stream.seek(0)
        _LOCK_MODULE.locking(
            stream.fileno(),
            _LOCK_MODULE.LK_UNLCK,
            1,
        )
    else:
        _LOCK_MODULE.flock(stream.fileno(), _LOCK_MODULE.LOCK_UN)


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
