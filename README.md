# RunTasks

RunTasks is a portable Python application for durable, reviewed operational tasks. It provides an isolated runtime home, a versioned SQLite Task and Run registry, validated lifecycle commands, a deterministic due-task scheduler, bounded named-handler execution, searchable history, and FTS5-backed search.

## Requirements

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/) 0.12.5
- SQLite compiled with FTS5 (included by common Python distributions)

## Development setup

```bash
uv sync --locked
uv run runtasks status
uv run python -m unittest discover -s tests -v
uv run mypy
```

All project, runtime, and development dependencies are exactly resolved in `uv.lock`. The pinned `tzdata` runtime dependency keeps IANA timezone validation portable on systems without an operating-system timezone database.

## Runtime home

RunTasks resolves its runtime home in this order:

1. `RUNTASKS_HOME`, when set.
2. `~/runtasks`, based on the current user's home directory.

Initialize and inspect it through the public CLI:

```bash
bin/runtasks init
bin/runtasks status
bin/runtasks status --json
```

## Task registry

Tasks are managed only through the validated CLI. Structured output uses the global
`--json` flag before the command; `task add` and `task update` use their own `--json`
option for the input payload. `--output-json` is also available after those two
commands.

```bash
bin/runtasks task add --json "$TASK_PAYLOAD"
bin/runtasks --json task add --json "$TASK_PAYLOAD"
bin/runtasks task list
bin/runtasks task list --json
bin/runtasks task show <task-id>
bin/runtasks task update <task-id> --json '{"description":"Reviewed policy"}'
bin/runtasks task disable <task-id>
bin/runtasks task enable <task-id>
bin/runtasks task remove <task-id>
bin/runtasks run <task-id>
bin/runtasks run-due
bin/runtasks history
bin/runtasks history <task-id>
bin/runtasks search "OAuth safety"
```

An add payload has this shape:

```json
{
  "name": "Pi MCP adapter update check",
  "description": "Review stable adapter releases every 14 days.",
  "source_type": "direct",
  "source_ref": null,
  "source_summary": "Escalate important or uncertain adapter changes.",
  "schedule": {"type": "interval-days", "days": 14, "time": "09:00"},
  "timezone": "Asia/Singapore",
  "next_run_at": "2026-09-01T01:00:00Z",
  "action_mode": "approved-procedure",
  "handler": "pi_mcp_adapter",
  "policy": {
    "important_conditions": ["security", "OAuth safety"],
    "approval_required": true
  }
}
```

Supported schedules are `daily` (`type` and `time`) and `interval-days` (`type`,
`days`, and `time`). Times use `HH:MM`; `next_run_at` must be an offset-aware RFC
3339 timestamp that falls at the configured local schedule time. Task timezones use
IANA `zoneinfo` names and default to `Asia/Singapore` when omitted. Human and JSON
Task output includes the next due time in the Task's configured timezone. Supported action
modes are `check`, `notify`, and `approved-procedure`. The bounded handler registry
currently accepts `manual_notification` for `notify` Tasks and `pi_mcp_adapter` for
`check` or `approved-procedure` Tasks. Handler execution is added separately; Task
registration never interprets policy prose as a command.

Task updates are partial replacements of the supplied fields and preserve the Task
ID and creation timestamp. Disabled Tasks remain visible and are explicitly marked
unavailable for scheduled execution. Manual runs remain an explicit user action and
may still be requested for a disabled Task. Removal creates an internal tombstone: the
Task leaves Task lists and search results and cannot be enabled or updated, but `task show`
continues to expose it by ID with an explicit `removed` status. Its stable database
row remains available as a foreign-key anchor, so retained user-visible history can
still resolve its Task instead of becoming orphaned. Its former identity and policy
fingerprints are released, allowing a later fresh registration. Identity-equivalent
and policy-equivalent adds return a nonzero,
update-oriented duplicate outcome rather than inserting another Task.

## Daily scheduler

`runtasks run-due` is the single scheduler entry point. It does not depend on
systemd: any daily wake mechanism may invoke it. The command takes one scheduler
current time, selects enabled Tasks with `next_run_at` at or before that time, and
claims each due occurrence in SQLite before invoking its named handler. The claim and
Task advancement commit in the same transaction, and a unique scheduled-occurrence
constraint prevents competing processes from claiming the same occurrence.

Each Task advances by its own local-calendar interval using Python `zoneinfo`; a
14-day Task therefore remains fortnightly behind a daily wake. After downtime, one
catch-up Run is claimed for the oldest due occurrence and `next_run_at` advances by
that Task's interval until it is in the future. Skipped overdue occurrences are
recorded in the Run's `scheduling` details. Repeating `run-due` at the same current
time produces no additional Run.

For deterministic tests and controlled replay, supply an offset-aware clock:

```bash
bin/runtasks run-due --now 2026-09-01T01:00:00Z --json
```

With no due work, the command exits successfully and reports `no-due-work`. Handler
failures are retained as failed scheduled Runs, cause exit status 1, and do not
re-open the safely claimed interval. If the process is interrupted after claiming,
history retains the Run as `claimed` or `running`; the Task has already advanced, so
a later invocation cannot duplicate that occurrence. Run history exposes
`scheduled_for` and the resulting `next_run_at` for auditing these outcomes.

## Named execution and Run history

`runtasks run <task-id>` creates a Run with a `manual` trigger, validates its
lifecycle transitions, and invokes only the Task's registered named handler. Policy
text is data: it is never compiled into a shell command or forwarded to an external
process. Unsupported handler names and executable or secret-bearing policy fields are
rejected when the Task is registered.

The current execution modes are deliberately bounded:

- `notify` through `manual_notification` records `manual-action-due` without calling
  an external adapter or mutating an external system.
- `check` (and the read-only check phase of `approved-procedure`) through
  `pi_mcp_adapter` issues only the named `pi_mcp_adapter.inspect` adapter operation.
  The production Pi release-inspection adapter is implemented in a later feature; until
  configured, the local adapter returns a structured failure without executing a command.

Handlers and external adapters exchange structured requests and outcomes. Successful,
failed, and manual-action-due Runs retain Task identity, timestamps, a redacted summary,
structured redacted details, and an optional redacted external log reference. Run
triggers distinguish `scheduled`, `manual`, and future `approval` execution in the same
history. Execution failures return exit status 1 while still recording inspectable history; validation
failures return exit status 2.

Use `runtasks history` for all Runs or `runtasks history <task-id>` for one Task.
Both commands support `--json`. FTS5 search returns matching Tasks and matching Run
summaries/details together, so validation evidence is available through the same public
`search` command.

Credential redaction is centralized across CLI output and stored execution outcomes.
It covers configured `RUNTASKS_*` secret values and common bearer-token, API-key,
credentialed-URL, private-key, GitHub, AWS, Slack, Telegram, and JWT shapes. RunTasks
stores only the redacted structured outcome; larger logs remain external and are
represented by an optional redacted reference.

Initialization creates:

```text
~/runtasks/config/runtasks.toml
~/runtasks/var/data/runtasks.sqlite3
~/runtasks/var/logs/
~/runtasks/var/backups/
```

The command is idempotent. SQLite foreign keys and a five-second busy timeout are enabled on every application connection. WAL mode is requested where the SQLite build supports it, and initialization fails safely if FTS5 is unavailable.

## Configuration

Non-secret settings live in `config/runtasks.toml`. `init` creates the following defaults:

```toml
timezone = "Asia/Singapore"
daily_run_time = "09:00"
```

Copy `config/runtasks.example.toml` when preparing configuration manually. Timezones must be installed IANA timezone names and times use 24-hour `HH:MM` format.

Secrets are loaded separately from non-secret TOML settings. RunTasks reads `<RUNTASKS_HOME>/.env` first, then lets `RUNTASKS_*` process environment variables override matching values. `.env.example` is the committed placeholder for future integration-specific names. Bootstrap commands do not require Telegram or any other live integration. RunTasks does not print secret values, and `.env` files are ignored by Git.

## Testing safely

Behavior tests execute `bin/runtasks` in subprocesses with a temporary `RUNTASKS_HOME`. They do not initialize or inspect the operator's real RunTasks home:

```bash
python -m unittest discover -s tests -v
```

## License

[MIT](LICENSE)
