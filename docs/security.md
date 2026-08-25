# RunTasks security model

RunTasks is designed for one trusted OS user operating named reviewed handlers. It reduces accidental and remote authorization risk; it is not a sandbox for hostile local code.

## Secrets

Store secrets only in `<RUNTASKS_HOME>/.env` with owner-only permissions. The public `.env.example` contains names and empty placeholders. Non-secret settings belong in TOML.

Never commit or send:

- Telegram bot tokens;
- numeric production user, chat, or thread IDs;
- private environment files;
- API keys, bearer tokens, private keys, credentialed URLs, or JWTs;
- unredacted subprocess output or private policy transcripts.

RunTasks loads `.env` separately, lets matching process environment variables override it, and does not persist the Telegram bot token or configured authorization allowlists in SQLite. Decision audit records necessarily retain the numeric responding user identity plus mapped chat and message IDs. Rotate a bot token immediately after suspected exposure and restart the listener.

Before publishing, run:

```bash
uv run python -m runtasks.release_checks
```

The check inspects Git-tracked files and rejects private environment files, high-confidence credential patterns, numeric Telegram IDs in environment assignments, machine-specific home paths, absolute symlinks, local path dependencies, and committed runtime databases, logs, locks, or backups. Synthetic credential strings are permitted only on explicitly marked test-fixture lines, where redaction behavior is verified without using real credentials.

## Redaction guarantees

Redaction is applied at CLI, storage, handler, notification, and logging boundaries. It recognizes configured secret values and representative bearer-token, API-key, credentialed-URL, private-key, GitHub, AWS, Slack, Telegram, and JWT forms. Sensitive structured keys are replaced rather than merely obscured in display formatting.

RunTasks promises that known configured secret values and recognized secret patterns are removed from normal CLI output, persisted Run and Decision summaries/details, application log records, and Telegram text. The tests include subprocess exceptions, URLs, environment values, execution failures, update recovery, and notification delivery.

Redaction is defense in depth, not permission to ingest arbitrary secret material. Unknown encodings, encrypted blobs, images, novel token formats, or data deliberately split across fields may not be recognizable. Keep raw credentials and private logs outside Task policy, release evidence, and notifications.

## Approval and execution boundaries

Read-only checks may run automatically. Mutations require:

1. a registered named handler;
2. a pending immutable Decision;
3. an exact canonical plan and SHA-256 plan hash;
4. explicit approval through the CLI or an authorized Telegram callback;
5. revalidation of installed state immediately before mutation.

Approval authorizes only the stored target and rollback version. It never means "install latest" and never authorizes a changed plan. Policy prose remains data and cannot become an arbitrary shell task. The first release includes only the reviewed Pi MCP adapter mutation procedure.

Telegram callbacks contain a compact Decision reference and action, not a plan or secret. The server reloads the immutable plan from SQLite. Both numeric sender user ID and chat ID are checked. The long-poll listener commits the response and requests a separate runner; it does not execute mutation logic.

## Threat boundaries

RunTasks protects against:

- accidental duplicate scheduling and repeated approvals;
- unauthorized Telegram users or chats;
- stale exact-version approvals;
- floating package targets;
- common credential disclosure through output;
- competing bot pollers under one OS account;
- partial database migration or restore replacement;
- unsafe automatic interpretation of policy prose;
- post-install validation failures through exact rollback.

RunTasks does **not** protect against:

- a compromised OS account, Python environment, Pi installation, package registry, GitHub account, Telegram account, or Telegram infrastructure;
- malicious code already able to edit the RunTasks database, source, executable environment, systemd units, or `.env`;
- an evaluator or release source intentionally supplying plausible but false public evidence;
- confidentiality of Telegram transport as if it were end-to-end encrypted;
- arbitrary multi-user tenancy or role-based authorization;
- package compromise that executes during an operator-approved exact installation.

Use OS account isolation, filesystem permissions, Telegram account two-factor authentication, trusted DNS/networking, reviewed lockfile changes, and off-machine backups. Treat Pi MCP packages as executable supply-chain inputs.

## Recovery and critical outcomes

Pre-mutation failures perform no rollback because no approved external change occurred. After target installation starts, validation failure authorizes rollback only to the exact old version recorded in the plan. Recovery reinstalls that pin, restarts Pi Web, checks health, and requires an exact result from a fresh MCP-only Pi process.

A verified rollback is urgent but recovered. A failed or ambiguous rollback is critical and remains `rollback-failed`; it cannot be flattened into a generic success. Interrupted update and rollback phases are checkpointed so restart can reconcile observed package state without blindly reinstalling.

## Reporting a vulnerability

Do not place a live credential or sensitive private policy in a public issue. Revoke exposed credentials first. Report the smallest redacted reproduction that demonstrates the boundary failure, including the RunTasks version, OS/Python/SQLite versions, command, expected behavior, and redacted output.
