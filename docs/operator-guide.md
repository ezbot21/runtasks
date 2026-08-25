# RunTasks operator guide

This guide covers installing, configuring, operating, recovering, and removing the first public RunTasks release. RunTasks is a single-user service: run every command and every user-systemd unit under the same OS account.

## Install

Requirements:

- Linux with Python 3.11 or newer;
- `uv` 0.12.5;
- SQLite with FTS5;
- a systemd user manager for managed scheduling and Telegram listening;
- Pi only when using the production `pi_mcp_adapter` handler.

From a trusted checkout:

```bash
uv sync --locked
bin/runtasks init
bin/runtasks install
bin/runtasks status
```

`init` creates private configuration, data, log, and backup directories under `RUNTASKS_HOME` (default `~/runtasks`). `install` validates the systemd calendar before writing or enabling units. It installs only:

- `runtasks-scheduler.service` — one-shot `run-due` execution;
- `runtasks-scheduler.timer` — persistent daily wake at 09:00 Asia/Singapore;
- `runtasks-telegram.service` — restart-on-failure long poller.

The installer also exposes the canonical `skills/runtasks` package at `~/.agents/skills/runtasks` and `~/.claude/skills/runtasks`. It launches each locally installed Pi, Codex, OpenCode, or Claude Code harness in an isolated temporary home to verify discovery. A managed copy is used only when an installed harness cannot discover a symlink.

Inspect the installation:

```bash
systemctl --user status runtasks-scheduler.timer
systemctl --user status runtasks-telegram.service
systemctl --user list-timers runtasks-scheduler.timer
bin/runtasks status --json
```

A missed timer wake is recovered by `Persistent=true`. Task intervals remain independent: the daily timer merely asks RunTasks to claim every Task whose `next_run_at` is due.

## Uninstall

Stop and remove managed units and skill discovery entries while preserving operator state:

```bash
bin/runtasks uninstall
```

Delete configuration, the registry, logs, and backups only when intentionally decommissioning the installation:

```bash
bin/runtasks uninstall --remove-data
```

`--remove-data` does not delete the repository, canonical skill source, unrelated user units, unrelated skill directories, or account-global Telegram poller locks. Take and verify a final backup first.

## Configure

Non-secret settings belong in `<RUNTASKS_HOME>/config/runtasks.toml`:

```toml
timezone = "Asia/Singapore"
daily_run_time = "09:00"
```

Secrets and configured Telegram authorization values belong in `<RUNTASKS_HOME>/.env`, never in TOML. The bot token and configured allowlists are not copied into SQLite; Decision audit records necessarily retain the numeric responding user identity plus mapped chat and message IDs. Process environment variables override matching `.env` values.

```bash
cp .env.example ~/runtasks/.env
chmod 600 ~/runtasks/.env
```

Keep `RUNTASKS_HOME` under the current user's home when using the generated systemd units. The application uses portable path resolution, and generated units use systemd `%h`; do not replace those paths with a machine-specific username.

## Use the Agent Skill safely

Ask an installed coding agent to use the `runtasks` skill with a current conversation, direct instruction, document, pasted text, or existing Task. The skill must show:

- add versus update;
- name, schedule, timezone, and automatic check;
- importance and notification conditions;
- approval, exact execution, validation, and rollback;
- every assumption;
- `YES`, `NO`, and `EDIT` controls.

`NO` changes nothing. `EDIT` stages another complete review. Only `YES` submits the exact hash-checked payload to the CLI. The skill never writes SQL and never turns policy prose into an unattended shell command.

Verify discovery by asking the agent to list or explain the `runtasks` skill. If discovery fails, rerun `bin/runtasks install`; do not create an untracked second copy by hand.

## Operate Tasks, Runs, and Decisions

Common commands:

```bash
bin/runtasks task list
bin/runtasks task show <task-id>
bin/runtasks run <task-id>
bin/runtasks run-due
bin/runtasks history <task-id>
bin/runtasks decisions
bin/runtasks decision show <decision-id>
bin/runtasks decision approve <decision-id>
bin/runtasks decision reject <decision-id>
bin/runtasks search "rollback failed"
```

A Decision approval authorizes one immutable plan hash and one exact target. It does not authorize a later release, a floating package version, or arbitrary commands. CLI and Telegram approval use the same idempotent state transition. Repeated approval cannot create another mutation Run; rejection creates no mutation Run.

The scheduler can be invoked without systemd. For deterministic diagnosis, use an explicit offset-aware time:

```bash
bin/runtasks run-due --now 2026-09-01T01:00:00Z --json
```

## Configure Telegram

RunTasks uses private Bot API long polling; it does not require a webhook, public port, domain, or TLS endpoint.

1. Create a bot with the official `@BotFather` account.
2. Put the token only in `~/runtasks/.env` and set mode `0600`.
3. Run `bin/runtasks telegram setup`, then send `/start` to the bot.
4. Copy the reported numeric user and chat IDs into the private `.env` file.
5. Run setup again to verify both IDs.
6. Run `bin/runtasks telegram test`.
7. Start or restart `runtasks-telegram.service`.

A private one-to-one chat is recommended. Group destinations require an allowed administrator and may specify a verified forum thread. Usernames never authorize Decisions. Both numeric sender user ID and chat ID must match.

Decision messages present `1. APPROVE`, `2. REJECT`, and `3. DETAILS`. DETAILS expands redacted evidence and repeats the controls. The listener records a response and requests the separate one-shot scheduler; it never executes a package mutation inside the network callback.

Only one listener may long poll a bot token under an OS account. A token-hash lock enforces this across separate `RUNTASKS_HOME` values.

## Back up and restore SQLite

Create a verified online backup:

```bash
bin/runtasks backup
bin/runtasks backup --json
```

Each backup is a `.sqlite3` file plus matching `.json` metadata. Keep the pair together. RunTasks verifies checksum, integrity, foreign keys, schema, and FTS content before publishing it. Retention keeps one verified artifact for each of the latest 14 UTC days. Unknown or unverifiable files are not deleted automatically.

For an additional off-machine copy, copy both files to storage with access controls appropriate for the policy history. Backups contain Tasks, Runs, Decisions, and redacted evidence; they do not contain the Telegram bot token.

Restore procedure:

```bash
systemctl --user stop runtasks-telegram.service runtasks-scheduler.timer

RUNTASKS_HOME=~/runtasks-restored \
  bin/runtasks restore /safe/path/runtasks-backup-v9-TIMESTAMP.sqlite3 \
  --replace-live

RUNTASKS_HOME=~/runtasks-restored bin/runtasks status
RUNTASKS_HOME=~/runtasks-restored bin/runtasks task list
RUNTASKS_HOME=~/runtasks-restored bin/runtasks history
RUNTASKS_HOME=~/runtasks-restored bin/runtasks decisions
```

Restore stages and validates a fresh database before replacement. Replacing existing live state takes an exclusive application lock and first creates a verified safety backup. Never copy a live WAL database with ordinary file-copy commands.

## Rotate a Telegram bot token

Rotate immediately after any suspected exposure:

1. Revoke or regenerate the token through the official `@BotFather` account.
2. Stop `runtasks-telegram.service`.
3. Replace only `RUNTASKS_TELEGRAM_BOT_TOKEN` in the private `.env` file.
4. Keep the file at mode `0600`.
5. Run `bin/runtasks telegram setup` and `bin/runtasks telegram test`.
6. Restart the listener and inspect its status.
7. Remove exposed terminal captures, tickets, or logs where possible and run the public-release secret scan before the next commit.

The token is not stored in SQLite, so database migration is not required. The old token-keyed coordination lock may remain; it contains only a one-way token hash and no credential.

## Troubleshoot

### `init` reports that FTS5 is unavailable

Install a Python/SQLite build compiled with FTS5. Verify with:

```bash
uv run python - <<'PY'
import sqlite3
with sqlite3.connect(":memory:") as db:
    db.execute("CREATE VIRTUAL TABLE check_fts USING fts5(content)")
print("FTS5 available")
PY
```

### The timer is not firing

Run:

```bash
systemd-analyze calendar '*-*-* 09:00:00 Asia/Singapore'
systemctl --user status runtasks-scheduler.timer
systemctl --user list-timers runtasks-scheduler.timer
journalctl --user -u runtasks-scheduler.service
```

Rerun `bin/runtasks install` after fixing systemd or timezone-data problems.

### A Task remains `claimed` or `running`

Inspect `history` and the Decision. Scheduled occurrences are deliberately not reclaimed after an ambiguous process interruption because duplicate mutation is more dangerous than a visible terminal Run. Pi MCP approval execution has separate recovery checkpoints and reconciles observed package state before continuing.

### Telegram setup sees no `/start`

Confirm the token, remove any configured webhook, send a fresh `/start` after setup reports readiness, and ensure no second process polls the token. Inspect `systemctl --user status runtasks-telegram.service` and the account-global lock error.

### Telegram rejects a callback

Confirm both numeric IDs, private-chat type, message mapping, and current Decision state. Forwarded messages and usernames do not authorize work. Repeated or stale controls intentionally return the current safe state.

### A backup or restore is rejected

Keep the database and metadata pair together, check permissions and free space, and do not edit either file. A checksum, schema, foreign-key, FTS, or integrity failure is a safety stop, not a repair operation.

### Pi MCP update or rollback fails

Inspect:

```bash
bin/runtasks decision show <decision-id>
bin/runtasks history <task-id>
bin/runtasks search "rollback"
systemctl --user status pi-web.service
```

A `rollback-failed` Decision is critical: stop further update attempts, preserve history, restore service manually to the exact recorded old pin if safe, and validate Pi Web plus a fresh exact `MCP_ADAPTER_OK` result before declaring recovery.
