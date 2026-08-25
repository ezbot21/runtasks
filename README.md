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
uv run ruff check .
uv run mypy
uv run python -m runtasks.release_checks
uv run python -m unittest discover -s tests -v
```

All project, runtime, and development dependencies are exactly resolved in `uv.lock`. The pinned `tzdata` runtime dependency keeps IANA timezone validation portable on systems without an operating-system timezone database.

## Documentation

- [Operator guide](docs/operator-guide.md): install, uninstall, configuration, Agent Skill discovery, scheduler and Telegram operation, backup/restore, token rotation, and troubleshooting.
- [Security model](docs/security.md): secret handling, redaction guarantees, approval boundaries, threat boundaries, and critical recovery.
- [Pi MCP adapter handler](docs/pi-mcp-handler.md): 14-day policy, importance and uncertainty, exact approval, update validation, rollback, and quiet non-important behavior.
- [Public-release verification](docs/release-verification.md): clean-checkout gates, FTS5 and systemd checks, fake Telegram authorization, and complete fake update/rollback proof.

## Agent Skill

The canonical standards-compatible Agent Skill is available directly from the repository at [`skills/runtasks/SKILL.md`](skills/runtasks/SKILL.md). It extracts one or more Task proposals from conversation context, documents, pasted text, direct instructions, or existing Tasks. Each proposal is staged as a hash-checked review and shows its full policy and assumptions before asking for `YES`, `NO`, or `EDIT`; only an explicit `YES` can invoke `task add` or `task update` with the exact reviewed payload.

`runtasks install` exposes this one canonical source through both `~/.agents/skills/runtasks` (Pi, Codex, and OpenCode) and `~/.claude/skills/runtasks` (Claude Code). The installer launches every supported agent executable found on the local `PATH` in an isolated temporary home and verifies that the agent actually discovers the skill. It uses symlinks when supported and replaces a managed discovery link with a documented managed-copy fallback when an installed harness does not follow symlinks.

## User installation

On Linux systems with a systemd user manager, install the runtime, persistent scheduler, Telegram listener, and cross-agent skill discovery with:

```bash
bin/runtasks install
bin/runtasks install --json
```

Installation is idempotent. Before enabling anything it asks the installed `systemd-analyze` to validate the exact calendar expression `*-*-* 09:00:00 Asia/Singapore`. It then manages only these clearly named user units under `~/.config/systemd/user/`:

```text
runtasks-scheduler.service  one-shot `runtasks run-due` runner
runtasks-scheduler.timer    persistent daily 09:00 Asia/Singapore wake
runtasks-telegram.service   long-poll listener with Restart=on-failure
```

Generated units use systemd `%h` expansion for the application executable, runtime working directory, and user-owned executable directories captured from the validated installer `PATH`; no username or literal home path is embedded. The Telegram listener records approvals and requests the separate `runtasks-scheduler.service`; it never executes approved mutations directly.

The application and `RUNTASKS_HOME` must both be under the current user's home so the generated units can remain portable. Missing systemd tooling, an unavailable systemd user manager, an invalid calendar expression, an unavailable canonical skill, an unmanaged conflicting unit/link, or failed installed-agent discovery is reported as a validation error before the timer or listener is enabled.

Remove only managed units and discovery entries with:

```bash
bin/runtasks uninstall
```

By default uninstallation preserves `config/`, `.env`, the SQLite database, logs, and backups. Remove those runtime data paths only with the explicit destructive option:

```bash
bin/runtasks uninstall --remove-data
```

Source code, the canonical skill, unrelated user units or skill directories, and account-global Telegram poller lock files are never removed by `--remove-data`.

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
`check` or `approved-procedure` Tasks. Task registration never interprets policy prose
as a command; only the registered handler's reviewed implementation can execute.

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
systemd: any daily wake mechanism or approval-triggered one-shot service may invoke it.
The command first claims approved execution Runs, then takes one scheduler current time,
selects enabled Tasks with `next_run_at` at or before that time, and claims each due
occurrence in SQLite before invoking its named handler. Claims commit before external
work begins. Unique scheduled-occurrence and approval-Run state constraints prevent
competing processes from executing the same work twice.

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
  This check phase never runs `pi install`, restarts a service, or changes the exact
  package pin.
- A separately claimed `approval` Run for the same handler reloads the immutable plan,
  verifies its SHA-256 hash and registered Task, reconfirms the exact old installed
  version, installs only `npm:pi-mcp-adapter@<approved-version>`, and verifies package
  metadata before restarting `pi-web.service`. It then requires an unambiguous healthy
  user-service state and exact `MCP_ADAPTER_OK` output from a fresh Pi MCP validation
  process. Only after every check succeeds does it record success, complete the
  Decision, and send the redacted operator notification.

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
supplies real read-only release assessments and immutable exact-version update plans.
`run-due` executes a claimed approved plan at most once, records ordered redacted step
outcomes, and exposes a successful Decision as `completed`. A stale old-version
precondition supersedes the approved Decision before mutation and makes the Task due
immediately so the normal read-only check creates a fresh assessment and Decision path.
Failures before package mutation do not roll back. Failures after mutation reinstall the
exact authorized old pin, restart and health-check Pi Web, and repeat the fresh
`MCP_ADAPTER_OK` validation. Verified recovery is recorded as `rolled-back`; failed or
ambiguous recovery is a distinct `rollback-failed` Decision outcome. Both recovery
outcomes send durable urgent redacted notifications, and interrupted mutation or
rollback phases are reconciled without repeating package installation.

Initialization creates:

```text
~/runtasks/config/runtasks.toml
~/runtasks/var/data/runtasks.sqlite3
~/runtasks/var/logs/
~/runtasks/var/backups/
```

The command is idempotent. SQLite foreign keys and a five-second busy timeout are enabled on every application connection. WAL mode is requested where the SQLite build supports it, and initialization fails safely if FTS5 is unavailable. When an existing database needs migration or a journal-mode change, `init` first creates and verifies an online SQLite backup; a backup failure aborts initialization before the database is modified.

## Backup and restore

Create an online backup while RunTasks remains available for normal reads and writes:

```bash
bin/runtasks backup
bin/runtasks backup --json
```

Backups use SQLite's online backup API and are written privately under
`<RUNTASKS_HOME>/var/backups/`. Each artifact has a matching JSON metadata file;
keep the `.sqlite3` and `.json` files together when moving a backup. Their names and
metadata identify the UTC creation time, source schema version, and SHA-256 artifact
checksum without copying Task policy text into metadata. Every backup is checked for SQLite integrity, foreign-key
consistency, supported schema, and the FTS tables expected by that schema before it is
published. Retention runs after every successful backup and keeps at most 14 verified
daily artifacts, using the backup most recently created by the operation as that UTC
day's snapshot. Unknown, malformed, orphaned, corrupt, or unverifiable files are left
untouched rather than deleted speculatively; only verified backups safely outside the
daily retention set are removed.

Restore always stages a fresh SQLite database, validates the backed-up schema, applies
supported migrations only to that staging database, enables WAL, and then revalidates
integrity, foreign keys, the current schema, and FTS before changing live state. Merely
naming a backup is not sufficient: replacing the live registry requires the explicit
`--replace-live` operator action.

```bash
# Stop RunTasks listeners and schedulers first.
RUNTASKS_HOME=~/runtasks-restored \
  bin/runtasks restore /safe/path/runtasks-backup-v6-20260901T010000.000000Z.sqlite3 \
  --replace-live

RUNTASKS_HOME=~/runtasks-restored \
  bin/runtasks restore /safe/path/runtasks-backup-v6-20260901T010000.000000Z.sqlite3 \
  --replace-live --json
```

A fresh `RUNTASKS_HOME` receives the normal private runtime layout and default
configuration. When replacing an existing live database, restore acquires exclusive
RunTasks database access and refuses to proceed while another RunTasks connection is
open. It then creates a verified safety backup of the locked live state. Any source
validation, staging, lock, safety backup, checkpoint, or destination failure returns
nonzero and leaves the live database in place. After success, use `status`, `task list`,
`history`, `decisions`, and `search` to inspect the restored user-visible state.

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

Use only numeric user and chat IDs for authorization, keep the bot in a private DM for the initial deployment, and enable two-factor authentication on the operator's Telegram account. If the BotFather token appears in a terminal capture, log, chat, issue, or commit, rotate it immediately through `@BotFather`, update `~/runtasks/.env`, and restart the listener. The bot token and configured authorization allowlists are not copied into SQLite. Decision audit records necessarily retain the numeric responding user identity plus the mapped chat and message IDs.

## Testing safely

Behavior tests execute `bin/runtasks` in subprocesses with a temporary `RUNTASKS_HOME`. Telegram tests use recorded Bot API update fixtures and fake notification clients; they never contact Telegram or inspect the operator's real home:

```bash
uv run python -m unittest discover -s tests -v
uv run python -m unittest tests.test_end_to_end_release -v
```

The end-to-end release suite advances an explicit fake clock and exercises fake release discovery, Telegram approval, exact update, success validation, exact rollback, critical failed rollback, and FTS history search. It never calls real Pi, systemd, npm, GitHub, package installation, networks, or Telegram.

## License

[MIT](LICENSE)
