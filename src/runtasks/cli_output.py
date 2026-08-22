from __future__ import annotations

import json
import sys
from typing import Iterable, Mapping

from runtasks.redaction import DEFAULT_REDACTOR, Redactor


_ACTIVE_REDACTOR = DEFAULT_REDACTOR


def configure_cli_redactor(redactor: Redactor) -> None:
    global _ACTIVE_REDACTOR
    _ACTIVE_REDACTOR = redactor


def print_json(
    payload: Mapping[str, object],
    *,
    public_values: Iterable[object] = (),
) -> None:
    redactor = _output_redactor(public_values)
    print(json.dumps(redactor.value(dict(payload)), indent=2, sort_keys=True))


def print_text(
    message: str,
    *,
    error: bool = False,
    flush: bool = False,
    public_values: Iterable[object] = (),
) -> None:
    redactor = _output_redactor(public_values)
    print(
        redactor.text(message),
        file=sys.stderr if error else sys.stdout,
        flush=flush,
    )


def _output_redactor(public_values: Iterable[object]) -> Redactor:
    public_text = {str(value) for value in public_values}
    if not public_text:
        return _ACTIVE_REDACTOR
    return Redactor(
        tuple(
            value
            for value in _ACTIVE_REDACTOR.secret_values
            if value not in public_text
        )
    )
