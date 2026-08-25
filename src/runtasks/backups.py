from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, BinaryIO, Iterator
from uuid import uuid4

from runtasks.database import (
    LATEST_SCHEMA_VERSION,
    DatabaseError,
    database_access_guard,
    initialize_staged_database,
    read_only_database_connection,
    read_schema_version,
    validate_supported_schema,
    verify_fts5,
    verify_fts_integrity,
)


BACKUP_FORMAT = "runtasks-sqlite-backup"
BACKUP_FORMAT_VERSION = 1


class BackupError(RuntimeError):
    """Raised when a safe database backup cannot be created or verified."""


@dataclass(frozen=True)
class BackupArtifact:
    path: Path
    metadata_path: Path
    created_at: datetime
    schema_version: int
    checksum_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "checksum_sha256": self.checksum_sha256,
            "created_at": _isoformat_utc(self.created_at),
            "metadata_path": str(self.metadata_path),
            "path": str(self.path),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class DatabaseValidation:
    schema_version: int


@dataclass(frozen=True)
class RestoreOutcome:
    source: Path
    destination: Path
    source_schema_version: int
    schema_version: int
    safety_backup: BackupArtifact | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "destination": str(self.destination),
            "safety_backup": (
                None if self.safety_backup is None else self.safety_backup.as_dict()
            ),
            "schema_version": self.schema_version,
            "source": str(self.source),
            "source_schema_version": self.source_schema_version,
        }


@dataclass(frozen=True)
class _VerifiedRetentionBackup:
    database_path: Path
    metadata_path: Path
    created_at: datetime
    schema_version: int
    checksum_sha256: str


def create_backup(
    database_path: Path,
    backup_directory: Path,
    *,
    created_at: datetime | None = None,
) -> BackupArtifact:
    return _create_backup(
        database_path,
        backup_directory,
        created_at=created_at,
        source_is_locked=False,
    )


def create_backup_from_locked_database(
    database_path: Path,
    backup_directory: Path,
) -> BackupArtifact:
    return _create_backup(
        database_path,
        backup_directory,
        created_at=None,
        source_is_locked=True,
    )


def _create_backup(
    database_path: Path,
    backup_directory: Path,
    *,
    created_at: datetime | None,
    source_is_locked: bool,
) -> BackupArtifact:
    if not database_path.is_file():
        raise BackupError("database does not exist")
    timestamp = _utc_timestamp(created_at)
    temporary_path: Path | None = None
    final_path: Path | None = None
    metadata_path: Path | None = None
    final_published = False
    metadata_published = False
    try:
        backup_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not backup_directory.is_dir():
            raise BackupError("backup destination is not a directory")
        temporary_path = backup_directory / f".backup-{uuid4().hex}.sqlite3"
        _create_private_empty_file(temporary_path)
        _online_backup(
            database_path,
            temporary_path,
            guard_source=not source_is_locked,
        )
        _prepare_portable_backup(temporary_path)
        validation = validate_database(temporary_path)
        checksum_sha256 = _sha256_file(temporary_path)
        final_path = backup_directory / _backup_filename(
            validation.schema_version,
            timestamp,
        )
        metadata_path = final_path.with_suffix(".json")
        if final_path.exists() or metadata_path.exists():
            raise BackupError("backup destination already exists")
        temporary_path.chmod(0o600)
        os.replace(temporary_path, final_path)
        final_published = True
        metadata = {
            "checksum_sha256": checksum_sha256,
            "created_at": _isoformat_utc(timestamp),
            "database_file": final_path.name,
            "format": BACKUP_FORMAT,
            "format_version": BACKUP_FORMAT_VERSION,
            "schema_version": validation.schema_version,
        }
        _write_private_json(metadata_path, metadata)
        metadata_published = True
        _apply_retention(backup_directory, pinned_path=final_path)
        return BackupArtifact(
            path=final_path,
            metadata_path=metadata_path,
            created_at=timestamp,
            schema_version=validation.schema_version,
            checksum_sha256=checksum_sha256,
        )
    except BackupError:
        _cleanup_failed_backup(
            temporary_path,
            final_path if final_published else None,
            metadata_path if metadata_published else None,
        )
        raise
    except (OSError, sqlite3.Error, ValueError) as error:
        _cleanup_failed_backup(
            temporary_path,
            final_path if final_published else None,
            metadata_path if metadata_published else None,
        )
        raise BackupError("backup creation failed") from error


def restore_backup(
    backup_path: Path,
    destination_path: Path,
    backup_directory: Path,
    *,
    replace_live: bool,
) -> RestoreOutcome:
    if not replace_live:
        raise BackupError("restore requires explicit --replace-live confirmation")
    source_path = backup_path.expanduser().resolve()
    destination = destination_path.expanduser().resolve()
    if source_path == destination:
        raise BackupError("backup source must be separate from the live database")
    metadata = _read_backup_metadata(source_path)

    staging_path: Path | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not destination.parent.is_dir():
            raise BackupError("restore destination is not a directory")
        staging_path = destination.parent / f".restore-{uuid4().hex}.sqlite3"
        _create_private_empty_file(staging_path)
        staged_checksum = _copy_backup_artifact(source_path, staging_path)
        if staged_checksum != metadata.checksum_sha256:
            raise BackupError("backup checksum does not match its metadata")
        source_validation = validate_database(
            staging_path,
            expected_schema_version=metadata.schema_version,
        )
        _prepare_staged_database(staging_path)
        staged_validation = validate_database(
            staging_path,
            expected_schema_version=LATEST_SCHEMA_VERSION,
            require_current_schema=True,
        )
        _remove_sqlite_sidecars(staging_path)
        _remove_if_present(Path(f"{staging_path}.lock"))
        with database_access_guard(destination, exclusive=True):
            safety_backup = (
                _create_backup(
                    destination,
                    backup_directory,
                    created_at=None,
                    source_is_locked=True,
                )
                if destination.is_file()
                else None
            )
            if destination.exists() and not destination.is_file():
                raise BackupError("live database destination is not a file")
            if destination.is_file():
                _checkpoint_live_database(destination)
                _remove_sqlite_sidecars(destination)
            os.replace(staging_path, destination)
    except (BackupError, DatabaseError):
        if staging_path is not None:
            _remove_restore_staging(staging_path)
        raise
    except (OSError, sqlite3.Error, ValueError) as error:
        if staging_path is not None:
            _remove_restore_staging(staging_path)
        raise BackupError("database restore failed") from error

    return RestoreOutcome(
        source=source_path,
        destination=destination,
        source_schema_version=source_validation.schema_version,
        schema_version=staged_validation.schema_version,
        safety_backup=safety_backup,
    )


def validate_database(
    database_path: Path,
    *,
    expected_schema_version: int | None = None,
    require_current_schema: bool = False,
) -> DatabaseValidation:
    if not database_path.is_file():
        raise BackupError("backup database does not exist")
    try:
        with read_only_database_connection(database_path, guard=False) as connection:
            integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
            if not integrity_rows or any(str(row[0]).lower() != "ok" for row in integrity_rows):
                raise BackupError("backup database integrity check failed")
            foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_rows:
                raise BackupError("backup database foreign key check failed")
            schema_version = read_schema_version(connection)
            if schema_version < 0 or schema_version > LATEST_SCHEMA_VERSION:
                raise BackupError("backup database schema is not supported")
            if expected_schema_version is not None and schema_version != expected_schema_version:
                raise BackupError("backup schema version does not match its metadata")
            if require_current_schema and schema_version != LATEST_SCHEMA_VERSION:
                raise BackupError("backup database schema is not current")
            validate_supported_schema(connection, schema_version)
        with closing(sqlite3.connect(database_path, timeout=5.0)) as writable:
            verify_fts_integrity(writable, schema_version)
        with closing(sqlite3.connect(":memory:")) as runtime_connection:
            verify_fts5(runtime_connection)
    except BackupError:
        raise
    except DatabaseError as error:
        raise BackupError("backup database schema validation failed") from error
    except (OSError, sqlite3.Error, ValueError) as error:
        raise BackupError("backup database validation failed") from error
    return DatabaseValidation(schema_version=schema_version)


def _copy_backup_artifact(source_path: Path, destination_path: Path) -> str:
    digest = hashlib.sha256()
    with source_path.open("rb") as source:
        with destination_path.open("wb") as destination:
            for chunk in _file_chunks(source):
                digest.update(chunk)
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in _file_chunks(file):
            digest.update(chunk)
    return digest.hexdigest()


def _file_chunks(file: BinaryIO) -> Iterator[bytes]:
    while chunk := file.read(1024 * 1024):
        yield chunk


def _online_backup(
    source_path: Path,
    destination_path: Path,
    *,
    guard_source: bool,
) -> None:
    with read_only_database_connection(source_path, guard=guard_source) as source:
        with closing(sqlite3.connect(destination_path)) as destination:
            source.backup(destination)


def _prepare_portable_backup(database_path: Path) -> None:
    with closing(sqlite3.connect(database_path, timeout=5.0)) as connection:
        journal_mode = str(
            connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
        ).lower()
        if journal_mode != "delete":
            raise BackupError("backup database could not enable portable journal mode")


def _prepare_staged_database(database_path: Path) -> None:
    try:
        initialize_staged_database(database_path)
    except DatabaseError as error:
        raise BackupError("backup staging migration failed") from error


def _apply_retention(
    backup_directory: Path,
    *,
    pinned_path: Path,
) -> None:
    managed_backups = [
        backup
        for metadata_path in backup_directory.glob("runtasks-backup-v*-*.json")
        if (backup := _read_verified_retention_backup(metadata_path)) is not None
    ]
    managed_backups.sort(key=lambda backup: backup.created_at, reverse=True)
    pinned_backup = next(
        (
            backup
            for backup in managed_backups
            if backup.database_path == pinned_path
        ),
        None,
    )
    if pinned_backup is None:
        raise BackupError("new backup failed retention validation")
    retained_days: set[date] = {pinned_backup.created_at.date()}
    retained_paths: set[Path] = {pinned_backup.database_path}
    for backup in managed_backups:
        backup_day = backup.created_at.date()
        if backup_day not in retained_days and len(retained_days) < 14:
            retained_days.add(backup_day)
            retained_paths.add(backup.database_path)

    for backup in managed_backups:
        if backup.database_path in retained_paths:
            continue
        _delete_artifact_pair(backup.database_path, backup.metadata_path)


def _delete_artifact_pair(database_path: Path, metadata_path: Path) -> None:
    metadata_content: bytes | None = None
    if metadata_path.exists():
        try:
            metadata_content = metadata_path.read_bytes()
            metadata_path.unlink()
        except OSError as error:
            raise BackupError("backup retention failed") from error
    try:
        database_path.unlink(missing_ok=True)
    except OSError as error:
        if metadata_content is not None:
            try:
                _write_private_bytes(metadata_path, metadata_content)
            except OSError as rollback_error:
                raise BackupError("backup retention rollback failed") from rollback_error
        raise BackupError("backup retention failed") from error


def _read_verified_retention_backup(metadata_path: Path) -> _VerifiedRetentionBackup | None:
    try:
        database_path = metadata_path.with_suffix(".sqlite3")
        metadata = _read_backup_metadata(database_path)
        if _sha256_file(database_path) != metadata.checksum_sha256:
            raise BackupError("backup checksum does not match its metadata")
        validate_database(
            database_path,
            expected_schema_version=metadata.schema_version,
        )
    except (BackupError, json.JSONDecodeError, OSError, ValueError):
        return None
    return _VerifiedRetentionBackup(
        database_path=database_path,
        metadata_path=metadata_path,
        created_at=metadata.created_at,
        schema_version=metadata.schema_version,
        checksum_sha256=metadata.checksum_sha256,
    )


def _read_backup_metadata(database_path: Path) -> _VerifiedRetentionBackup:
    metadata_path = database_path.with_suffix(".json")
    if (
        database_path.is_symlink()
        or metadata_path.is_symlink()
        or not database_path.is_file()
        or not metadata_path.is_file()
    ):
        raise BackupError("backup artifact or metadata does not exist")
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise BackupError("backup metadata is invalid") from error
    if not isinstance(payload, dict):
        raise BackupError("backup metadata is invalid")
    checksum_sha256 = payload.get("checksum_sha256")
    database_file = payload.get("database_file")
    created_at_value = payload.get("created_at")
    schema_version = payload.get("schema_version")
    if (
        payload.get("format") != BACKUP_FORMAT
        or payload.get("format_version") != BACKUP_FORMAT_VERSION
        or not isinstance(checksum_sha256, str)
        or len(checksum_sha256) != 64
        or any(character not in "0123456789abcdef" for character in checksum_sha256)
        or database_file != database_path.name
        or not isinstance(created_at_value, str)
        or not isinstance(schema_version, int)
    ):
        raise BackupError("backup metadata is invalid")
    try:
        created_at = _parse_timestamp(created_at_value)
    except ValueError as error:
        raise BackupError("backup metadata is invalid") from error
    if database_path.name != _backup_filename(schema_version, created_at):
        raise BackupError("backup name does not match its metadata")
    return _VerifiedRetentionBackup(
        database_path=database_path,
        metadata_path=metadata_path,
        created_at=created_at,
        schema_version=schema_version,
        checksum_sha256=checksum_sha256,
    )


def _checkpoint_live_database(database_path: Path) -> None:
    with closing(sqlite3.connect(database_path, timeout=5.0)) as connection:
        connection.execute("PRAGMA busy_timeout = 5000")
        result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result is not None and int(result[0]) != 0:
            raise BackupError("live database is busy; stop RunTasks before restoring")


def _remove_sqlite_sidecars(
    database_path: Path,
    *,
    ignore_errors: bool = False,
) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{database_path}{suffix}")
        try:
            sidecar.unlink(missing_ok=True)
        except OSError:
            if not ignore_errors:
                raise


def _write_private_bytes(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())


def _write_private_json(path: Path, payload: dict[str, object]) -> None:
    temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2, sort_keys=True)
            file.write("\n")
        os.replace(temporary_path, path)
    except Exception:
        _remove_if_present(temporary_path)
        raise


def _create_private_empty_file(path: Path) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)


def _utc_timestamp(value: datetime | None) -> datetime:
    timestamp = datetime.now(timezone.utc) if value is None else value
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise BackupError("backup creation time must include a timezone")
    return timestamp.astimezone(timezone.utc)


def _backup_filename(schema_version: int, created_at: datetime) -> str:
    compact_time = created_at.astimezone(timezone.utc).strftime(
        "%Y%m%dT%H%M%S.%fZ"
    )
    return f"runtasks-backup-v{schema_version}-{compact_time}.sqlite3"


def _isoformat_utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("backup timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _remove_restore_staging(staging_path: Path) -> None:
    _remove_if_present(staging_path)
    _remove_if_present(Path(f"{staging_path}.lock"))
    _remove_sqlite_sidecars(staging_path, ignore_errors=True)


def _cleanup_failed_backup(
    temporary_path: Path | None,
    final_path: Path | None,
    metadata_path: Path | None,
) -> None:
    if temporary_path is not None:
        _remove_if_present(temporary_path)
    if metadata_path is not None:
        _remove_if_present(metadata_path)
    if final_path is not None:
        _remove_if_present(final_path)


def _remove_if_present(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
