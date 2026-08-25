from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import importlib
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, BinaryIO, Callable, Iterator
from urllib.parse import quote


LATEST_SCHEMA_VERSION = 9
BUSY_TIMEOUT_MS = 5_000
_FTS_TABLES_BY_VERSION = (
    (2, "task_fts"),
    (3, "run_fts"),
    (5, "decision_fts"),
    (9, "decision_execution_fts"),
)
_FTS_TABLES = tuple(table for _, table in _FTS_TABLES_BY_VERSION)
_FTS_SHADOW_TABLES = {
    f"{table}_{suffix}"
    for table in _FTS_TABLES
    for suffix in ("config", "content", "data", "docsize", "idx")
}
_LOCK_MODULE: Any = (
    importlib.import_module("msvcrt")
    if os.name == "nt"
    else importlib.import_module("fcntl")
)
_CTYPES: Any = ctypes
_WINDOWS_KERNEL32: Any = (
    _CTYPES.WinDLL("kernel32", use_last_error=True)
    if os.name == "nt"
    else None
)


class _WindowsOverlapped(ctypes.Structure):
    _fields_ = (
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", ctypes.c_uint32),
        ("OffsetHigh", ctypes.c_uint32),
        ("hEvent", ctypes.c_void_p),
    )


if os.name == "nt":
    _WINDOWS_KERNEL32.LockFileEx.argtypes = (
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(_WindowsOverlapped),
    )
    _WINDOWS_KERNEL32.LockFileEx.restype = ctypes.c_int
    _WINDOWS_KERNEL32.UnlockFileEx.argtypes = (
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(_WindowsOverlapped),
    )
    _WINDOWS_KERNEL32.UnlockFileEx.restype = ctypes.c_int


class DatabaseError(RuntimeError):
    """Raised when the runtime database cannot be initialized or inspected."""


@dataclass(frozen=True)
class DatabaseHealth:
    path: str
    exists: bool
    schema_version: int
    foreign_keys: bool
    busy_timeout_ms: int
    journal_mode: str
    fts5: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def initialize_database(
    path: Path,
    *,
    before_existing_change: Callable[[], None],
) -> bool:
    if path.exists():
        with database_access_guard(path, exclusive=True):
            return _initialize_database(
                path,
                before_existing_change=before_existing_change,
                protect_existing=True,
                guard_database_access=False,
            )
    return _initialize_database(
        path,
        before_existing_change=before_existing_change,
        protect_existing=True,
        guard_database_access=True,
    )


def initialize_staged_database(path: Path) -> bool:
    return _initialize_database(
        path,
        before_existing_change=None,
        protect_existing=False,
        guard_database_access=False,
    )


def _initialize_database(
    path: Path,
    *,
    before_existing_change: Callable[[], None] | None,
    protect_existing: bool,
    guard_database_access: bool,
) -> bool:
    existed = path.exists()
    try:
        requires_change = existed and _existing_database_requires_change(
            path,
            guard=guard_database_access,
        )
        if requires_change and protect_existing:
            if before_existing_change is None:
                raise DatabaseError("existing database backup is required")
            before_existing_change()
        with database_connection(
            path,
            enable_wal=True,
            guard=guard_database_access,
        ) as connection:
            changed = _apply_migrations(connection)
            _read_health(connection, path)
        if not existed:
            path.chmod(0o600)
    except (OSError, sqlite3.Error, ValueError) as error:
        raise DatabaseError("database initialization failed") from error
    return changed or not existed


def _existing_database_requires_change(path: Path, *, guard: bool) -> bool:
    with read_only_database_connection(path, guard=guard) as connection:
        schema_version = read_schema_version(connection)
        if schema_version > LATEST_SCHEMA_VERSION:
            raise DatabaseError("database schema is newer than this RunTasks version")
        journal_mode = str(
            connection.execute("PRAGMA journal_mode").fetchone()[0]
        ).lower()
        return schema_version < LATEST_SCHEMA_VERSION or journal_mode != "wal"


def inspect_database(path: Path) -> DatabaseHealth:
    if not path.is_file():
        raise DatabaseError("database does not exist")
    try:
        with database_connection(path, enable_wal=False) as connection:
            return _read_health(connection, path)
    except (sqlite3.Error, ValueError) as error:
        raise DatabaseError("database health check failed") from error


@contextmanager
def database_access_guard(
    path: Path,
    *,
    exclusive: bool,
) -> Iterator[None]:
    lock_path = Path(f"{path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    lock_file = os.fdopen(descriptor, "r+b", buffering=0)
    acquired = False
    try:
        _acquire_database_lock(lock_file, exclusive=exclusive)
        acquired = True
        yield
    finally:
        try:
            if acquired:
                _release_database_lock(lock_file)
        finally:
            lock_file.close()


@contextmanager
def read_only_database_connection(
    path: Path,
    *,
    guard: bool = True,
) -> Iterator[sqlite3.Connection]:
    if guard:
        with database_access_guard(path, exclusive=False):
            with read_only_database_connection(path, guard=False) as connection:
                yield connection
        return
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    connection = sqlite3.connect(
        uri,
        uri=True,
        timeout=BUSY_TIMEOUT_MS / 1000,
    )
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        yield connection
    finally:
        connection.close()


@contextmanager
def database_connection(
    path: Path,
    *,
    enable_wal: bool,
    guard: bool = True,
) -> Iterator[sqlite3.Connection]:
    if guard:
        with database_access_guard(path, exclusive=False):
            with database_connection(
                path,
                enable_wal=enable_wal,
                guard=False,
            ) as connection:
                yield connection
        return
    connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        if enable_wal:
            connection.execute("PRAGMA journal_mode = WAL").fetchone()
        yield connection
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _acquire_database_lock(lock_file: BinaryIO, *, exclusive: bool) -> None:
    deadline = time.monotonic() + (BUSY_TIMEOUT_MS / 1000)
    while True:
        try:
            if os.name == "nt":
                _acquire_windows_database_lock(
                    lock_file,
                    exclusive=exclusive,
                )
            else:
                operation = (
                    _LOCK_MODULE.LOCK_EX if exclusive else _LOCK_MODULE.LOCK_SH
                )
                _LOCK_MODULE.flock(
                    lock_file.fileno(),
                    operation | _LOCK_MODULE.LOCK_NB,
                )
            return
        except OSError as error:
            if time.monotonic() >= deadline:
                raise DatabaseError(
                    "database is busy; stop RunTasks before restoring"
                ) from error
            time.sleep(0.05)


def _release_database_lock(lock_file: BinaryIO) -> None:
    if os.name == "nt":
        _release_windows_database_lock(lock_file)
    else:
        _LOCK_MODULE.flock(lock_file.fileno(), _LOCK_MODULE.LOCK_UN)


def _acquire_windows_database_lock(
    lock_file: BinaryIO,
    *,
    exclusive: bool,
) -> None:
    handle = _LOCK_MODULE.get_osfhandle(lock_file.fileno())
    flags = 0x00000001
    if exclusive:
        flags |= 0x00000002
    overlapped = _WindowsOverlapped()
    if not _WINDOWS_KERNEL32.LockFileEx(
        handle,
        flags,
        0,
        1,
        0,
        ctypes.byref(overlapped),
    ):
        error_code = _CTYPES.get_last_error()
        raise OSError(error_code, "database lock is unavailable")


def _release_windows_database_lock(lock_file: BinaryIO) -> None:
    handle = _LOCK_MODULE.get_osfhandle(lock_file.fileno())
    overlapped = _WindowsOverlapped()
    if not _WINDOWS_KERNEL32.UnlockFileEx(
        handle,
        0,
        1,
        0,
        ctypes.byref(overlapped),
    ):
        error_code = _CTYPES.get_last_error()
        raise OSError(error_code, "database lock could not be released")


def _apply_migrations(
    connection: sqlite3.Connection,
    *,
    target_version: int = LATEST_SCHEMA_VERSION,
) -> bool:
    if target_version < 0 or target_version > LATEST_SCHEMA_VERSION:
        raise DatabaseError("database schema version is not supported")
    try:
        connection.execute("BEGIN IMMEDIATE")
        current_version = read_schema_version(connection)
        if current_version > target_version:
            raise DatabaseError("database schema is newer than the requested version")
        if current_version == target_version:
            connection.commit()
            return False
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )

        if current_version < 1 <= target_version:
            _record_migration(connection, 1)
        if current_version < 2 <= target_version:
            _create_task_registry(connection)
            _record_migration(connection, 2)
        if current_version < 3 <= target_version:
            _create_run_history(connection)
            _record_migration(connection, 3)
        if current_version < 4 <= target_version:
            _add_scheduled_run_claims(connection)
            _record_migration(connection, 4)
        if current_version < 5 <= target_version:
            _create_decisions(connection)
            _record_migration(connection, 5)
        if current_version < 6 <= target_version:
            _create_telegram_decision_messages(connection)
            _record_migration(connection, 6)
        if current_version < 7 <= target_version:
            _create_decision_notification_deliveries(connection)
            _record_migration(connection, 7)
        if current_version < 8 <= target_version:
            _create_decision_execution_outcomes(connection)
            _record_migration(connection, 8)
        if current_version < 9 <= target_version:
            _migrate_pi_mcp_recovery_and_execution_history(connection)
            _record_migration(connection, 9)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return True


def _record_migration(connection: sqlite3.Connection, version: int) -> None:
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
        (version, datetime.now(timezone.utc).isoformat()),
    )


def _create_task_registry(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            identity_key TEXT NOT NULL UNIQUE,
            source_identity_key TEXT UNIQUE,
            policy_key TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_ref TEXT,
            source_summary TEXT NOT NULL,
            schedule_type TEXT NOT NULL,
            schedule_json TEXT NOT NULL,
            timezone TEXT NOT NULL,
            next_run_at TEXT NOT NULL,
            action_mode TEXT NOT NULL,
            handler TEXT NOT NULL,
            policy_json TEXT NOT NULL,
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            removed_at TEXT
        )
        """
    )
    connection.execute(
        "CREATE INDEX tasks_due_idx ON tasks(enabled, next_run_at)"
    )
    connection.execute(
        """
        CREATE VIRTUAL TABLE task_fts USING fts5(
            task_id UNINDEXED,
            name,
            description,
            source_summary,
            policy
        )
        """
    )
    connection.execute(
        """
        CREATE TRIGGER tasks_fts_insert AFTER INSERT ON tasks BEGIN
            INSERT INTO task_fts(task_id, name, description, source_summary, policy)
            SELECT new.id, new.name, new.description, new.source_summary, new.policy_json
            WHERE new.removed_at IS NULL;
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER tasks_fts_update AFTER UPDATE ON tasks BEGIN
            DELETE FROM task_fts WHERE task_id = old.id;
            INSERT INTO task_fts(task_id, name, description, source_summary, policy)
            SELECT new.id, new.name, new.description, new.source_summary, new.policy_json
            WHERE new.removed_at IS NULL;
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER tasks_fts_delete AFTER DELETE ON tasks BEGIN
            DELETE FROM task_fts WHERE task_id = old.id;
        END
        """
    )


def _create_run_history(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            task_name TEXT NOT NULL,
            trigger TEXT NOT NULL CHECK (
                trigger IN ('manual', 'scheduled', 'approval')
            ),
            status TEXT NOT NULL CHECK (
                status IN (
                    'claimed', 'running', 'success', 'no-change',
                    'non-important', 'decision-required', 'failed',
                    'rolled-back', 'manual-action-due'
                )
            ),
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            summary TEXT NOT NULL,
            details_json TEXT NOT NULL,
            external_log_ref TEXT
        )
        """
    )
    connection.execute(
        "CREATE INDEX runs_task_history_idx ON runs(task_id, created_at DESC)"
    )
    connection.execute(
        "CREATE INDEX runs_status_idx ON runs(status, created_at DESC)"
    )
    connection.execute(
        """
        CREATE VIRTUAL TABLE run_fts USING fts5(
            run_id UNINDEXED,
            task_name,
            summary,
            details
        )
        """
    )
    connection.execute(
        """
        CREATE TRIGGER runs_fts_insert AFTER INSERT ON runs BEGIN
            INSERT INTO run_fts(run_id, task_name, summary, details)
            VALUES (new.id, new.task_name, new.summary, new.details_json);
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER runs_fts_update AFTER UPDATE ON runs BEGIN
            DELETE FROM run_fts WHERE run_id = old.id;
            INSERT INTO run_fts(run_id, task_name, summary, details)
            VALUES (new.id, new.task_name, new.summary, new.details_json);
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER runs_fts_delete AFTER DELETE ON runs BEGIN
            DELETE FROM run_fts WHERE run_id = old.id;
        END
        """
    )


def _add_scheduled_run_claims(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE runs ADD COLUMN scheduled_for TEXT")
    connection.execute("ALTER TABLE runs ADD COLUMN next_run_at TEXT")
    connection.execute(
        """
        CREATE UNIQUE INDEX runs_scheduled_occurrence_idx
        ON runs(task_id, scheduled_for)
        WHERE trigger = 'scheduled'
        """
    )


def _create_decisions(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE decisions (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'approved', 'rejected')
            ),
            plan_json TEXT NOT NULL,
            plan_hash TEXT NOT NULL,
            reason TEXT NOT NULL,
            validation_summary TEXT NOT NULL,
            rollback_summary TEXT NOT NULL,
            response_action TEXT CHECK (
                response_action IS NULL OR response_action IN ('approve', 'reject')
            ),
            response_channel TEXT,
            responded_by TEXT,
            responded_at TEXT,
            approval_run_id TEXT UNIQUE REFERENCES runs(id),
            execution_scheduled_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (
                (
                    status = 'pending'
                    AND response_action IS NULL
                    AND response_channel IS NULL
                    AND responded_by IS NULL
                    AND responded_at IS NULL
                    AND approval_run_id IS NULL
                    AND execution_scheduled_at IS NULL
                )
                OR (
                    status = 'rejected'
                    AND response_action = 'reject'
                    AND response_channel IS NOT NULL
                    AND responded_by IS NOT NULL
                    AND responded_at IS NOT NULL
                    AND approval_run_id IS NULL
                    AND execution_scheduled_at IS NULL
                )
                OR (
                    status = 'approved'
                    AND response_action = 'approve'
                    AND response_channel IS NOT NULL
                    AND responded_by IS NOT NULL
                    AND responded_at IS NOT NULL
                    AND approval_run_id IS NOT NULL
                    AND execution_scheduled_at IS NOT NULL
                )
            )
        )
        """
    )
    connection.execute(
        "CREATE INDEX decisions_status_idx ON decisions(status, created_at DESC)"
    )
    connection.execute(
        "CREATE INDEX decisions_task_idx ON decisions(task_id, created_at DESC)"
    )
    connection.execute(
        """
        CREATE TRIGGER decisions_immutable_plan
        BEFORE UPDATE OF task_id, run_id, plan_json, plan_hash, reason,
                         validation_summary, rollback_summary ON decisions
        WHEN old.task_id != new.task_id
          OR old.run_id != new.run_id
          OR old.plan_json != new.plan_json
          OR old.plan_hash != new.plan_hash
          OR old.reason != new.reason
          OR old.validation_summary != new.validation_summary
          OR old.rollback_summary != new.rollback_summary
        BEGIN
            SELECT RAISE(ABORT, 'Decision plan and evidence are immutable');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER decisions_resolved_immutable
        BEFORE UPDATE ON decisions
        WHEN old.status != 'pending'
        BEGIN
            SELECT RAISE(ABORT, 'resolved Decision audit records are immutable');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER decisions_no_delete
        BEFORE DELETE ON decisions
        BEGIN
            SELECT RAISE(ABORT, 'Decision audit records cannot be deleted');
        END
        """
    )
    connection.execute(
        """
        CREATE VIRTUAL TABLE decision_fts USING fts5(
            decision_id UNINDEXED,
            reason,
            validation_summary,
            rollback_summary
        )
        """
    )
    connection.execute(
        """
        CREATE TRIGGER decisions_fts_insert AFTER INSERT ON decisions BEGIN
            INSERT INTO decision_fts(
                decision_id, reason, validation_summary, rollback_summary
            ) VALUES (
                new.id, new.reason, new.validation_summary, new.rollback_summary
            );
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER decisions_fts_update AFTER UPDATE ON decisions BEGIN
            DELETE FROM decision_fts WHERE decision_id = old.id;
            INSERT INTO decision_fts(
                decision_id, reason, validation_summary, rollback_summary
            ) VALUES (
                new.id, new.reason, new.validation_summary, new.rollback_summary
            );
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER decisions_fts_delete AFTER DELETE ON decisions BEGIN
            DELETE FROM decision_fts WHERE decision_id = old.id;
        END
        """
    )


def _create_telegram_decision_messages(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE approval_run_trigger_requests (
            approval_run_id TEXT PRIMARY KEY REFERENCES runs(id),
            decision_id TEXT NOT NULL UNIQUE REFERENCES decisions(id),
            created_at TEXT NOT NULL,
            requested_at TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO approval_run_trigger_requests(
            approval_run_id, decision_id, created_at, requested_at
        )
        SELECT approval_run_id, id, execution_scheduled_at, NULL
        FROM decisions
        WHERE status = 'approved' AND approval_run_id IS NOT NULL
        """
    )
    connection.execute(
        """
        CREATE INDEX approval_run_trigger_pending_idx
        ON approval_run_trigger_requests(created_at, approval_run_id)
        WHERE requested_at IS NULL
        """
    )
    connection.execute(
        """
        CREATE TABLE telegram_decision_messages (
            decision_id TEXT NOT NULL REFERENCES decisions(id),
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            message_kind TEXT NOT NULL CHECK (
                message_kind IN ('decision', 'details')
            ),
            sent_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, message_id)
        )
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX telegram_decision_initial_message_idx
        ON telegram_decision_messages(decision_id)
        WHERE message_kind = 'decision'
        """
    )
    connection.execute(
        """
        CREATE INDEX telegram_decision_message_lookup_idx
        ON telegram_decision_messages(decision_id, chat_id, message_id)
        """
    )


def _create_decision_notification_deliveries(
    connection: sqlite3.Connection,
) -> None:
    connection.execute(
        """
        CREATE TABLE decision_notification_deliveries (
            decision_id TEXT PRIMARY KEY REFERENCES decisions(id),
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'retryable-failure', 'delivered')
            ),
            attempts INTEGER NOT NULL CHECK (attempts >= 0),
            last_attempt_at TEXT,
            last_error TEXT,
            delivered_at TEXT,
            CHECK (
                (status = 'pending' AND attempts = 0
                 AND last_attempt_at IS NULL AND last_error IS NULL
                 AND delivered_at IS NULL)
                OR (status = 'retryable-failure' AND attempts > 0
                    AND last_attempt_at IS NOT NULL
                    AND last_error IS NOT NULL AND delivered_at IS NULL)
                OR (status = 'delivered' AND attempts > 0
                    AND last_attempt_at IS NOT NULL
                    AND last_error IS NULL AND delivered_at IS NOT NULL)
            )
        )
        """
    )
    connection.execute(
        """
        INSERT INTO decision_notification_deliveries(
            decision_id, status, attempts
        )
        SELECT decisions.id, 'pending', 0
        FROM decisions
        LEFT JOIN telegram_decision_messages
          ON telegram_decision_messages.decision_id = decisions.id
         AND telegram_decision_messages.message_kind = 'decision'
        WHERE telegram_decision_messages.decision_id IS NULL
        """
    )
    connection.execute(
        """
        INSERT INTO decision_notification_deliveries(
            decision_id, status, attempts, last_attempt_at, delivered_at
        )
        SELECT decisions.id, 'delivered', 1,
               telegram_decision_messages.sent_at,
               telegram_decision_messages.sent_at
        FROM decisions
        JOIN telegram_decision_messages
          ON telegram_decision_messages.decision_id = decisions.id
         AND telegram_decision_messages.message_kind = 'decision'
        """
    )


def _create_decision_execution_outcomes(
    connection: sqlite3.Connection,
) -> None:
    connection.execute(
        """
        CREATE TABLE decision_execution_outcomes (
            decision_id TEXT PRIMARY KEY REFERENCES decisions(id),
            approval_run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
            status TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
            summary TEXT NOT NULL,
            details_json TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            notification_status TEXT NOT NULL CHECK (
                notification_status IN (
                    'not-required', 'pending', 'sending',
                    'retryable-failure', 'delivered'
                )
            ),
            notification_attempts INTEGER NOT NULL CHECK (
                notification_attempts >= 0
            ),
            notification_claimed_at TEXT,
            notification_last_attempt_at TEXT,
            notification_last_error TEXT,
            notification_delivered_at TEXT,
            CHECK (
                (status = 'failed'
                 AND notification_status = 'not-required'
                 AND notification_attempts = 0
                 AND notification_claimed_at IS NULL
                 AND notification_last_attempt_at IS NULL
                 AND notification_last_error IS NULL
                 AND notification_delivered_at IS NULL)
                OR
                (status = 'completed'
                 AND notification_status = 'pending'
                 AND notification_attempts = 0
                 AND notification_claimed_at IS NULL
                 AND notification_last_attempt_at IS NULL
                 AND notification_last_error IS NULL
                 AND notification_delivered_at IS NULL)
                OR
                (status = 'completed'
                 AND notification_status = 'sending'
                 AND notification_claimed_at IS NOT NULL
                 AND notification_delivered_at IS NULL)
                OR
                (status = 'completed'
                 AND notification_status = 'retryable-failure'
                 AND notification_attempts > 0
                 AND notification_claimed_at IS NULL
                 AND notification_last_attempt_at IS NOT NULL
                 AND notification_last_error IS NOT NULL
                 AND notification_delivered_at IS NULL)
                OR
                (status = 'completed'
                 AND notification_status = 'delivered'
                 AND notification_attempts > 0
                 AND notification_claimed_at IS NULL
                 AND notification_last_attempt_at IS NOT NULL
                 AND notification_last_error IS NULL
                 AND notification_delivered_at IS NOT NULL)
            )
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX decision_execution_status_idx
        ON decision_execution_outcomes(status, completed_at DESC)
        """
    )


def _migrate_pi_mcp_recovery_and_execution_history(
    connection: sqlite3.Connection,
) -> None:
    running = connection.execute(
        """
        SELECT 1
        FROM runs
        JOIN tasks ON tasks.id = runs.task_id
        WHERE runs.trigger = 'approval'
          AND runs.status = 'running'
          AND tasks.handler = 'pi_mcp_adapter'
        LIMIT 1
        """
    ).fetchone()
    if running is not None:
        raise DatabaseError(
            "cannot migrate while a legacy Pi MCP approval Run is still running"
        )
    connection.execute(
        """
        CREATE TABLE pi_mcp_execution_recovery (
            decision_id TEXT PRIMARY KEY REFERENCES decisions(id),
            approval_run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
            phase TEXT NOT NULL CHECK (
                phase IN (
                    'execution-started', 'target-install-started',
                    'target-installed', 'rollback-required',
                    'rollback-install-started', 'rollback-installed'
                )
            ),
            failed_step TEXT,
            failure_summary TEXT,
            pending_outcome_json TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "ALTER TABLE decision_execution_outcomes RENAME TO decision_execution_outcomes_v8"
    )
    connection.execute(
        """
        CREATE TABLE decision_execution_outcomes (
            decision_id TEXT PRIMARY KEY REFERENCES decisions(id),
            approval_run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
            status TEXT NOT NULL CHECK (
                status IN (
                    'completed', 'failed', 'superseded',
                    'rolled-back', 'rollback-failed'
                )
            ),
            summary TEXT NOT NULL,
            details_json TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            notification_status TEXT NOT NULL CHECK (
                notification_status IN (
                    'not-required', 'pending', 'sending',
                    'retryable-failure', 'delivered'
                )
            ),
            notification_attempts INTEGER NOT NULL CHECK (
                notification_attempts >= 0
            ),
            notification_claimed_at TEXT,
            notification_last_attempt_at TEXT,
            notification_last_error TEXT,
            notification_delivered_at TEXT,
            CHECK (
                (notification_status = 'not-required'
                 AND notification_attempts = 0
                 AND notification_claimed_at IS NULL
                 AND notification_last_attempt_at IS NULL
                 AND notification_last_error IS NULL
                 AND notification_delivered_at IS NULL)
                OR
                (notification_status = 'pending'
                 AND notification_attempts = 0
                 AND notification_claimed_at IS NULL
                 AND notification_last_attempt_at IS NULL
                 AND notification_last_error IS NULL
                 AND notification_delivered_at IS NULL)
                OR
                (notification_status = 'sending'
                 AND notification_claimed_at IS NOT NULL
                 AND notification_delivered_at IS NULL)
                OR
                (notification_status = 'retryable-failure'
                 AND notification_attempts > 0
                 AND notification_claimed_at IS NULL
                 AND notification_last_attempt_at IS NOT NULL
                 AND notification_last_error IS NOT NULL
                 AND notification_delivered_at IS NULL)
                OR
                (notification_status = 'delivered'
                 AND notification_attempts > 0
                 AND notification_claimed_at IS NULL
                 AND notification_last_attempt_at IS NOT NULL
                 AND notification_last_error IS NULL
                 AND notification_delivered_at IS NOT NULL)
            ),
            CHECK (
                (status IN ('completed', 'rolled-back', 'rollback-failed')
                 AND notification_status != 'not-required')
                OR
                (status IN ('failed', 'superseded')
                 AND notification_status = 'not-required')
            )
        )
        """
    )
    connection.execute(
        """
        INSERT INTO decision_execution_outcomes(
            decision_id, approval_run_id, status, summary, details_json,
            completed_at, notification_status, notification_attempts,
            notification_claimed_at, notification_last_attempt_at,
            notification_last_error, notification_delivered_at
        )
        SELECT decision_id, approval_run_id, status, summary, details_json,
               completed_at, notification_status, notification_attempts,
               notification_claimed_at, notification_last_attempt_at,
               notification_last_error, notification_delivered_at
        FROM decision_execution_outcomes_v8
        """
    )
    connection.execute("DROP TABLE decision_execution_outcomes_v8")
    connection.execute(
        """
        CREATE INDEX decision_execution_status_idx
        ON decision_execution_outcomes(status, completed_at DESC)
        """
    )
    connection.execute(
        """
        CREATE VIRTUAL TABLE decision_execution_fts USING fts5(
            decision_id UNINDEXED,
            summary,
            details
        )
        """
    )
    connection.execute(
        """
        INSERT INTO decision_execution_fts(decision_id, summary, details)
        SELECT decision_id, summary, details_json
        FROM decision_execution_outcomes
        """
    )
    connection.execute(
        """
        CREATE TRIGGER decision_execution_fts_insert
        AFTER INSERT ON decision_execution_outcomes BEGIN
            INSERT INTO decision_execution_fts(decision_id, summary, details)
            VALUES (new.decision_id, new.summary, new.details_json);
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER decision_execution_fts_update
        AFTER UPDATE ON decision_execution_outcomes BEGIN
            DELETE FROM decision_execution_fts
            WHERE decision_id = old.decision_id;
            INSERT INTO decision_execution_fts(decision_id, summary, details)
            VALUES (new.decision_id, new.summary, new.details_json);
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER decision_execution_fts_delete
        AFTER DELETE ON decision_execution_outcomes BEGIN
            DELETE FROM decision_execution_fts
            WHERE decision_id = old.decision_id;
        END
        """
    )


def read_schema_version(connection: sqlite3.Connection) -> int:
    table_exists = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'schema_migrations'
        """
    ).fetchone()
    if table_exists is None:
        return 0
    row = connection.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
    ).fetchone()
    if row is None:
        return 0
    return int(row[0])


def validate_supported_schema(
    connection: sqlite3.Connection,
    schema_version: int,
) -> None:
    if schema_version < 0 or schema_version > LATEST_SCHEMA_VERSION:
        raise DatabaseError("database schema version is not supported")
    if read_schema_version(connection) != schema_version:
        raise DatabaseError("database schema version changed during validation")
    if schema_version > 0:
        migration_versions = [
            int(row[0])
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        if migration_versions != list(range(1, schema_version + 1)):
            raise DatabaseError("database migration history is invalid")
    with sqlite3.connect(":memory:") as reference:
        _apply_migrations(reference, target_version=schema_version)
        expected_schema = _schema_definition(reference)
    if _schema_definition(connection) != expected_schema:
        raise DatabaseError("database schema does not match its recorded version")
    _verify_fts_content(connection, schema_version)


def _schema_definition(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, str, str], ...]:
    rows = connection.execute(
        """
        SELECT type, name, sql
        FROM sqlite_master
        WHERE sql IS NOT NULL
        ORDER BY type, name
        """
    ).fetchall()
    return tuple(
        (str(row[0]), str(row[1]), " ".join(str(row[2]).split()))
        for row in rows
        if _is_application_schema_object(str(row[1]))
    )


def _is_application_schema_object(name: str) -> bool:
    if name.startswith("sqlite_"):
        return False
    return name not in _FTS_SHADOW_TABLES


def _verify_fts_content(
    connection: sqlite3.Connection,
    schema_version: int,
) -> None:
    comparisons = {
        "task_fts": (
            "SELECT task_id, name, description, source_summary, policy "
            "FROM task_fts ORDER BY task_id",
            "SELECT id, name, description, source_summary, policy_json "
            "FROM tasks WHERE removed_at IS NULL ORDER BY id",
        ),
        "run_fts": (
            "SELECT run_id, task_name, summary, details "
            "FROM run_fts ORDER BY run_id",
            "SELECT id, task_name, summary, details_json "
            "FROM runs ORDER BY id",
        ),
        "decision_fts": (
            "SELECT decision_id, reason, validation_summary, rollback_summary "
            "FROM decision_fts ORDER BY decision_id",
            "SELECT id, reason, validation_summary, rollback_summary "
            "FROM decisions ORDER BY id",
        ),
        "decision_execution_fts": (
            "SELECT decision_id, summary, details "
            "FROM decision_execution_fts ORDER BY decision_id",
            "SELECT decision_id, summary, details_json "
            "FROM decision_execution_outcomes ORDER BY decision_id",
        ),
    }
    for table in _fts_tables_for_schema(schema_version):
        fts_query, source_query = comparisons[table]
        connection.execute(
            f"SELECT bm25({table}) FROM {table} "
            f"WHERE {table} MATCH ? LIMIT 1",
            ("runtasks_fts_validation_token",),
        ).fetchone()
        if connection.execute(fts_query).fetchall() != connection.execute(
            source_query
        ).fetchall():
            raise DatabaseError("database full-text search index is inconsistent")


def verify_fts_integrity(
    connection: sqlite3.Connection,
    schema_version: int,
) -> None:
    try:
        connection.execute("BEGIN")
        for table in _fts_tables_for_schema(schema_version):
            connection.execute(
                f"INSERT INTO {table}({table}, rank) "
                "VALUES ('integrity-check', 1)"
            )
        connection.rollback()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def _fts_tables_for_schema(schema_version: int) -> tuple[str, ...]:
    return tuple(
        table
        for introduced_in, table in _FTS_TABLES_BY_VERSION
        if introduced_in <= schema_version
    )


def _read_health(connection: sqlite3.Connection, path: Path) -> DatabaseHealth:
    foreign_keys = bool(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    busy_timeout_ms = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
    journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    fts5 = verify_fts5(connection)
    return DatabaseHealth(
        path=str(path),
        exists=True,
        schema_version=read_schema_version(connection),
        foreign_keys=foreign_keys,
        busy_timeout_ms=busy_timeout_ms,
        journal_mode=journal_mode,
        fts5=fts5,
    )


def verify_fts5(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE temp.runtasks_fts5_check USING fts5(content)"
        )
        connection.execute("DROP TABLE temp.runtasks_fts5_check")
    except sqlite3.OperationalError as error:
        raise DatabaseError("SQLite FTS5 support is required") from error
    return True
