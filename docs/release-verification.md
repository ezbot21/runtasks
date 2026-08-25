# Public-release verification

Run these checks from a clean checkout before publishing a RunTasks release.

## Automated gates

```bash
uv lock --check
uv sync --locked
uv run ruff check .
uv run mypy
uv run python -m runtasks.release_checks
uv run python -m unittest discover -s tests -v
```

The release check scans Git-tracked files rather than the working directory's ignored runtime state. CI repeats the same locked dependency, lint, type, secret/portability, contract, subprocess, and end-to-end gates.

The safe end-to-end module can be run independently:

```bash
uv run python -m unittest tests.test_end_to_end_release -v
```

It uses a temporary `RUNTASKS_HOME`, advances an explicit scheduler clock, discovers an important fake release, sends and approves a fake Telegram Decision, executes only fixture adapters, validates success, searches history, verifies an exact rollback, and verifies critical failed-rollback reporting. It does not access real Pi, npm, GitHub, systemd services, networks, package installation, or Telegram.

## Platform capabilities

Verify SQLite FTS5:

```bash
uv run python - <<'PY'
import sqlite3
with sqlite3.connect(":memory:") as db:
    db.execute("CREATE VIRTUAL TABLE release_fts USING fts5(content)")
    db.execute("INSERT INTO release_fts(content) VALUES ('ready')")
    assert db.execute(
        "SELECT content FROM release_fts WHERE release_fts MATCH 'ready'"
    ).fetchone() == ("ready",)
print("FTS5 ready")
PY
```

When `systemd-analyze` is installed, verify the exact timer expression used by the installer:

```bash
systemd-analyze calendar '*-*-* 09:00:00 Asia/Singapore'
```

The release test performs this check conditionally, and the production installer refuses to enable the timer when the locally installed systemd cannot validate the expression.

## Agent Skill discovery

On every locally installed supported agent, run:

```bash
bin/runtasks install
```

Confirm the installer reports discovery for Pi, Codex, OpenCode, and Claude Code executables present on `PATH`. Then ask each agent to list or explain the `runtasks` skill. The automated installation tests use fake harnesses to verify symlink discovery, managed-copy fallback, idempotent reinstall, and cleanup, but a release operator should still check actual locally installed products.

## Telegram authorization

Use the private setup flow and verify:

- numeric user and chat IDs are reported from a fresh `/start`;
- configured IDs both match;
- `telegram test` succeeds;
- an unauthorized user, unauthorized chat, malformed callback, and repeated button press do not create mutation work.

Automated tests use recorded Bot API updates and fake clients. No CI job contacts Telegram.

## Update and rollback proof

The public release is not ready unless tests prove:

- all intervening fake releases are represented in evidence;
- important and uncertain results create immutable Decisions;
- non-important and no-change results remain quiet;
- approval executes one exact target at most once;
- stale installed state prevents mutation;
- target metadata, Pi Web health, and exact `MCP_ADAPTER_OK` are required;
- every post-mutation validation failure attempts the exact old pin;
- verified rollback is recorded and announced;
- every rollback-step failure is critical, searchable, and announced;
- subprocess output and notification text remain redacted.

## Repository contents

Inspect the release tree and dependency lock. It must contain no:

- real token, credential, Telegram production ID, or private environment file;
- developer-specific absolute home path or absolute discovery symlink;
- runtime SQLite database, WAL file, log, lock, backup, or virtual environment;
- local path dependency or floating executable integration;
- web UI or web framework;
- OpenClaw dependency;
- MCP-based scheduler;
- arbitrary policy-to-shell execution surface;
- private policy transcript.

The expected product boundary is a CLI, SQLite registry, Agent Skill, daily systemd adapter, direct Telegram long poller, and named Pi MCP handler.
