# RunTasks — Build Handoff for a New Pi Session

**Created:** 2026-08-20  
**Project directory:** `~/runtasks/`  
**Status:** Design agreed; implementation has not started. At handoff creation, `~/runtasks/` contained no implementation files other than this handoff.

## 1. Purpose of this handoff

Use this document as the primary context for a new Pi session that will build **RunTasks**.

RunTasks will turn tasks and operating policies discussed with a coding agent—or found in a document—into durable scheduled jobs. It will:

1. Extract a proposed task and its relevant operating policy.
2. Select sensible defaults for missing low-risk details.
3. Show the complete proposal and assumptions.
4. **Always ask the user whether to proceed before changing the task registry.**
5. Store tasks, runs, decisions, and searchable history in SQLite with FTS5.
6. Wake once daily to process due tasks.
7. Run safe checks automatically.
8. Use direct Telegram Bot API integration for human decisions.
9. Execute mutating operations only after explicit approval.
10. Log checks, decisions, updates, validation, and rollback.

The first real task/handler will implement the Pi MCP adapter update policy described in:

```text
~/vault247/13-technology/2026-08-20_pi-mcp-adapter-setup-version-pinning-maintenance.md
```

The new Pi session should read that source document completely before implementing the first handler.

---

## 2. Non-negotiable user requirements

### Naming and locations

- Agent Skill name: `runtasks`
- All operational application files and state must be under:

  ```text
  ~/runtasks/
  ```

- Files outside `~/runtasks/` must have names that clearly relate to RunTasks.
- `RUNTASKS_HOME` relocates configuration, databases, logs, backups, and other runtime-specific state. The sole exception is the non-secret, token-hash Telegram poller lock under the OS account's canonical `~/runtasks/var/data/` directory. That lock must remain account-global so two runtime homes cannot long-poll the same bot concurrently; tests inject a temporary global lock directory.
- Do not hardcode `/home/kai`; use:
  - `~/` in user-facing documentation and shell examples
  - `Path.home()` in Python for runtime-specific paths; the ADR-0001 Telegram coordination lock uses the OS account's canonical home so changing `HOME` cannot split the lock
  - `%h` in systemd unit files
- Proposed external systemd names:

  ```text
  ~/.config/systemd/user/runtasks-scheduler.service
  ~/.config/systemd/user/runtasks-scheduler.timer
  ~/.config/systemd/user/runtasks-telegram.service
  ```

### Scheduling

- Default timezone: `Asia/Singapore`
- Scheduled timer wake-up: once daily
- Recommended default time: `09:00 Asia/Singapore`
- The daily scheduler checks each task's `next_run_at`; a fortnightly task therefore runs every 14 days even though the scheduler wakes daily.
- Use `Persistent=true` so a missed daily wake runs after the machine starts again.
- An explicit Telegram approval may trigger an additional one-shot scheduler service run immediately. This is event-driven, not another recurring timer.

### Clarifications and confirmation

When extracting a task:

1. Choose the most sensible low-risk default for missing information.
2. Clearly display every assumption.
3. **Always ask the user whether to proceed.**
4. Do not add or update the task until the user confirms.

Suggested interactive confirmation:

```text
Proceed with adding/updating this task?

1. YES
2. NO
3. EDIT
```

The skill must not infer approval for destructive, costly, credential-related, externally visible, or otherwise high-risk actions.

### Telegram decisions

The user-facing Telegram controls must be simple:

```text
[1. APPROVE] [2. REJECT] [3. DETAILS]
```

Use Telegram inline keyboard buttons. The user should not need to type or copy a decision ID.

Internally, callback data must still carry a compact decision identifier and action so multiple pending decisions remain unambiguous. If text fallback is implemented, the user may reply `1`, `2`, or `3` to the exact decision message; use Telegram's reply/message identity to map it to the decision.

### Open-source requirement

The user intends to publish RunTasks in a public GitHub repository.

- No user-specific hardcoded paths.
- No real secrets, tokens, Telegram IDs, or private policy contents committed.
- Real secrets belong in `~/runtasks/.env`.
- Commit `.env.example`, never `.env`.
- Runtime database, logs, backups, and virtual environments must be ignored by Git.
- Keep the core architecture portable and avoid depending on OpenClaw.
- OpenClaw will be decommissioned; do not integrate with it.

---

## 3. Agent Skill availability across coding agents

The Agent Skills standard defines the skill package format, but coding-agent products do not all use the same global discovery directory.

| Agent | Global support for `~/.agents/skills/` | Notes |
|---|---:|---|
| Pi | Yes | Pi documents `~/.agents/skills/` as a global skill location. |
| Codex CLI | Yes | Official Codex documentation lists `$HOME/.agents/skills`. |
| OpenCode | Yes | Official OpenCode documentation lists `~/.agents/skills`. |
| Claude Code | No | Claude Code uses `~/.claude/skills/` for personal/global skills. |

Keep one canonical skill source in the repository:

```text
~/runtasks/skills/runtasks/SKILL.md
```

Create discovery links:

```bash
mkdir -p ~/.agents/skills ~/.claude/skills
ln -sfn ~/runtasks/skills/runtasks ~/.agents/skills/runtasks
ln -sfn ~/runtasks/skills/runtasks ~/.claude/skills/runtasks
```

This should make the same skill available to Pi, Codex, OpenCode, and Claude Code without duplicated copies. Claude Code and Codex explicitly document symlink support. The installer must validate actual discovery with every locally available agent and provide a fallback if a harness does not follow a symlink.

Relevant documentation:

- Pi: local `docs/skills.md`
- Claude Code: <https://code.claude.com/docs/en/skills>
- Codex: <https://developers.openai.com/codex/skills>
- OpenCode: <https://opencode.ai/docs/skills/>
- Agent Skills specification: <https://agentskills.io/specification>

Use only standard Agent Skills frontmatter where possible:

```yaml
---
name: runtasks
description: Extracts scheduled tasks and operating policies from documents, current conversations, selected text, or direct instructions; proposes sensible defaults; and manages the RunTasks registry after explicit user confirmation.
---
```

Do not rely on Claude-only frontmatter or proprietary session APIs in the shared skill.

---

## 4. Required architectural separation

The architecture must preserve these boundaries:

```text
Agent Skill
    Interprets the user's intent, session, or document
    Extracts task + policy
    Proposes assumptions
    Asks whether to proceed
            ↓
RunTasks CLI
    Validates structured input
    Reads/writes SQLite
    Enforces task and decision state transitions
            ↓
Daily scheduler
    Finds due tasks
    Claims work safely
    Runs checks and handlers
    Records results
            ↓
Named handlers
    Perform controlled, predefined operations
    Validate and roll back
            ↓
Telegram listener
    Sends decisions/results
    Receives button callbacks
    Records approval/rejection
```

### Skill responsibilities

The `runtasks` skill is still the **policy interpreter and task-registry manager**. It should:

- interpret relevant messages in the current coding-agent session;
- read a supplied policy document when one is supplied;
- accept direct task instructions without requiring a document;
- identify one or multiple scheduled tasks;
- extract schedule, conditions, check procedure, importance rules, approval policy, execution procedure, validation, rollback, and notifications;
- select and disclose sensible defaults;
- show a concise proposal;
- ask whether to proceed;
- call `~/runtasks/bin/runtasks` only after confirmation;
- list, inspect, search, update, enable, disable, or remove registered tasks through the CLI.

The skill must not write raw SQL.

### CLI responsibilities

`~/runtasks/bin/runtasks` is the stable interface between any coding agent and the RunTasks application. It should:

- validate data independently of the model;
- enforce allowed state transitions;
- prevent duplicate tasks and duplicate execution;
- write the SQLite database transactionally;
- expose simple human-readable commands and a structured JSON mode for skills/automation;
- never treat arbitrary policy prose as unattended shell code.

### Scheduler responsibilities

The skill is **not** the persistent scheduler. Agent Skills are on-demand instructions and do not remain alive after a session. The scheduler must be deterministic and independently runnable through systemd.

### Handler responsibilities

Mutating operations must use named, reviewed handlers such as:

```text
pi_mcp_adapter
```

Do not auto-execute arbitrary shell commands generated from policy prose. Unsupported policy actions should initially become manual notification tasks until a safe named handler exists.

Suggested action modes:

| Mode | Behaviour |
|---|---|
| `check` | Read-only work may run automatically. |
| `notify` | Tell the user that manual action is due. |
| `approved-procedure` | Run a named handler only after approval. |

---

## 5. Input sources and extraction workflow

A document is optional. The skill must support:

| Source | Example |
|---|---|
| Current conversation | “Use the runtasks skill to schedule what we just agreed on.” |
| Direct instruction | “Check package X every two weeks and tell me only about security releases.” |
| File path | A Markdown maintenance policy. |
| Selected/pasted text | A policy excerpt or terminal output. |
| Existing task | “Change the existing check from monthly to fortnightly.” |

For a session-derived task, store a concise source summary rather than requiring the entire proprietary session transcript. Suggested source metadata:

```text
source_type = session | document | direct | existing-task
source_ref  = optional path or portable reference
source_summary = concise policy summary accepted by the user
```

### Required proposal format

Before writing anything, the skill should show:

- task name;
- schedule and timezone;
- automatic check behaviour;
- conditions considered important;
- notification conditions;
- approval requirements;
- approved execution procedure;
- validation and rollback;
- assumptions/defaults;
- whether the proposal adds a task or updates an existing task.

Example:

```text
I extracted one proposed RunTasks task.

Task
Pi MCP adapter update check

Schedule
Every 14 days at 09:00 Asia/Singapore

Automatic action
1. Check the installed version.
2. Check the latest stable version.
3. If a new version exists, inspect intervening release notes.
4. Determine whether the update is important.

Notification
Notify through Telegram only when:
- the update is important;
- the assessment is uncertain; or
- the check fails.

Approved update
Install the exact approved version, restart Pi Web, validate the
adapter, and roll back automatically if validation fails.

Assumptions
- Non-important releases remain pinned.
- Missing release notes require human review.
- Mutating operations require explicit approval.

Proceed with adding this task?

1. YES
2. NO
3. EDIT
```

No registry change occurs before `YES`.

---

## 6. Repository and runtime layout

Recommended public repository layout:

```text
~/runtasks/
├── README.md
├── LICENSE
├── pyproject.toml
├── uv.lock
├── .env.example
├── .gitignore
│
├── bin/
│   └── runtasks
│
├── src/
│   └── runtasks/
│       ├── __init__.py
│       ├── cli.py
│       ├── config.py
│       ├── database.py
│       ├── migrations.py
│       ├── scheduler.py
│       ├── telegram.py
│       ├── evaluator.py
│       └── handlers/
│           ├── __init__.py
│           └── pi_mcp_adapter.py
│
├── skills/
│   └── runtasks/
│       └── SKILL.md
│
├── config/
│   └── runtasks.example.toml
│
├── deploy/
│   └── systemd/
│       ├── runtasks-scheduler.service
│       ├── runtasks-scheduler.timer
│       └── runtasks-telegram.service
│
├── tests/
│
└── var/
    ├── data/
    │   └── runtasks.sqlite3      # generated; ignored
    ├── logs/                     # generated; ignored
    └── backups/                  # generated; ignored
```

The authoritative operational database should be:

```text
~/runtasks/var/data/runtasks.sqlite3
```

Systemd's journal may be used as a technical fallback, but RunTasks' authoritative task/run/decision history must remain under `~/runtasks/`, primarily in SQLite.

### Path handling

- Python: `Path.home()` and configurable `RUNTASKS_HOME`.
- User documentation: `~/runtasks/...`.
- Systemd: `%h/runtasks/...` because systemd does not perform normal shell `~` expansion.

Example:

```ini
ExecStart=%h/runtasks/bin/runtasks run-due
```

---

## 7. SQLite and FTS5

SQLite is preferred over YAML for the registry. FTS5 is required for searching task policies and operational history.

The current machine was checked during design:

- Python SQLite: `3.45.1`
- FTS5: available
- Standalone `sqlite3` CLI: not installed, but not required for the application

Use normal indexed columns for scheduling and state. Use FTS5 only for text search.

### Keep the data model small

At minimum, provide three user-facing entities:

1. **Tasks** — what should run and when.
2. **Runs** — what happened each time.
3. **Decisions** — what awaits or received human approval.

A schema version/migration table and an FTS virtual table may exist as technical support tables.

Suggested minimum fields follow. The implementation may adjust names, but should keep the model understandable.

### `tasks`

- `id` — stable human-readable or UUID identifier
- `name`
- `description`
- `source_type`
- `source_ref`
- `source_summary`
- `schedule_type`
- `schedule_value`
- `timezone`
- `next_run_at`
- `handler`
- `policy_json`
- `status` — enabled/disabled
- `created_at`
- `updated_at`

### `runs`

- `id`
- `task_id`
- `trigger` — scheduled/manual/approval
- `status` — claimed/running/success/no-change/non-important/decision-required/failed/rolled-back
- `started_at`
- `finished_at`
- `summary`
- `details_json`
- `log_path`, if a large external log exists

### `decisions`

- `id`
- `task_id`
- `run_id`
- `status` — pending/approved/rejected/executing/completed/failed/expired
- `plan_json` — exact immutable approved plan
- `plan_hash`
- `reason`
- `telegram_chat_id`
- `telegram_message_id`
- `created_at`
- `responded_at`
- `responded_by_user_id`

### FTS5

Index searchable text such as:

- task name and description;
- accepted policy/source summary;
- run summaries;
- decision reasons;
- validation and rollback summaries.

Example user commands:

```bash
runtasks search "MCP security update"
runtasks search "failed validation"
runtasks search "OAuth compatibility"
```

### Reliability requirements

- Enable foreign keys.
- Use transactions for claims and state transitions.
- Use WAL mode if appropriate for the scheduler and Telegram listener sharing one database.
- Configure a busy timeout.
- Prevent overlapping execution of the same task.
- Make approval and execution idempotent.
- Repeated Telegram callbacks must not run an update twice.
- Store timestamps consistently; schedules are interpreted/displayed in `Asia/Singapore` by default.

---

## 8. CLI expectations

Suggested commands:

```bash
# General
runtasks status
runtasks init
runtasks install
runtasks uninstall

# Tasks
runtasks task list
runtasks task show <task-id>
runtasks task add --json <payload>
runtasks task update <task-id> --json <payload>
runtasks task enable <task-id>
runtasks task disable <task-id>
runtasks task remove <task-id>

# Scheduler and runs
runtasks run-due
runtasks run <task-id>
runtasks history
runtasks history <task-id>

# Decisions
runtasks decisions
runtasks decision show <decision-id>
runtasks decision approve <decision-id>
runtasks decision reject <decision-id>

# Search
runtasks search <query>

# Telegram
runtasks telegram setup
runtasks telegram test
runtasks telegram listen
```

The CLI should support:

- concise human output by default;
- `--json` structured output for Agent Skills and automation;
- nonzero exit codes on validation or execution failure;
- no secrets in output;
- no direct arbitrary SQL interface required for normal use.

---

## 9. Direct Telegram integration

OpenClaw must not be used. Integrate directly with the Telegram Bot API.

### Recommended initial deployment

- Private one-to-one chat with the bot.
- Long polling, not a webhook.
- No public inbound port, domain, TLS certificate, or reverse proxy.
- One always-running systemd user service:

  ```text
  ~/.config/systemd/user/runtasks-telegram.service
  ```

The Telegram listener is not the scheduler. It only receives decisions, sends messages, records callbacks, and triggers a one-shot approved run.

### Information required from the user

1. Telegram bot token created through official `@BotFather`.
2. Numeric Telegram user ID allowed to approve/reject.
3. Notification chat ID.
4. Optional forum topic/thread ID if group support is later enabled.

A separate bot ID is normally unnecessary because it is represented in the bot token.

Provide a setup command that obtains IDs from an official Bot API update after the user sends `/start`, avoiding third-party ID bots:

```bash
runtasks telegram setup
```

Ensure only one process polls the bot at a time.

### Telegram `.env` values

Commit this shape in `.env.example` without values:

```dotenv
# Telegram token received from official @BotFather
RUNTASKS_TELEGRAM_BOT_TOKEN=

# Comma-separated numeric Telegram user IDs allowed to make decisions
RUNTASKS_TELEGRAM_ALLOWED_USER_IDS=

# Private chat or group ID used for notifications
RUNTASKS_TELEGRAM_NOTIFICATION_CHAT_ID=

# Optional Telegram forum topic ID
RUNTASKS_TELEGRAM_THREAD_ID=
```

Protect the real file:

```bash
chmod 600 ~/runtasks/.env
```

### Telegram message and callbacks

Primary decision UI:

```text
[1. APPROVE] [2. REJECT] [3. DETAILS]
```

Requirements:

- `APPROVE` records approval for the exact immutable plan.
- `REJECT` closes the decision without execution.
- `DETAILS` sends expanded release notes, reasoning, exact procedure, validation, and rollback, then shows the same buttons again.
- Verify both sender user ID and chat ID.
- Do not authorize by username alone.
- Keep callback data compact; Telegram limits callback-data size.
- Do not include secrets in callback data.
- Repeated callbacks must be safe and idempotent.

### Telegram security

Telegram Bot API chats are not end-to-end encrypted. Therefore:

- never send credentials, API keys, `.env`, or unredacted sensitive logs;
- redact tokens from command output and database summaries;
- use numeric user-ID and chat-ID allowlists;
- enable two-factor authentication on the Telegram account;
- rotate the BotFather token immediately if exposed;
- never log the bot token;
- store only the Telegram metadata needed for audit and callback mapping.

Use a mature Telegram library rather than implementing the entire protocol. `python-telegram-bot` is a reasonable default, pinned to a reviewed version. Keep the notification interface internally abstract enough that another transport could be added later without rewriting scheduling or decisions.

---

## 10. Configuration and Git hygiene

Real secrets:

```text
~/runtasks/.env
```

Non-secret configuration:

```text
~/runtasks/config/runtasks.toml
```

Suggested configuration:

```toml
timezone = "Asia/Singapore"
daily_run_time = "09:00"
telegram_mode = "polling"
default_approval_required = true
```

Suggested `.gitignore` minimum:

```gitignore
.env
.venv/
__pycache__/
*.pyc

var/data/*
var/log/*
var/backups/*

!var/data/.gitkeep
!var/log/.gitkeep
!var/backups/.gitkeep
```

Do not put non-secret schedule/policy settings into `.env`. Do not commit real task databases, logs, Telegram IDs, tokens, policy excerpts containing private data, or generated backups.

For public portability:

- use `RUNTASKS_HOME` as an optional override;
- default it to `~/runtasks`;
- keep the core Python package independent from systemd;
- provide systemd as the initial Linux deployment adapter;
- macOS launchd and Windows Task Scheduler may be future additions, not MVP requirements;
- pin dependencies and commit a lock file;
- tests must use temporary databases and fake Telegram clients.

The license was not selected during the design discussion. Ask the user or propose a common permissive license such as MIT before publishing.

---

## 11. Daily scheduler and systemd

### Timer

Recommended timer behaviour:

```ini
[Unit]
Description=RunTasks daily scheduler timer for %h/runtasks

[Timer]
OnCalendar=*-*-* 09:00:00 Asia/Singapore
Persistent=true
Unit=runtasks-scheduler.service

[Install]
WantedBy=timers.target
```

Validate the exact calendar syntax on the target system with `systemd-analyze calendar` before installation.

### Scheduler service

The one-shot scheduler service should run something equivalent to:

```ini
[Unit]
Description=RunTasks scheduled-task runner for %h/runtasks

[Service]
Type=oneshot
WorkingDirectory=%h/runtasks
ExecStart=%h/runtasks/bin/runtasks run-due
```

Add sensible environment/config handling, timeouts, and hardening without making the MVP difficult to understand. Be careful that systemd `EnvironmentFile` syntax is not identical to every dotenv parser; using the application to load `~/runtasks/.env` is acceptable.

### Telegram service

The Telegram service should be a persistent long-poll listener:

```ini
[Unit]
Description=RunTasks Telegram decision listener for %h/runtasks

[Service]
Type=simple
WorkingDirectory=%h/runtasks
ExecStart=%h/runtasks/bin/runtasks telegram listen
Restart=on-failure
RestartSec=5
```

Do not make the Telegram listener execute mutation code directly. It records the decision and asks the one-shot runner to process it.

---

## 12. First handler: Pi MCP adapter update policy

Read the original policy note before implementation:

```text
~/vault247/13-technology/2026-08-20_pi-mcp-adapter-setup-version-pinning-maintenance.md
```

At the time of that policy review:

- `pi-mcp-adapter@2.26.1` was exactly pinned;
- the adapter installation and post-restart validation passed;
- checks were required every two weeks;
- important/security fixes could justify an earlier update;
- all updates should remain reviewed and exact-pinned;
- validation and rollback were required after changes.

Do not hardcode `2.26.1` as the installed version. Detect the actual installed version at runtime.

### Scheduled flow

Every 14 days at 09:00 `Asia/Singapore`:

```text
Read installed exact version
        ↓
Query latest stable version
        ↓
Same version?
  Yes → log successful no-change check; no Telegram
  No  → gather intervening release information
                ↓
         assess importance
                ↓
Important or uncertain?
  No  → log successful non-important check; remain pinned; no Telegram
  Yes → create immutable pending decision and notify Telegram
```

### Importance criteria

An update is important when it includes at least one relevant item:

1. Security fix affecting the installation.
2. Credential-handling or OAuth safety fix.
3. Compatibility fix required by the installed Pi version.
4. Fix for a currently broken active MCP server.
5. Protocol negotiation or connection fix affecting current operation.
6. Approval-gate or output-guard safety fix.
7. Serious defect likely to affect the current installation.

Routine features, documentation changes, refactoring, and irrelevant fixes are not important.

The evaluator must inspect all intervening releases, not only the newest release title. It must not classify importance solely from a version number.

Suggested structured assessment:

```json
{
  "important": true,
  "category": "security",
  "installed_version": "2.26.1",
  "available_version": "2.27.0",
  "reason": "Fixes credential handling affecting OAuth-backed MCP servers.",
  "recommendation": "Update after approval.",
  "confidence": "high"
}
```

If release notes cannot be obtained or the assessment is genuinely uncertain, fail safely and create a human decision. Do not silently call it non-important.

The semantic evaluator should be behind a configurable agent/LLM adapter rather than hardwired to one proprietary coding agent. The MVP may support Pi first, but keep a stable evaluator interface so Claude Code, Codex, OpenCode, or a direct model API can be added without changing the scheduler/database model.

### Sources/checks

The original policy used:

```bash
npm view pi-mcp-adapter version
```

Installed version can be read safely from the package JSON under the user's home directory. Use Python path handling rather than embedding a literal user home path.

Release sources include:

- <https://github.com/nicobailon/pi-mcp-adapter/releases>
- <https://github.com/nicobailon/pi-mcp-adapter/blob/main/CHANGELOG.md>

Pin and review every external dependency used for fetching or parsing release information.

### Immutable decision plan

When an important update is found, store an immutable plan containing:

- task and run IDs;
- detected installed version;
- exact proposed version;
- assessment category/reason/confidence;
- source release references;
- named handler `pi_mcp_adapter`;
- exact validation procedure;
- exact rollback version;
- plan hash.

Approval authorizes only this plan. Before executing, verify that the actual installed version still equals the old version recorded in the plan. If state changed, do not proceed; create a new decision.

### Approved update procedure

For an approved target version:

1. Reconfirm current installed version.
2. Install the exact approved version:

   ```text
   pi install npm:pi-mcp-adapter@<approved-version>
   ```

3. Verify the installed package reports the approved version.
4. Restart:

   ```text
   systemctl --user restart pi-web.service
   ```

5. Confirm Pi Web is healthy.
6. Run the MCP validation equivalent to:

   ```bash
   pi --no-session --tools mcp -p \
     'Call the mcp tool with an empty object. If successful, reply exactly MCP_ADAPTER_OK.'
   ```

7. Require the exact expected result:

   ```text
   MCP_ADAPTER_OK
   ```

8. Record success and notify Telegram.
9. Tell the user that already-open terminal Pi sessions may need to be closed and reopened.

### Rollback

Approval of the update also authorizes rollback to the exact previous version if validation fails.

On failure:

1. Reinstall the exact old version.
2. Restart Pi Web.
3. Validate the rollback.
4. Record both the failed update and rollback outcome.
5. Send an urgent Telegram result.

Example:

```text
RunTasks update failed and was rolled back

Task: Pi MCP adapter update
Attempted: 2.26.1 → 2.27.0
Failure: MCP validation did not return MCP_ADAPTER_OK
Rollback: 2.26.1 restored successfully
Pi Web: Healthy
```

A failed rollback is a critical notification and must not be hidden.

### Non-important releases

Per the user's simplified desired behaviour:

- log the completed assessment;
- keep the installed package pinned;
- do not send Telegram;
- do not automatically update.

This differs from a general “monthly stable update” policy because non-important releases may remain unapplied indefinitely. That is an accepted MVP behaviour. Do not add a monthly digest unless the user asks later.

---

## 13. Telegram messages for the first handler

### Decision message

```text
RunTasks needs your decision

Pi MCP adapter important update

Installed: 2.26.1
Proposed: 2.27.0

Reason:
Fixes an OAuth credential-handling issue that may affect the current installation.

Proposed operation:
- Install exact version 2.27.0
- Restart Pi Web
- Validate MCP_ADAPTER_OK
- Roll back to 2.26.1 if validation fails
```

Buttons:

```text
[1. APPROVE] [2. REJECT] [3. DETAILS]
```

### Success message

```text
RunTasks update completed successfully

Task: Pi MCP adapter update
Updated: 2.26.1 → 2.27.0
Pi Web: Healthy
Validation: MCP_ADAPTER_OK
Rollback: Not required

Open a fresh terminal Pi session to ensure it loads the new version.
```

### Failure/rollback message

Include:

- attempted versions;
- failing step;
- rollback attempt and result;
- Pi Web status;
- run reference suitable for `runtasks history`;
- no secrets or unredacted large logs.

---

## 14. Security and safety requirements

- Agent Skills and handlers may execute with user permissions; review them carefully.
- Never convert arbitrary untrusted policy prose directly into unattended shell commands.
- Read-only checks may be automatic.
- Mutating, destructive, costly, credential-related, or externally visible actions require approval unless they are rollback steps already included in the exact approved plan.
- Keep exact package pins. Never use `@latest` for a persistent executable dependency.
- Validate actual state immediately before applying an approved plan.
- Use transactions and idempotency to prevent duplicate execution.
- Do not store or print Telegram bot tokens.
- Redact credentials from logs and notifications.
- Do not send secrets through Telegram.
- Use numeric Telegram user IDs and chat IDs, not usernames, for authorization.
- Restrict `.env`, SQLite, logs, and backups to the user.
- Use temporary test directories and fake external commands for automated tests.
- Never let tests restart the real Pi Web service or install real packages.

---

## 15. Existing projects reviewed

No exact drop-in project was found that combines cross-agent policy extraction, SQLite FTS5, daily execution, semantic importance assessment, Telegram approvals, and deterministic update/rollback.

Relevant projects to review for ideas rather than blindly adopt:

### Claude Code Scheduler

<https://github.com/jshchnz/claude-code-scheduler>

- Natural-language one-time and recurring scheduling.
- Native OS schedulers and logs.
- Claude-specific, JSON registry, no Telegram human-decision workflow, and no policy/rollback model.

### Ductor

<https://github.com/PleasePrompto/ductor>

- Direct Telegram control for Claude Code, Codex CLI, Gemini CLI, and others.
- Includes cron jobs.
- Broader agent gateway, closer to an OpenClaw replacement than a small RunTasks transport.
- Worth studying for Telegram and multi-agent transport, but likely too large to use solely for approvals.

### Instar

<https://github.com/JKHeadley/instar>

- Telegram, persistent Claude agents, scheduling, SQLite, and FTS5.
- Architecturally close in some areas but is a larger personal-agent framework and primarily Claude-centred.

### MCP Cron

<https://github.com/jolks/mcp-cron>

- Cron scheduling, shell/API/AI tasks, SQLite, multi-instance safety.
- No direct policy-extraction skill or Telegram decision workflow.
- Adds an MCP scheduler layer when one daily systemd timer is sufficient.

### MCP Scheduler

<https://github.com/PhialsBasement/scheduler-mcp>

- MCP-managed scheduled tasks and SQLite history.
- Missing the cross-agent policy extraction and Telegram approval model.

### General workflow systems

Dagu, Windmill, Kestra, and n8n provide scheduling, retries, logging, notifications, and sometimes approvals, but are much larger than the desired solution.

Conclusion: build a thin RunTasks-specific layer using existing standards and libraries rather than adopting a broad workflow/agent platform.

Reuse rather than recreate:

- Agent Skills standard;
- Python SQLite and FTS5;
- systemd timers/services;
- a mature Telegram Bot API library;
- Python `zoneinfo`;
- named handlers and transactional state machines.

---

## 16. Recommended implementation sequence

Keep the initial build small and testable.

### Phase 1 — Repository, configuration, database, and CLI

- Create public-ready project structure.
- Add `pyproject.toml`, lock file, `.gitignore`, `.env.example`, license placeholder, and README.
- Implement configuration and path resolution.
- Implement SQLite initialization, schema versioning, FTS5, and transactions.
- Implement task/run/decision CLI operations and JSON mode.
- Add unit tests with temporary databases.

### Phase 2 — Global `runtasks` Agent Skill

- Write standards-compatible `SKILL.md`.
- Support session, document, pasted text, and direct instructions.
- Require proposal + assumptions + confirmation before writing.
- Use only the CLI for registry changes.
- Add installer logic for `~/.agents/skills/runtasks` and `~/.claude/skills/runtasks` links.
- Validate visibility in installed agents.

### Phase 3 — Daily scheduler

- Implement due-task calculation and safe claiming.
- Implement manual and daily triggers.
- Create and install `runtasks-scheduler.service` and `.timer`.
- Validate `Asia/Singapore`, daily 09:00, and `Persistent=true` behaviour.
- Add overlap/idempotency tests.

### Phase 4 — Direct Telegram

- Add long-polling bot service with a mature pinned library.
- Add `telegram setup`, `telegram test`, and `telegram listen`.
- Enforce allowed numeric user IDs and chat IDs.
- Add inline `APPROVE`, `REJECT`, and `DETAILS` buttons.
- Record immutable decisions transactionally.
- Trigger the one-shot runner after approval.
- Use a fake Telegram client in tests.

### Phase 5 — Pi MCP adapter handler

- Re-read the source policy.
- Implement installed/latest version checks.
- Gather release/changelog information.
- Add structured importance evaluation with fail-safe uncertainty handling.
- Log no-change and non-important checks without Telegram.
- Create Telegram decisions for important/uncertain cases.
- Implement exact approved update, Pi Web restart, validation, automatic rollback, and notifications.
- Mock all package/service commands in tests.

### Phase 6 — Documentation and hardening

- Installation/uninstallation instructions.
- Threat model and security notes.
- Backup/restore for SQLite.
- Secret rotation instructions.
- Systemd status and troubleshooting commands.
- Public-repository secret scan and CI.
- End-to-end smoke tests in a safe, non-production fixture environment.

---

## 17. Acceptance criteria

The first usable release is complete when all of the following pass.

### Skill and portability

- [ ] One canonical `runtasks` skill exists in `~/runtasks/skills/runtasks/`.
- [ ] Pi discovers it globally.
- [ ] Codex discovers it globally.
- [ ] Claude Code discovers it globally through its linked location.
- [ ] OpenCode discovery is documented/tested if OpenCode is installed.
- [ ] The skill can extract a task from the current conversation without a document.
- [ ] The skill can extract from a file path.
- [ ] The skill shows sensible assumptions and always asks before saving.

### Registry and search

- [ ] SQLite database is under `~/runtasks/var/data/`.
- [ ] FTS5 search works for tasks, runs, and decisions.
- [ ] No YAML task registry is required.
- [ ] CLI can list, show, enable, disable, update, search, and inspect history.
- [ ] Repeated task proposals do not silently create duplicates.

### Scheduler

- [ ] One recurring systemd timer wakes daily at 09:00 `Asia/Singapore`.
- [ ] Missed runs catch up after restart.
- [ ] A 14-day task executes only when due.
- [ ] Concurrent scheduler invocations cannot execute the same task twice.

### Telegram

- [ ] No OpenClaw dependency exists.
- [ ] Direct long polling works without an inbound public port.
- [ ] Unauthorized users/chats cannot make decisions.
- [ ] The user sees only `1. APPROVE`, `2. REJECT`, and `3. DETAILS` buttons.
- [ ] Internal decision IDs remain correctly mapped.
- [ ] Repeated button presses do not duplicate execution.
- [ ] Secrets are not logged or sent.

### Pi MCP example

- [ ] No update: successful check logged; no Telegram.
- [ ] New non-important update: assessment logged; remains pinned; no Telegram.
- [ ] Important update: immutable Telegram decision created.
- [ ] Uncertain assessment: human decision requested.
- [ ] Approval: exact approved version installed once.
- [ ] Rejection: no mutation occurs.
- [ ] Validation success: success logged and Telegram notification sent.
- [ ] Validation failure: exact old version rollback attempted and logged.
- [ ] Rollback result always reported.
- [ ] Actual installed version is detected at runtime, not hardcoded.

### Public repository

- [ ] No `/home/kai` hardcoding exists.
- [ ] `.env` is ignored and `.env.example` contains placeholders only.
- [ ] Database/logs/backups are ignored.
- [ ] Systemd files use `%h` and `runtasks-` naming.
- [ ] Tests do not modify the real system.
- [ ] A secret scan passes before publishing.

---

## 18. Decisions still requiring confirmation during the build

The following were not finally selected in this discussion. The new Pi session should present a sensible recommendation and ask before committing to them:

1. **License:** recommended default is MIT, but confirm with the user.
2. **Python dependency workflow:** `uv` with `pyproject.toml` and `uv.lock` is recommended, but confirm if necessary.
3. **Telegram library:** `python-telegram-bot` is recommended and should be exactly pinned after review.
4. **Telegram chat mode:** private DM is the recommended MVP; group/topic support can follow later.
5. **Semantic evaluator backend:** keep an adapter interface; recommend Pi as the first local backend because this project is being built in Pi, but avoid locking the database/scheduler to Pi.
6. **Database backup retention:** propose a simple retention policy before enabling automated backups.
7. **Repository owner/name and remote:** not specified.

These build-time choices are distinct from the RunTasks skill's own rule that every extracted task proposal must ask the user whether to proceed.

---

## 19. Guidance to the new Pi session

1. Read this handoff completely.
2. Read the original Pi MCP adapter policy completely.
3. Read Pi's current Agent Skills documentation before implementing the skill.
4. Inspect the empty/current state of `~/runtasks/`.
5. Present a concise implementation plan and any recommended defaults that still need confirmation.
6. Keep the first implementation minimal; do not introduce a web UI, large workflow engine, MCP scheduler, or OpenClaw dependency.
7. Implement in phases with tests after each phase.
8. Never run real Pi package updates, service restarts, or Telegram sends in automated tests.
9. Show all created paths clearly.
10. Validate the systemd calendar, global skill discovery, SQLite FTS5 availability, and Telegram authorization before calling the build complete.
