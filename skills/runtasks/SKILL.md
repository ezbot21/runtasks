---
name: runtasks
description: Extracts scheduled Tasks and operating policy from the current conversation, documents, pasted or selected text, direct instructions, or existing Tasks; presents complete add or update proposals; and changes the RunTasks registry only after explicit YES confirmation. Use when a user wants an agreement or policy turned into a durable reviewed Task.
---

# RunTasks

Turn policy prose into reviewed structured Task proposals. The registry boundary is the RunTasks CLI; never write SQL.

## Locate RunTasks

Resolve the real path of this skill directory, including through a discovery symlink. Its repository root is `../..`, and the stable CLI is `<repository-root>/bin/runtasks`. The canonical skill therefore works directly from the repository before global discovery installation exists.

Use `scripts/confirm.py` from this skill directory to stage, apply, or discard proposals. Staging validates the full payload with the application's Task parser, writes a hash-checked temporary review file, and does not invoke the Task CLI.

## 1. Establish the source

Use the source the user supplied:

- **current conversation**: use the relevant visible messages; set `source_type` to `session`, `source_ref` to `null`, and do not require a product-specific session API or complete transcript.
- **document**: read the supplied file with the available file-reading tools; set `source_type` to `document` and `source_ref` to a portable path or document reference.
- **pasted or selected text**: use the supplied text; set `source_type` to `direct`, `source_ref` to `null`, and identify it as pasted or selected text in the summary.
- **direct instruction**: use the instruction; set `source_type` to `direct` and `source_ref` to `null`.
- **existing Task**: inspect it with `task show <task-id> --json`; set `source_type` to `existing-task` and `source_ref` to its Task ID in the reviewed update.

Write a concise source_summary of the accepted policy, normally one to three sentences. Preserve decisions and relevant constraints, not full conversations or documents.

## 2. Inspect before proposing

Run the non-mutating command:

```bash
<repository-root>/bin/runtasks task list --json
```

Compare the candidate name, source reference, schedule, handler, and policy with registered Tasks. Inspect likely matches with `task show <task-id> --json`.

- A changed policy for the same identity is an **update**, not another add.
- A semantically equivalent existing Task is a duplicate; propose an update only when the reviewed content would actually change it.
- Copy all unchanged fields from an inspected existing Task into an update proposal so the user reviews the complete resulting Task.
- Never use `task add` as a pre-confirmation duplicate probe.

## 3. Extract independent Tasks

Create one proposal for each independent schedule and operating policy. Different steps in one check, execution, validation, or rollback procedure remain one Task. Different cadences or independently manageable outcomes become separate Tasks.

When a source contains multiple Tasks, state the count, then review and confirm each proposal independently. A response for one Task never confirms another.

Each full Task payload must contain:

- `name`, `description`, `source_type`, `source_ref`, and concise `source_summary`;
- a supported `schedule`, `timezone`, and matching `next_run_at`;
- `action_mode`, registered `handler`, and structured `policy`;
- optional `enabled` only when its reviewed value matters.

The structured `policy` must contain non-empty text lists named:

- `automatic_behavior`
- `important_conditions`
- `notification_conditions`
- `approval_requirements`
- `execution`
- `validation`
- `rollback`
- `assumptions`

These fields drive the complete human proposal shown by the confirmation helper.

## 4. Apply conservative defaults

Use defaults only for missing routine details and disclose every one in `policy.assumptions`:

- Timezone: `Asia/Singapore`.
- Time of day: `09:00` when a cadence exists but no time is supplied.
- First due time: the next valid occurrence at the accepted local schedule time.
- Notification: important, uncertain, or failed results rather than routine success, unless the source asks for a reminder every occurrence.
- Unsupported automated work: a `notify` Task using `manual_notification`, with the procedure left for the operator.

Ask a clarification instead of inventing whether work recurs when no cadence is expressed.

Use only currently registered safe pairs:

- `notify` with `manual_notification` for reminders and unsupported procedures;
- `check` or `approved-procedure` with `pi_mcp_adapter` only for that reviewed named handler.

Read-only behavior may be automatic only when a registered handler supports it. Destructive, costly, credential-related, externally visible, or otherwise high-risk work always states that separate execution approval is required. Registration confirmation is not execution approval. When no reviewed named handler exists, represent the work as manual notification; never turn prose into unattended shell commands.

Keep credentials and secret values out of proposals. Describe where an operator obtains a credential, never store the credential itself.

## 5. Stage the exact proposal

Build one JSON object with this envelope:

```json
{
  "operation": "add",
  "task_id": null,
  "task": {
    "name": "...",
    "description": "...",
    "source_type": "direct",
    "source_ref": null,
    "source_summary": "...",
    "schedule": {"type": "daily", "time": "09:00"},
    "timezone": "Asia/Singapore",
    "next_run_at": "2026-09-01T01:00:00Z",
    "action_mode": "notify",
    "handler": "manual_notification",
    "policy": {
      "automatic_behavior": ["..."],
      "important_conditions": ["..."],
      "notification_conditions": ["..."],
      "approval_requirements": ["..."],
      "execution": ["..."],
      "validation": ["..."],
      "rollback": ["..."],
      "assumptions": ["..."]
    }
  }
}
```

For an update, use `"operation": "update"`, set `task_id`, and include the complete resulting Task payload.

Stage it through standard input:

```bash
python3 <skill-directory>/scripts/confirm.py stage <<'JSON'
<proposal JSON>
JSON
```

The helper displays Task name, schedule, timezone, automatic behavior, importance conditions, notifications, approvals, execution, validation, rollback, source, assumptions, add/update operation, and the exact structured proposal. Retain the printed review file path for the next user response.

No mutating CLI command has run at this point.

## 6. Require an explicit decision

Interpret only an unambiguous response to the currently staged proposal:

- **YES**: run `python3 <skill-directory>/scripts/confirm.py apply <review-file> YES`. The helper verifies the review hash and submits exactly the reviewed Task payload through `task add` or `task update`.
- **NO**: run `python3 <skill-directory>/scripts/confirm.py discard <review-file>`. Report that the registry was not changed.
- **EDIT**: discard the old review file, revise the proposal from the user's requested changes, stage the replacement, display every field again, and ask YES, NO, or EDIT again.
- Anything else: ask the user to choose YES, NO, or EDIT. Leave the review staged and do not invoke a mutating command.

If a confirmed add returns a duplicate/update-oriented CLI outcome, do not convert it automatically. Inspect the returned existing Task ID, prepare a complete update proposal, and require a new YES, NO, or EDIT decision.

The only path to `task add` or `task update` is the helper's `apply` command after explicit YES. NO and EDIT never change the registry.
