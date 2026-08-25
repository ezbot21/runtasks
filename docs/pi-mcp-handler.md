# Pi MCP adapter handler

`pi_mcp_adapter` is the first production RunTasks handler. It implements a conservative 14-day release-review policy for the exact installed `pi-mcp-adapter` pin.

## Schedule and quiet policy

The recommended Task is due every 14 local-calendar days at 09:00 Asia/Singapore. The system scheduler may wake daily, but the Task runs only when its own `next_run_at` is due.

The check detects the installed version at runtime. It does not contain an assumed current pin. If the installed and latest stable versions match, the Run records `no-change` and sends no Telegram message. If all intervening releases are confidently non-important, the Run records `non-important`, preserves the old pin indefinitely, and sends no message. The first release has no digest for non-important updates.

## Evidence collection

The read-only check:

1. reads package metadata below the configured Pi agent directory;
2. reads Pi's configured npm command;
3. queries npm's stable `latest` dist-tag;
4. gathers every intervening stable version;
5. collects normalized GitHub Release and changelog evidence for each version;
6. submits only normalized evidence to an ephemeral, tool-disabled Pi evaluator.

A semantic-version shape is never importance evidence by itself. A patch can be important and a major release can be irrelevant.

## Importance categories

An update is important when available evidence indicates a relevant:

- security fix;
- credential-handling or OAuth safety fix;
- compatibility fix required by the installed Pi version;
- fix for an active broken MCP server;
- protocol negotiation or connection fix affecting operation;
- approval-gate safety fix;
- output-guard or context-protection safety fix;
- serious defect likely to affect the installation.

Routine features, documentation, refactoring, and irrelevant fixes are non-important when evidence is complete and confidence is high.

## Uncertainty

Uncertainty fails safe to a human Decision. Missing notes, incomplete intervening releases, unavailable sources, malformed package metadata, parser errors, evaluator errors, malformed evaluator output, timeout, low confidence, or genuine ambiguity cannot produce a quiet non-important result.

When installed and target versions are valid, important and uncertain assessments create an immutable plan containing:

- Task and Run identity;
- exact old and target versions;
- importance, category, reason, recommendation, and confidence;
- redacted release evidence and references;
- named handler and exact operation;
- package spec pinned as `npm:pi-mcp-adapter@<version>`;
- Pi Web restart and health expectations;
- exact `MCP_ADAPTER_OK` validation expectation;
- exact rollback version;
- SHA-256 hash of strict canonical plan JSON.

## Approval and exact pinning

CLI and Telegram approval authorize only that plan hash. Immediately before mutation, the runner re-reads installed package metadata. If it no longer equals the plan's old version, no install or restart occurs. The stale Decision is superseded and the Task is made due for a fresh check.

The runner never substitutes a newer registry result for the approved target. Package installation uses one exact package spec. Repeated or concurrent approval processing cannot install the target twice.

## Approved update protocol

The observable ordered protocol is:

1. confirm the installed version is still the exact old version;
2. install the exact approved target;
3. verify package metadata reports the target;
4. restart `pi-web.service`;
5. require unambiguous active service health;
6. start a fresh Pi process restricted to the MCP tool;
7. require stdout to be exactly `MCP_ADAPTER_OK` with no surrounding output;
8. durably record completion;
9. send a redacted success message reminding the operator to reopen existing terminal Pi sessions.

Failure before target installation performs no rollback. Failure after mutation enters exact rollback.

## Rollback protocol

Approval also authorizes this bounded recovery:

1. install the exact old pin from the immutable plan;
2. verify package metadata reports that old version;
3. restart `pi-web.service`;
4. require unambiguous active health;
5. run the same fresh MCP-only Pi validation;
6. require exact `MCP_ADAPTER_OK`;
7. record both update failure and rollback result;
8. send an urgent redacted notification.

Verified recovery produces `rolled-back`. Any failed or ambiguous rollback step produces `rollback-failed`, a critical Decision and Run outcome. It remains searchable and is always announced. A process interruption during update or rollback is reconciled from durable phase checkpoints and observed package state; RunTasks does not blindly repeat installation.

## Safety limits

The handler does not:

- install non-important releases;
- manage unrelated MCP packages;
- accept a floating `latest` installation target;
- run policy prose as shell code;
- mutate during the read-only check;
- notify about no-change or non-important results;
- run inside the Telegram callback;
- depend on OpenClaw, a web UI, or an MCP scheduler.

Automated tests replace npm, GitHub, Pi, systemd, package installation, service health, and Telegram delivery with deterministic fakes. The public end-to-end suite proves success, exact rollback, and critical failed-rollback reporting without touching real external systems.
