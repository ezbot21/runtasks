#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Literal, Mapping, NoReturn, Sequence, TypedDict, cast


_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPOSITORY_ROOT / "src"))

from runtasks.tasks import (  # type: ignore[import-untyped]
    REQUIRED_TASK_FIELDS as APPLICATION_REQUIRED_TASK_FIELDS,
    SOURCE_TYPES,
    TASK_FIELDS,
    TaskValidationError,
    parse_task_add_json,
)


SKILL_REQUIRED_TASK_FIELDS = APPLICATION_REQUIRED_TASK_FIELDS | {"timezone"}
REVIEW_POLICY_FIELDS = (
    "automatic_behavior",
    "important_conditions",
    "notification_conditions",
    "approval_requirements",
    "execution",
    "validation",
    "rollback",
    "assumptions",
)
REVIEW_FILE_NAME = re.compile(r"^[0-9a-f]{64}\.json$")


Operation = Literal["add", "update"]


class ReviewProposal(TypedDict):
    operation: Operation
    task_id: str | None
    task: dict[str, Any]


class ReviewError(RuntimeError):
    """Raised when a proposal cannot be reviewed or safely applied."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stage and apply an exact reviewed RunTasks Task proposal."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("stage", help="read one proposal as JSON from stdin")

    apply_parser = subparsers.add_parser(
        "apply", help="apply a staged proposal after explicit confirmation"
    )
    apply_parser.add_argument("review_file")
    apply_parser.add_argument("confirmation")

    discard_parser = subparsers.add_parser(
        "discard", help="discard a staged proposal without changing the registry"
    )
    discard_parser.add_argument("review_file")
    return parser


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _review_root() -> Path:
    user_suffix = str(os.getuid()) if hasattr(os, "getuid") else "current-user"
    root = Path(tempfile.gettempdir()) / f"runtasks-skill-reviews-{user_suffix}"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root.resolve()


def _required_text(values: Mapping[str, Any], field: str) -> str:
    value = values.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ReviewError(f"task.{field} must be a non-empty string")
    return value.strip()


def _required_text_list(policy: Mapping[str, Any], field: str) -> list[str]:
    value = policy.get(field)
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise ReviewError(f"task.policy.{field} must be a non-empty list of text")
    return [cast(str, item).strip() for item in value]


def _validate_proposal(value: object) -> ReviewProposal:
    if not isinstance(value, dict):
        raise ReviewError("proposal must be a JSON object")
    proposal = cast(dict[str, Any], value)
    if set(proposal) != {"operation", "task", "task_id"}:
        raise ReviewError("proposal must contain only operation, task_id, and task")

    operation = proposal.get("operation")
    task_id = proposal.get("task_id")
    if operation not in {"add", "update"}:
        raise ReviewError("proposal.operation must be add or update")
    if operation == "add" and task_id is not None:
        raise ReviewError("an add proposal must use a null task_id")
    if operation == "update" and (
        not isinstance(task_id, str) or not task_id.strip()
    ):
        raise ReviewError("an update proposal must name the existing task_id")

    task_value = proposal.get("task")
    if not isinstance(task_value, dict):
        raise ReviewError("proposal.task must be a JSON object")
    task = cast(dict[str, Any], task_value)
    missing = sorted(SKILL_REQUIRED_TASK_FIELDS - task.keys())
    if missing:
        raise ReviewError("proposal.task is missing: " + ", ".join(missing))
    unsupported = sorted(task.keys() - TASK_FIELDS)
    if unsupported:
        raise ReviewError("proposal.task contains unsupported fields")

    for field in ("name", "description", "source_summary", "timezone", "next_run_at"):
        _required_text(task, field)

    source_type = _required_text(task, "source_type")
    if source_type not in SOURCE_TYPES:
        raise ReviewError(
            "task.source_type must be session, document, direct, or existing-task"
        )
    source_ref = task.get("source_ref")
    if source_ref is not None and (
        not isinstance(source_ref, str) or not source_ref.strip()
    ):
        raise ReviewError("task.source_ref must be null or non-empty text")
    if source_type in {"document", "existing-task"} and source_ref is None:
        raise ReviewError(f"task.source_ref is required for {source_type} sources")

    policy_value = task.get("policy")
    if not isinstance(policy_value, dict):
        raise ReviewError("task.policy must be a JSON object")
    policy = cast(dict[str, Any], policy_value)
    for field in REVIEW_POLICY_FIELDS:
        _required_text_list(policy, field)

    try:
        parse_task_add_json(_canonical_json(task))
    except TaskValidationError as error:
        raise ReviewError(f"task payload is invalid: {error}") from error
    return cast(ReviewProposal, proposal)


def _parse_stdin_proposal() -> ReviewProposal:
    try:
        value: object = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ReviewError("proposal input must be valid JSON") from error
    return _validate_proposal(value)


def _stage(proposal: ReviewProposal) -> int:
    canonical = _canonical_json(proposal)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    review_file = _review_root() / f"{digest}.json"
    temporary_file = review_file.with_suffix(".tmp")
    temporary_file.write_text(canonical + "\n", encoding="utf-8")
    temporary_file.chmod(0o600)
    temporary_file.replace(review_file)

    print(_render_proposal(proposal))
    print(f"Review hash: {digest}")
    print(f"Review file: {review_file}")
    return 0


def _render_proposal(proposal: ReviewProposal) -> str:
    operation = proposal["operation"]
    task_id = proposal["task_id"]
    task = proposal["task"]
    policy = cast(dict[str, Any], task["policy"])
    source = cast(str, task["source_type"])
    if task["source_ref"] is not None:
        source += f" ({task['source_ref']})"
    schedule = cast(dict[str, Any], task["schedule"])
    if schedule["type"] == "daily":
        schedule_text = f"Daily at {schedule['time']}"
    else:
        schedule_text = f"Every {schedule['days']} days at {schedule['time']}"

    lines = [
        "Proposed RunTasks Task",
        "",
        f"Task name: {task['name']}",
        f"Description: {task['description']}",
        f"Schedule: {schedule_text}",
        f"Timezone: {task['timezone']}",
        f"Next due: {task['next_run_at']}",
        f"Operation: {operation.upper()}"
        + ("" if task_id is None else f" Task {task_id}"),
        f"Action mode: {task['action_mode']} via {task['handler']}",
        "",
    ]
    sections = (
        ("Automatic behavior", "automatic_behavior"),
        ("Importance conditions", "important_conditions"),
        ("Notifications", "notification_conditions"),
        ("Approvals", "approval_requirements"),
        ("Execution", "execution"),
        ("Validation", "validation"),
        ("Rollback", "rollback"),
    )
    for heading, key in sections:
        lines.append(f"{heading}:")
        lines.extend(f"- {item}" for item in cast(list[str], policy[key]))
        lines.append("")
    lines.extend(
        [
            f"Source: {source}",
            f"Source summary: {task['source_summary']}",
            "",
            "Assumptions:",
            *(f"- {item}" for item in cast(list[str], policy["assumptions"])),
            "",
            "Structured proposal:",
            json.dumps(proposal, ensure_ascii=False, indent=2, sort_keys=True),
            "",
            "Proceed with this Task proposal?",
            "1. YES",
            "2. NO",
            "3. EDIT",
        ]
    )
    return "\n".join(lines)


def _load_review(review_file_text: str) -> tuple[Path, ReviewProposal]:
    review_file = Path(review_file_text).expanduser().resolve()
    if review_file.parent != _review_root() or not REVIEW_FILE_NAME.fullmatch(
        review_file.name
    ):
        raise ReviewError("review file is not a staged RunTasks proposal")
    try:
        raw = review_file.read_text(encoding="utf-8")
        value: object = json.loads(raw)
    except FileNotFoundError as error:
        raise ReviewError("review file does not exist") from error
    except (json.JSONDecodeError, UnicodeError, OSError) as error:
        raise ReviewError("review file could not be read safely") from error
    proposal = _validate_proposal(value)
    digest = hashlib.sha256(_canonical_json(proposal).encode("utf-8")).hexdigest()
    if review_file.name != f"{digest}.json":
        raise ReviewError("review file changed after it was shown")
    return review_file, proposal


def _repository_root() -> Path:
    if not (_REPOSITORY_ROOT / "bin" / "runtasks").is_file():
        raise ReviewError("RunTasks repository root could not be located")
    return _REPOSITORY_ROOT


def _apply(review_file_text: str, confirmation: str) -> int:
    if confirmation != "YES":
        raise ReviewError("apply requires the exact confirmation YES")
    review_file, proposal = _load_review(review_file_text)
    operation = proposal["operation"]
    task_id = proposal["task_id"]
    task = proposal["task"]
    command = [str(_repository_root() / "bin" / "runtasks"), "--json", "task"]
    if operation == "add":
        command.extend(["add", "--json", _canonical_json(task)])
    else:
        assert task_id is not None
        command.extend(["update", task_id, "--json", _canonical_json(task)])

    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )
    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)
    if completed.returncode == 0:
        review_file.unlink(missing_ok=True)
    return completed.returncode


def _discard(review_file_text: str) -> int:
    review_file, _ = _load_review(review_file_text)
    review_file.unlink()
    print("Discarded reviewed proposal; the RunTasks registry was not changed.")
    return 0


def _fail(message: str) -> NoReturn:
    print(f"RunTasks skill confirmation failed: {message}", file=sys.stderr)
    raise SystemExit(2)


def main(arguments: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    try:
        if options.command == "stage":
            return _stage(_parse_stdin_proposal())
        if options.command == "apply":
            return _apply(options.review_file, options.confirmation)
        if options.command == "discard":
            return _discard(options.review_file)
    except ReviewError as error:
        _fail(str(error))
    except OSError:
        _fail("proposal state could not be accessed safely")
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())
