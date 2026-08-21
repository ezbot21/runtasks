# RunTasks

RunTasks is a portable Python application for durable, reviewed operational tasks. This bootstrap release provides an isolated runtime home, versioned SQLite initialization, and CLI health reporting.

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
