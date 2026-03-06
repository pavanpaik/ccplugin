# ccplugin — Claude Code Guidelines

## Project Overview

Single-file Python CLI (`ccplugin.py`) that installs Claude Code plugins from any Git URL or local path, bypassing marketplace allowlist restrictions. Zero external dependencies — stdlib only.

## Running Tests

```bash
python3 test_ccplugin.py -v
```

No test framework required. All 25 tests use stdlib `unittest`.

## Code Conventions

- Single file (`ccplugin.py`) — keep it that way. No new modules.
- No external dependencies. Only stdlib imports.
- Python 3.9+ syntax is fine (`X | Y` union types are not — use `Optional[X]`).
- All JSON reads/writes go through `read_json`/`write_json`. Never bypass these — `write_json` is atomic and `write_json` acquires a file lock via `_lock()`.
- `error()` calls `sys.exit(1)` — use it for unrecoverable failures only.

## Key Architecture Decisions

- **Plugin key format**: `<name>@local` — matches Claude Code's internal format.
- **Install record**: stored as an array in `installed_plugins.json["plugins"]` to support multiple scopes for the same plugin.
- **`targetName`** is stored in the install record so `ccplugin update` can pass the correct subdirectory selector back to `resolve_plugin`.
- **File locking**: `fcntl.flock` on POSIX, no-op on Windows. Always lock before read-modify-write on `installed_plugins.json` or any `settings.json`.
- **Atomic installs**: copy to `.tmp-<name>` first, then rename. Never delete the live directory before the copy succeeds.

## Important Caveats

The `installed_plugins.json` and `settings.json` formats are reverse-engineered from Claude Code internals. If Anthropic changes these formats, plugins may silently fail to load. The `doctor` command can help diagnose this.
