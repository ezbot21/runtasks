# Dependency review: python-telegram-bot 22.8

**Reviewed:** 2026-08-21
**Decision:** Approved as an exact runtime pin: `python-telegram-bot==22.8`

## Sources reviewed

- PyPI release metadata: <https://pypi.org/project/python-telegram-bot/22.8/>
- Versioned API documentation: <https://docs.python-telegram-bot.org/en/v22.8/>
- Versioned changelog: <https://docs.python-telegram-bot.org/en/v22.8/changelog.html>
- Source release tag: <https://github.com/python-telegram-bot/python-telegram-bot/tree/v22.8>
- Telegram Bot API `getUpdates` contract: <https://core.telegram.org/bots/api#getupdates>
- Telegram Bot API `sendChatAction` contract: <https://core.telegram.org/bots/api#sendchataction>
- Telegram Bot API inline keyboard contract: <https://core.telegram.org/bots/api#inlinekeyboardmarkup>
- Telegram Bot API callback query contract: <https://core.telegram.org/bots/api#callbackquery>
- Telegram Bot API `answerCallbackQuery` contract: <https://core.telegram.org/bots/api#answercallbackquery>

## Reviewed use in RunTasks

RunTasks uses only the asynchronous `telegram.Bot` client for:

- `getMe` to verify the bot username;
- `getWebhookInfo` to reject webhook mode;
- `getUpdates` with a positive timeout for long polling, plus one non-blocking `timeout=0`, `offset=-1` request during setup to drain stale pending updates before waiting for the operator's new `/start`;
- `getChat` and `getChatMember` to verify private or group destinations;
- `sendChatAction` with a configured forum topic ID to verify that the topic exists and is writable;
- `sendMessage` for redacted outbound notifications and Decision messages with one inline keyboard row;
- normalized `callback_query` updates containing only numeric sender/chat identity, message identity, and compact callback data;
- `answerCallbackQuery` for fixed redacted authorization, validation, and current-state responses.

RunTasks does not use the library's webhook server, persistence, job queue, arbitrary callback dispatch framework, file download, or payment features. Callback actions are parsed and authorized by RunTasks before its transactional Decision state machine is called; the library never selects or invokes a mutation handler. Tokens remain in private configuration and are never serialized to SQLite. Library exceptions are converted to fixed safe application errors. Telegram/HTTP log records pass through a process-wide redacting record factory, while the handler boundary sanitizes structured `extra` fields added after record creation.

## Pinning and update policy

The direct dependency is exact-pinned in `pyproject.toml`, and the complete resolution is committed in `uv.lock`. Any update requires a new review of release notes, relevant security advisories, API compatibility, transitive dependency changes, and the fake Bot API test suite before changing the pin.
