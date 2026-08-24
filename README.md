# RunTasks

RunTasks is a portable Python application for durable, reviewed operational tasks. It provides an isolated runtime home, a versioned SQLite Task, Run, and Decision registry, validated lifecycle commands, a deterministic due-task scheduler, bounded named-handler execution, immutable human approvals, searchable history, and FTS5-backed search.

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

## Agent Skill

The canonical standards-compatible Agent Skill is available directly from the repository at [`skills/runtasks/SKILL.md`](skills/runtasks/SKILL.md). It extracts one or more Task proposals from conversation context, documents, pasted text, direct instructions, or existing Tasks. Each proposal is staged as a hash-checked review and shows its full policy and assumptions before asking for `YES`, `NO`, or `EDIT`; only an explicit `YES` can invoke `task add` or `task update` with the exact reviewed payload.

Global cross-agent discovery installation is intentionally handled by a later feature. Until then, load or invoke the canonical skill from this repository in any Agent Skills-compatible harness.

## Runtime home

RunTasks resolves its runtime home in this order:

1. `RUNTASKS_HOME`, when set.
2. `~/runtasks`, based on the current user's home directory.

This override relocates runtime-specific configuration, database, log, and backup files. The token-hash Telegram poller lock is the sole account-global exception: it remains under the OS account's canonical `~/runtasks/var/data/` directory so separate runtime homes cannot poll the same bot concurrently. See [ADR 0001](docs/adr/0001-account-global-telegram-poller-lock.md).

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
bin/runtasks decisions
bin/runtasks decision show <decision-id>
bin/runtasks decision approve <decision-id>
bin/runtasks decision reject <decision-id>
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
14-day Task therefore remains fortnightly behind a daily wake. A local wall time that
does not exist during a daylight-saving transition is skipped rather than silently
moving the Task to a different displayed time. After downtime, one
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
- The scheduled check phase of `approved-procedure` through `pi_mcp_adapter` issues
  only the named, read-only `pi_mcp_adapter.inspect` operation. It reads the installed
  `pi-mcp-adapter` package metadata relative to `PI_CODING_AGENT_DIR` (or the current
  user's `~/.pi/agent`), queries npm's stable `latest` dist-tag using Pi's configured
  `npmCommand`, gathers every intervening version from GitHub Releases and the project
  changelog, and sends normalized evidence to a tool-disabled, ephemeral Pi evaluator.
  It never runs `pi install`, restarts a service, or changes the exact package pin.

A same-version result records `no-change`. A high-confidence assessment covering only
routine features, documentation, refactoring, or irrelevant fixes records
`non-important`. Both outcomes remain quiet because they create no pending Decision.
Important results and every uncertain result—source failure, missing release evidence,
malformed metadata, evaluator error, low confidence, or timeout—record
`decision-required` with an immutable exact-version plan when valid versions are known.
Version shape alone is never used to decide importance.

Handlers and external adapters exchange structured requests and outcomes. Successful,
failed, and manual-action-due Runs retain Task identity, timestamps, a redacted summary,
structured redacted details, and an optional redacted external log reference. Run
triggers distinguish `scheduled`, `manual`, and separately queued `approval` execution
in the same history. Execution failures return exit status 1 while still recording
inspectable history; validation failures return exit status 2.

Use `runtasks history` for all Runs or `runtasks history <task-id>` for one Task.
Both commands support `--json`. FTS5 search returns matching Tasks, Run
summaries/details, and Decision summaries together through the same public `search`
command.

Credential redaction is centralized across CLI output and stored execution outcomes.
It covers configured `RUNTASKS_*` secret values and common bearer-token, API-key,
credentialed-URL, private-key, GitHub, AWS, Slack, Telegram, and JWT shapes. RunTasks
stores only the redacted structured outcome; larger logs remain external and are
represented by an optional redacted reference.

## Immutable Decisions

An `approved-procedure` handler may finish its read-only check by returning a structured
Decision request containing one exact plan, a reason, a validation summary, and a
rollback summary. The plan must name the Task handler and include a bounded operation,
its complete parameter object, validation instructions, and rollback instructions;
additional evidence is optional. RunTasks rejects secret-bearing operation fields,
redacts evidence values and keys, serializes every stored plan field as strict canonical
JSON, hashes that exact representation with SHA-256, and creates the pending Decision in
the same transaction that moves the requesting Run to `decision-required`. Database
triggers prevent later edits to the plan, response, or audit record.

Use `runtasks decisions` and `runtasks decision show <decision-id>` to inspect pending
and answered Decisions in human or JSON mode. `decision reject` closes a pending
Decision without invoking a handler or creating mutation work. `decision approve`
authorizes only the stored plan hash and atomically creates one claimed approval Run for
a separate runner to execute. Repeating the same response returns the existing state;
a conflicting response fails with a nonzero status. Concurrent approvals cannot create
more than one approval Run. A pending Decision cannot be approved after its Task has
been removed, although it can still be rejected to close the audit record.

Decision reason, validation summary, and rollback summary text participates in the
public FTS5-backed `search` command alongside Task and Run matches. Plan evidence is
redacted before storage and output. Telegram and CLI responses share this same
transactional Decision transition: an approval can create only one claimed approval
Run, while rejection creates no execution work. The production Pi MCP adapter handler
now supplies real read-only release assessments and immutable exact-version update
plans. Execution of claimed approval Runs, package mutation, restart validation, and
rollback remain separate later work; this handler never performs them during a check.

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

## Telegram notifications

RunTasks integrates directly with Telegram through the exactly pinned and reviewed `python-telegram-bot==22.8` release. It uses Bot API `getUpdates` long polling, never a webhook, so it does not open a public inbound port or require a domain, TLS certificate, or reverse proxy.

The initial operating model is a private one-to-one chat with the bot:

1. Create a bot through Telegram's official `@BotFather` account and copy `.env.example` to `~/runtasks/.env`.
2. Put only the bot token in `.env`, then protect the file:

   ```bash
   chmod 600 ~/runtasks/.env
   ```

3. Start setup, then send `/start` when the command says it is ready. Setup discards older pending updates so stale chats cannot be mistaken for the current operator:

   ```bash
   bin/runtasks telegram setup
   bin/runtasks --json telegram setup
   ```

   The readiness prompt is written to stderr so JSON on stdout remains machine-readable.

4. Set `RUNTASKS_TELEGRAM_ALLOWED_USER_IDS` and `RUNTASKS_TELEGRAM_NOTIFICATION_CHAT_ID` to the numeric values shown. Authorization never relies on a username. A positive private-DM chat ID is recommended. For a negative group ID, Telegram must verify that an allowed user is an administrator; `RUNTASKS_TELEGRAM_THREAD_ID` may then select a forum topic in a verified supergroup.
5. Run `telegram setup` again and send a fresh `/start`. With authorization configured, human output reports `Authorization: verified` or `Authorization: mismatch`; JSON includes the separate numeric-user and chat checks.
6. Send a harmless redacted notification and check the exit status:

   ```bash
   bin/runtasks telegram test
   bin/runtasks --json telegram test
   ```

7. Run the long-poll listener. It reports authorized `/start` checks to the local console, sends each unmapped pending Decision with inline controls, and handles callbacks:

   ```bash
   bin/runtasks telegram listen
   ```

   Decision messages use exactly these controls:

   ```text
   [1. APPROVE] [2. REJECT] [3. DETAILS]
   ```

   `APPROVE` commits the exact stored plan, creates at most one approval Run, and asks
   the separately installed `runtasks-scheduler.service` one-shot runner to process it;
   the listener never invokes a mutation handler. The wake request is retained durably
   and retried after listener polling or restart until the one-shot adapter accepts it.
   `REJECT` closes the Decision without
   execution. `DETAILS` sends the expanded redacted plan evidence and repeats the same
   controls. Repeated, conflicting, malformed, unknown, expired, or unauthorized
   callbacks preserve a safe state and receive a current-state or error response.

Setup and listening refuse to poll while a webhook is configured. A token-keyed global lock file under the user's default `~/runtasks/var/data/` directory prevents a second runtime home from polling the same bot concurrently; other runtime files remain under the configured `RUNTASKS_HOME`. The long-running listener intentionally uses human output only; `--json` is rejected rather than emitting multiple JSON documents over its lifetime. Telegram persists only the Decision-to-message identity needed to validate and audit callbacks; callback data contains a compact Decision reference and action, never a secret or full plan.

### Telegram security

Telegram bot chats are **not end-to-end encrypted**. Never send tokens, credentials, environment files, private keys, or unredacted logs through the bot. RunTasks redacts configured private values and sensitive URL components from outbound notifications and reports integration failures without echoing Telegram responses.

Use only numeric user and chat IDs for authorization, keep the bot in a private DM for the initial deployment, and enable two-factor authentication on the operator's Telegram account. If the BotFather token appears in a terminal capture, log, chat, issue, or commit, rotate it immediately through `@BotFather`, update `~/runtasks/.env`, and restart the listener. Telegram credentials and IDs are loaded from private configuration and are never stored in SQLite.

## Testing safely

Behavior tests execute `bin/runtasks` in subprocesses with a temporary `RUNTASKS_HOME`. Telegram tests use recorded Bot API update fixtures and fake notification clients; they never contact Telegram or inspect the operator's real home:

```bash
python -m unittest discover -s tests -v
```

## License

[MIT](LICENSE)
