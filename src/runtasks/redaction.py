from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping, Sequence


REDACTED = "[REDACTED]"
_MAX_SECRET_LENGTH = 4_096
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|credential|password|private[_-]?key|secret|token)",
    re.IGNORECASE,
)
_GENERIC_PATTERNS = (
    re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{4,}", re.IGNORECASE),
    re.compile(
        r"\b(?:token|password|secret|api[_ -]?key)\s*[:=]\s*\S+",
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
            if len(value) >= 4 and len(value) <= _MAX_SECRET_LENGTH
        }
        return cls(tuple(sorted(secrets, key=len, reverse=True)))

    def text(self, value: str) -> str:
        redacted = value
        for secret in self.secret_values:
            redacted = redacted.replace(secret, REDACTED)
        for pattern in _GENERIC_PATTERNS:
            redacted = pattern.sub(REDACTED, redacted)
        return redacted

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
