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

## Reviewed use in RunTasks

RunTasks uses only the asynchronous `telegram.Bot` client for:

- `getMe` to verify the bot username;
- `getWebhookInfo` to reject webhook mode;
- `getUpdates` with a positive timeout for long polling;
- `getChat` and `getChatMember` to verify private or group destinations;
- `sendChatAction` with a configured forum topic ID to verify that the topic exists and is writable;
- `sendMessage` for redacted outbound notifications.

RunTasks does not use the library's webhook server, persistence, job queue, arbitrary callback execution, file download, or payment features. Tokens remain in private configuration and are never serialized to SQLite. Library exceptions are converted to fixed safe application errors. Telegram/HTTP log records pass through a process-wide redacting record factory, while the handler boundary sanitizes structured `extra` fields added after record creation.

## Pinning and update policy

The direct dependency is exact-pinned in `pyproject.toml`, and the complete resolution is committed in `uv.lock`. Any update requires a new review of release notes, relevant security advisories, API compatibility, transitive dependency changes, and the fake Bot API test suite before changing the pin.
