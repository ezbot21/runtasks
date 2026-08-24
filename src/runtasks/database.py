from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Iterator


LATEST_SCHEMA_VERSION = 7
BUSY_TIMEOUT_MS = 5_000


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


def initialize_database(path: Path) -> bool:
    existed = path.exists()
    try:
        with database_connection(path, enable_wal=True) as connection:
            changed = _apply_migrations(connection)
            _read_health(connection, path)
        if not existed:
            path.chmod(0o600)
    except (OSError, sqlite3.Error, ValueError) as error:
        raise DatabaseError("database initialization failed") from error
    return changed or not existed


def inspect_database(path: Path) -> DatabaseHealth:
    if not path.is_file():
        raise DatabaseError("database does not exist")
    try:
        with database_connection(path, enable_wal=False) as connection:
            return _read_health(connection, path)
    except (sqlite3.Error, ValueError) as error:
        raise DatabaseError("database health check failed") from error


@contextmanager
def database_connection(
    path: Path, *, enable_wal: bool
) -> Iterator[sqlite3.Connection]:
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


def _apply_migrations(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        current_version = _schema_version(connection)
        if current_version > LATEST_SCHEMA_VERSION:
            raise DatabaseError("database schema is newer than this RunTasks version")
        if current_version == LATEST_SCHEMA_VERSION:
            connection.commit()
            return False

        if current_version < 1:
            _record_migration(connection, 1)
        if current_version < 2:
            _create_task_registry(connection)
            _record_migration(connection, 2)
        if current_version < 3:
            _create_run_history(connection)
            _record_migration(connection, 3)
        if current_version < 4:
            _add_scheduled_run_claims(connection)
            _record_migration(connection, 4)
        if current_version < 5:
            _create_decisions(connection)
            _record_migration(connection, 5)
        if current_version < 6:
            _create_telegram_decision_messages(connection)
            _record_migration(connection, 6)
        if current_version < 7:
            _create_decision_notification_deliveries(connection)
            _record_migration(connection, 7)
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


def _schema_version(connection: sqlite3.Connection) -> int:
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


def _read_health(connection: sqlite3.Connection, path: Path) -> DatabaseHealth:
    foreign_keys = bool(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    busy_timeout_ms = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
    journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    fts5 = _verify_fts5(connection)
    return DatabaseHealth(
        path=str(path),
        exists=True,
        schema_version=_schema_version(connection),
        foreign_keys=foreign_keys,
        busy_timeout_ms=busy_timeout_ms,
        journal_mode=journal_mode,
        fts5=fts5,
    )


def _verify_fts5(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE temp.runtasks_fts5_check USING fts5(content)"
        )
        connection.execute("DROP TABLE temp.runtasks_fts5_check")
    except sqlite3.OperationalError as error:
        raise DatabaseError("SQLite FTS5 support is required") from error
    return True
