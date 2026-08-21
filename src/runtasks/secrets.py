from __future__ import annotations

import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping

from runtasks.paths import RuntimePaths


_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class SecretConfigurationError(ValueError):
    """Raised when a private environment file cannot be loaded safely."""


def load_secret_settings(
    paths: RuntimePaths,
    environment: Mapping[str, str] | None = None,
) -> Mapping[str, str]:
    process_environment = os.environ if environment is None else environment
    values = _load_secret_environment_file(paths.secret_environment_file)
    values.update(
        {
            name: value
            for name, value in process_environment.items()
            if name.startswith("RUNTASKS_") and name != "RUNTASKS_HOME"
        }
    )
    return MappingProxyType(values)


def _load_secret_environment_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise SecretConfigurationError(
            "secret environment file could not be read"
        ) from error

    values: dict[str, str] = {}
    for original_line in lines:
        line = original_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        name, separator, raw_value = line.partition("=")
        name = name.strip()
        if not separator or _ENVIRONMENT_NAME.fullmatch(name) is None:
            raise SecretConfigurationError("secret environment file is invalid")
        values[name] = _parse_environment_file_value(raw_value.strip())
    return values


def _parse_environment_file_value(value: str) -> str:
    if not value:
        return ""
    if value[0] in {'"', "'"}:
        if len(value) < 2 or value[-1] != value[0]:
            raise SecretConfigurationError("secret environment file is invalid")
        return value[1:-1]
    return value
