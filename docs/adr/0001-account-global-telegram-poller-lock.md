# Keep the Telegram poller lock account-global

`RUNTASKS_HOME` relocates one runtime's configuration, database, logs, and backups, but the Telegram single-poller guarantee must also hold when the same bot token is configured in two runtime homes. Store the non-secret, token-hash lock under the OS account's canonical `~/runtasks/var/data/` directory, create it lazily, and allow tests to inject a temporary global lock directory; no Telegram credential or application record is stored there.
