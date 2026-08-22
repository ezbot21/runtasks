from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from threading import Lock
import traceback
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit


REDACTED = "[REDACTED]"
_MAX_SECRET_LENGTH = 4_096
_SENSITIVE_LABEL_PATTERN = (
    r"api[_-]?key|authorization|credential|passcode|password|pin|"
    r"private[_-]?key|secret|token"
)
_SENSITIVE_KEY = re.compile(
    rf"(?:{_SENSITIVE_LABEL_PATTERN})",
    re.IGNORECASE,
)
_SENSITIVE_PATH_LABEL = re.compile(
    rf"(?:{_SENSITIVE_LABEL_PATTERN})",
    re.IGNORECASE,
)
_SENSITIVE_PATH_ASSIGNMENT = re.compile(
    rf"(?P<label>{_SENSITIVE_LABEL_PATTERN})(?P<separator>[=:_-]?).+",
    re.IGNORECASE,
)
_TELEGRAM_TOKEN_PATH = re.compile(
    r"bot\d{6,}:[A-Za-z0-9_-]{20,}\Z",
    re.IGNORECASE,
)
_GENERIC_PATTERNS = (
    re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{4,}", re.IGNORECASE),
    re.compile(
        r"\b(?:token|password|passcode|pin|secret|api[_ -]?key)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"https?://[^/\s:@]+:[^@\s/]+@", re.IGNORECASE),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
)
_ENVIRONMENT_ASSIGNMENT = re.compile(r"\bRUNTASKS_[A-Z0-9_]+=([^\s]+)")
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*"
    r"(?:TOKEN|PASSWORD|PASSCODE|PIN|SECRET|CREDENTIAL|API_KEY|PRIVATE_KEY)"
    r"[A-Za-z0-9_]*\s*=\s*([^\s]+)",
    re.IGNORECASE,
)
_CREDENTIAL_LABEL = re.compile(
    r"\b(?:Authorization\s*[:=]\s*(?:Bearer|Basic)\s+|"
    r"[\"']?(?:password|passcode|pin|token|secret|credential|api[_ -]?key|private[_ -]?key)"
    r"[\"']?\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;}]+)",
    re.IGNORECASE,
)
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
    r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"']+")
_LOG_NUMERIC_CONTROL_FIELDS = {
    "created",
    "levelno",
    "lineno",
    "msecs",
    "process",
    "relativeCreated",
    "thread",
}
_LOG_HANDLER_LOCK = Lock()
_LOG_SENSITIVE_VALUES: set[str] = set()
_ORIGINAL_LOG_RECORD_FACTORY = logging.getLogRecordFactory()
_ORIGINAL_HANDLER_HANDLE = logging.Handler.handle
_LOG_HANDLER_INSTALLED = False


@dataclass(frozen=True)
class Redactor:
    secret_values: tuple[str, ...] = ()

    @classmethod
    def from_secret_settings(cls, settings: Mapping[str, str]) -> Redactor:
        return cls.from_secret_values(tuple(settings.values()))

    @classmethod
    def from_secret_values(cls, values: Sequence[str]) -> Redactor:
        secrets = {
            value
            for value in values
            if value and len(value) <= _MAX_SECRET_LENGTH
        }
        return cls(tuple(sorted(secrets, key=len, reverse=True)))

    def text(self, value: str) -> str:
        return redact_text(value, sensitive_values=self.secret_values)

    def value(self, value: object, *, key: str | None = None) -> object:
        if key is not None and _SENSITIVE_KEY.search(key):
            return None if value is None else REDACTED
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, tuple):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            redacted: dict[str, object] = {}
            items = sorted(value.items(), key=lambda item: str(item[0]))
            for item_key, item in items:
                original_key = str(item_key)
                safe_key = self.text(original_key)
                candidate = safe_key
                suffix = 2
                while candidate in redacted:
                    candidate = f"{safe_key}#{suffix}"
                    suffix += 1
                redacted[candidate] = self.value(item, key=original_key)
            return redacted
        return value


DEFAULT_REDACTOR = Redactor()


def redact_text(text: str, *, sensitive_values: Iterable[str] = ()) -> str:
    """Remove private configuration and sensitive URL components from text."""
    redacted = text
    values = sorted(
        {value for value in sensitive_values if value},
        key=len,
        reverse=True,
    )
    for value in values:
        redacted = _replace_sensitive_value(redacted, value)
    redacted = _PRIVATE_KEY_BLOCK.sub(REDACTED, redacted)
    for assignment_pattern in (
        _ENVIRONMENT_ASSIGNMENT,
        _CREDENTIAL_ASSIGNMENT,
        _CREDENTIAL_LABEL,
    ):
        redacted = assignment_pattern.sub(
            lambda match: match.group(0).replace(match.group(1), REDACTED),
            redacted,
        )
    redacted = _URL.sub(_redact_url, redacted)
    for pattern in _GENERIC_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    return redacted


class RedactingLogFilter(logging.Filter):
    def __init__(self, *, sensitive_values: Iterable[str] = ()) -> None:
        super().__init__()
        self._sensitive_values = tuple(sensitive_values)

    def filter(self, record: logging.LogRecord) -> bool:
        original_message = record.getMessage()
        redacted_message = redact_text(
            original_message,
            sensitive_values=self._sensitive_values,
        )
        if redacted_message != original_message:
            record.msg = redacted_message
            record.args = ()
        if record.exc_info is not None:
            original_exception = "".join(
                traceback.format_exception(*record.exc_info)
            )
            redacted_exception = redact_text(
                original_exception,
                sensitive_values=self._sensitive_values,
            )
            if redacted_exception != original_exception:
                record.exc_info = None
                record.exc_text = redacted_exception
        for name, value in tuple(record.__dict__.items()):
            if name not in {"msg", "args", "exc_info", "exc_text"}:
                record.__dict__[name] = _redact_log_value(
                    name,
                    value,
                    sensitive_values=self._sensitive_values,
                )
        return True


def install_redacting_log_filter(*, sensitive_values: Iterable[str]) -> None:
    global _LOG_HANDLER_INSTALLED
    with _LOG_HANDLER_LOCK:
        _LOG_SENSITIVE_VALUES.update(value for value in sensitive_values if value)
        if not _LOG_HANDLER_INSTALLED:
            logging.setLogRecordFactory(_redacting_log_record_factory)
            logging.Handler.handle = _redacting_handler_handle  # type: ignore[method-assign]
            _LOG_HANDLER_INSTALLED = True


def _redacting_log_record_factory(
    *args: Any,
    **kwargs: Any,
) -> logging.LogRecord:
    record = _ORIGINAL_LOG_RECORD_FACTORY(*args, **kwargs)
    with _LOG_HANDLER_LOCK:
        sensitive_values = tuple(_LOG_SENSITIVE_VALUES)
    RedactingLogFilter(sensitive_values=sensitive_values).filter(record)
    return record


def _redacting_handler_handle(
    self: logging.Handler,
    record: logging.LogRecord,
) -> bool:
    with _LOG_HANDLER_LOCK:
        sensitive_values = tuple(_LOG_SENSITIVE_VALUES)
    RedactingLogFilter(sensitive_values=sensitive_values).filter(record)
    return _ORIGINAL_HANDLER_HANDLE(self, record)


def _redact_log_value(
    name: str,
    value: Any,
    *,
    sensitive_values: Iterable[str],
) -> Any:
    if name in _LOG_NUMERIC_CONTROL_FIELDS:
        return value
    if _SENSITIVE_KEY.search(name) is not None:
        return REDACTED
    sensitive_value_set = set(sensitive_values)
    if isinstance(value, str):
        return redact_text(value, sensitive_values=sensitive_value_set)
    if value is None or isinstance(value, (bool, int, float)):
        return REDACTED if str(value) in sensitive_value_set else value
    if isinstance(value, bytes):
        return redact_text(
            value.decode("utf-8", errors="replace"),
            sensitive_values=sensitive_value_set,
        )
    if isinstance(value, dict):
        return {
            redact_text(
                str(key),
                sensitive_values=sensitive_value_set,
            ): _redact_log_value(
                str(key),
                item,
                sensitive_values=sensitive_values,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _redact_log_value(name, item, sensitive_values=sensitive_values)
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _redact_log_value(name, item, sensitive_values=sensitive_values)
            for item in value
        )
    if isinstance(value, (set, frozenset)):
        redacted_items = {
            _redact_log_value(name, item, sensitive_values=sensitive_values)
            for item in value
        }
        return type(value)(redacted_items)
    try:
        rendered = str(value)
    except Exception:
        return REDACTED
    return redact_text(rendered, sensitive_values=sensitive_value_set)


def _replace_sensitive_value(text: str, value: str) -> str:
    if len(value) >= 4 and not value.isdecimal():
        return text.replace(value, REDACTED)
    return re.sub(
        rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])",
        REDACTED,
        text,
    )


def _redact_url(match: re.Match[str]) -> str:
    parsed = urlsplit(match.group(0))
    if parsed.hostname is None:
        return REDACTED
    hostname = (
        f"[{parsed.hostname}]"
        if ":" in parsed.hostname
        else parsed.hostname
    )
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = f"{hostname}:{port}" if port is not None else hostname
    query = urlencode(
        [(name, REDACTED) for name, _ in parse_qsl(parsed.query)],
        doseq=True,
    )
    fragment = REDACTED if parsed.fragment else ""
    return urlunsplit(
        (
            parsed.scheme,
            netloc,
            _redact_url_path(parsed.path),
            query,
            fragment,
        )
    )


def _redact_url_path(path: str) -> str:
    parts = path.split("/")
    redact_next = False
    for index, part in enumerate(parts):
        if not part:
            continue
        decoded_part = unquote(part)
        if redact_next:
            parts[index] = REDACTED
            redact_next = False
            continue
        if _TELEGRAM_TOKEN_PATH.fullmatch(decoded_part) is not None:
            parts[index] = f"bot{REDACTED}"
            continue
        if _SENSITIVE_PATH_LABEL.fullmatch(decoded_part) is not None:
            parts[index] = decoded_part
            redact_next = True
            continue
        assignment = _SENSITIVE_PATH_ASSIGNMENT.fullmatch(decoded_part)
        if assignment is not None:
            parts[index] = (
                f"{assignment.group('label')}"
                f"{assignment.group('separator')}{REDACTED}"
            )
    return "/".join(parts)
