from __future__ import annotations

import argparse
import ast
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Iterable, Sequence


_ALLOW_FAKE_SECRET = "release-check: allow-fake-secret"
_SECRET_PATTERNS = (
    ("Telegram bot token", re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    (
        "private key",
        re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    ),
    ("bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9._~-]{20,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
)
_PRODUCTION_ID = re.compile(
    r"^\s*RUNTASKS_TELEGRAM_(?:ALLOWED_USER_IDS|NOTIFICATION_CHAT_ID|THREAD_ID)"
    r"\s*=\s*['\"]?-?\d{5,}(?:\s*,\s*-?\d{5,})*['\"]?\s*$"
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?[A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY)"
    r"[A-Za-z0-9_]*\s*=\s*(?P<value>.+?)\s*$",
    re.IGNORECASE,
)
_HARDCODED_HOME = re.compile(r"(?:^|[^A-Za-z0-9_])(?:/home|/Users)/[^/\s'\"]+")
_RUNTIME_SUFFIXES = (
    ".sqlite3",
    ".sqlite3-shm",
    ".sqlite3-wal",
    ".sqlite3.lock",
    ".log",
    ".bak",
)
_RUNTIME_DIRECTORIES = (
    ("var", "data"),
    ("var", "logs"),
    ("var", "backups"),
)
_TEXT_EXCLUDED_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
)


@dataclass(frozen=True)
class ReleaseFinding:
    kind: str
    path: str
    line: int | None
    message: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def scan_repository(
    root: Path,
    *,
    tracked_paths: Sequence[Path] | None = None,
) -> tuple[ReleaseFinding, ...]:
    repository = root.resolve()
    paths = tuple(tracked_paths) if tracked_paths is not None else _git_paths(repository)
    findings: list[ReleaseFinding] = []
    for relative in sorted(paths, key=lambda item: item.as_posix()):
        normalized = Path(relative.as_posix())
        display = normalized.as_posix()
        absolute = repository / normalized
        findings.extend(_path_findings(normalized))
        if absolute.is_symlink():
            target = os.readlink(absolute)
            if Path(target).is_absolute():
                findings.append(
                    ReleaseFinding(
                        "machine-dependency",
                        display,
                        None,
                        "tracked symlink has an absolute machine-specific target",
                    )
                )
            continue
        if not absolute.is_file() or absolute.suffix.lower() in _TEXT_EXCLUDED_SUFFIXES:
            continue
        try:
            text = absolute.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        findings.extend(_text_findings(normalized, text))
    return tuple(findings)


def _git_paths(root: Path) -> tuple[Path, ...]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("release checks require a Git worktree")
    return tuple(
        Path(os.fsdecode(value))
        for value in result.stdout.split(b"\0")
        if value
    )


def _path_findings(relative: Path) -> Iterable[ReleaseFinding]:
    display = relative.as_posix()
    name = relative.name
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        yield ReleaseFinding(
            "private-environment",
            display,
            None,
            "private environment files must not be committed",
        )
    if any(
        part in {"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
        for part in relative.parts
    ):
        yield ReleaseFinding(
            "machine-dependency",
            display,
            None,
            "development-machine cache must not be committed",
        )
    if any(part in {".venv", "venv"} for part in relative.parts):
        yield ReleaseFinding(
            "machine-dependency",
            display,
            None,
            "virtual environments must not be committed",
        )
    runtime_directory = relative.parts[:2] in _RUNTIME_DIRECTORIES
    runtime_name = name != ".gitkeep" and (
        runtime_directory
        or name.endswith(_RUNTIME_SUFFIXES)
        or "backup" in name.lower() and name.endswith((".json", ".sqlite3"))
    )
    if runtime_name:
        yield ReleaseFinding(
            "runtime-state",
            display,
            None,
            "runtime databases, logs, locks, and backups must not be committed",
        )


def _text_findings(relative: Path, text: str) -> Iterable[ReleaseFinding]:
    display = relative.as_posix()
    for line_number, line in enumerate(text.splitlines(), start=1):
        if _HARDCODED_HOME.search(line):
            yield ReleaseFinding(
                "hardcoded-home",
                display,
                line_number,
                "use home-relative notation, pathlib.Path.home(), or systemd %h",
            )
        if _PRODUCTION_ID.fullmatch(line):
            yield ReleaseFinding(
                "production-id",
                display,
                line_number,
                "numeric Telegram operator IDs belong in private configuration",
            )
        assignment = _SENSITIVE_ASSIGNMENT.fullmatch(line)
        config_like = relative.suffix.lower() in {
            ".toml",
            ".yaml",
            ".yml",
            ".ini",
            ".cfg",
        } or relative.name.startswith(".env")
        if (
            config_like
            and assignment is not None
            and _non_placeholder(assignment.group("value"))
        ):
            yield ReleaseFinding(
                "secret",
                display,
                line_number,
                "non-placeholder secret assignment is committed",
            )
        if _ALLOW_FAKE_SECRET in line:
            continue
        for label, pattern in _SECRET_PATTERNS:
            if pattern.search(line):
                yield ReleaseFinding(
                    "secret",
                    display,
                    line_number,
                    f"representative {label} pattern is committed",
                )
    if relative.parts[:1] == ("src",) and relative.suffix == ".py":
        yield from _python_boundary_findings(relative, text)
    if relative.as_posix() == "pyproject.toml":
        lowered = text.lower()
        for forbidden in ("openclaw", "flask", "fastapi", "django"):
            if forbidden in lowered:
                yield ReleaseFinding(
                    "forbidden-dependency",
                    display,
                    None,
                    f"public release includes forbidden dependency {forbidden}",
                )
        if "file://" in lowered or "path =" in lowered:
            yield ReleaseFinding(
                "machine-dependency",
                display,
                None,
                "project dependencies must not resolve from local machine paths",
            )


def _python_boundary_findings(
    relative: Path,
    text: str,
) -> Iterable[ReleaseFinding]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return
    display = relative.as_posix()
    forbidden_imports = {"openclaw", "flask", "fastapi", "django"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for name in names:
                root_name = name.split(".", 1)[0].lower()
                if root_name in forbidden_imports:
                    yield ReleaseFinding(
                        "forbidden-interface",
                        display,
                        node.lineno,
                        f"public release imports forbidden interface {root_name}",
                    )
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id == "os"
            and function.attr in {"system", "popen"}
        ):
            yield ReleaseFinding(
                "arbitrary-shell",
                display,
                node.lineno,
                "production code must use bounded argv-based process adapters",
            )
        if any(
            keyword.arg == "shell"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        ):
            yield ReleaseFinding(
                "arbitrary-shell",
                display,
                node.lineno,
                "production subprocess calls must not enable a shell",
            )


def _non_placeholder(raw_value: str) -> bool:
    value = raw_value.strip().strip("'\"")
    if value == "" or value.startswith(("${", "<")):
        return False
    return value.lower() not in {
        "changeme",
        "example",
        "placeholder",
        "replace-me",
        "your-token-here",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check tracked release files for secrets and machine-specific state."
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    findings = scan_repository(options.root)
    if options.as_json:
        print(
            json.dumps(
                {
                    "findings": [finding.as_dict() for finding in findings],
                    "status": "ok" if not findings else "failed",
                },
                sort_keys=True,
            )
        )
    elif findings:
        for finding in findings:
            location = finding.path
            if finding.line is not None:
                location = f"{location}:{finding.line}"
            print(f"{location}: {finding.kind}: {finding.message}")
    else:
        print("Public-release secret and portability checks passed.")
    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
