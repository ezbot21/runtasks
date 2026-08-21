from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Iterator


LATEST_SCHEMA_VERSION = 1
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
        with _database_connection(path, enable_wal=True) as connection:
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
        with _database_connection(path, enable_wal=False) as connection:
            return _read_health(connection, path)
    except (sqlite3.Error, ValueError) as error:
        raise DatabaseError("database health check failed") from error


@contextmanager
def _database_connection(
    path: Path, *, enable_wal: bool
) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        if enable_wal:
            connection.execute("PRAGMA journal_mode = WAL").fetchone()
        yield connection
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

        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (1, datetime.now(timezone.utc).isoformat()),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return True


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
