from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib
import os
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class RuntimePaths:
    home: Path
    global_lock_directory: Path

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        global_lock_directory: Path | None = None,
    ) -> RuntimePaths:
        values = os.environ if environment is None else environment
        runtime_default_home = Path.home() / "runtasks"
        override = values.get("RUNTASKS_HOME")
        home = Path(override).expanduser() if override else runtime_default_home
        lock_directory = global_lock_directory or (
            _canonical_user_home() / "runtasks" / "var" / "data"
        )
        return cls(home=home, global_lock_directory=lock_directory)

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

    def telegram_poller_lock_file(self, bot_token: str) -> Path:
        token_key = hashlib.sha256(bot_token.encode("utf-8")).hexdigest()[:20]
        return self.global_lock_directory / f"telegram-poller-{token_key}.lock"

    @property
    def required_directories(self) -> tuple[Path, ...]:
        return (
            self.config_directory,
            self.data_directory,
            self.log_directory,
            self.backup_directory,
        )


def _canonical_user_home() -> Path:
    if os.name == "nt":
        registry = importlib.import_module("winreg")
        try:
            with registry.OpenKey(
                registry.HKEY_CURRENT_USER,
                r"Volatile Environment",
            ) as key:
                profile, _ = registry.QueryValueEx(key, "USERPROFILE")
            return Path(str(profile))
        except OSError:
            return Path.home()

    password_database = importlib.import_module("pwd")
    account = password_database.getpwuid(os.getuid())
    return Path(str(account.pw_dir))
