from __future__ import annotations

import json
import sys
from typing import Mapping

from runtasks.redaction import DEFAULT_REDACTOR, Redactor


_ACTIVE_REDACTOR = DEFAULT_REDACTOR


def configure_cli_redactor(redactor: Redactor) -> None:
    global _ACTIVE_REDACTOR
    _ACTIVE_REDACTOR = redactor


def print_json(payload: Mapping[str, object]) -> None:
    print(json.dumps(_ACTIVE_REDACTOR.value(dict(payload)), indent=2, sort_keys=True))


def print_text(
    message: str,
    *,
    error: bool = False,
    flush: bool = False,
) -> None:
    print(
        _ACTIVE_REDACTOR.text(message),
        file=sys.stderr if error else sys.stdout,
        flush=flush,
    )
