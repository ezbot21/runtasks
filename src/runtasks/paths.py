from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class RuntimePaths:
    home: Path

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> RuntimePaths:
        values = os.environ if environment is None else environment
        override = values.get("RUNTASKS_HOME")
        home = Path(override).expanduser() if override else Path.home() / "runtasks"
        return cls(home=home)

    @property
    def config_directory(self) -> Path:
        return self.home / "config"

    @property
    def config_file(self) -> Path:
        return self.config_directory / "runtasks.toml"

    @property
    def data_directory(self) -> Path:
        return self.home / "var" / "data"

    @property
    def log_directory(self) -> Path:
        return self.home / "var" / "logs"

    @property
    def backup_directory(self) -> Path:
        return self.home / "var" / "backups"

    @property
    def database_file(self) -> Path:
        return self.data_directory / "runtasks.sqlite3"

    @property
    def secret_environment_file(self) -> Path:
        return self.home / ".env"

    @property
    def required_directories(self) -> tuple[Path, ...]:
        return (
            self.config_directory,
            self.data_directory,
            self.log_directory,
            self.backup_directory,
        )
