# AGENTS.md

This file provides guidance to AI agents when working with code in this repository.

## What this is

A restic backup wrapper (`mb`) for one Mac ("my-mac"), backing up to a Synology NAS over SFTP (`sftp:syn:/shares/Mac/Backup`). Packaged with a `uv_build` `pyproject.toml` so it can be installed globally (`uv tool install git+https://github.com/pythoninthegrass/macos_backup.git` drops `mb` into `~/.local/bin`) or run ad hoc from a checkout (`./main.py ...` / `uv run main.py ...`), the way `~/git/icarus` is packaged.

Backs up all of `$HOME` minus reinstallable runtimes/packages, cloud-synced folders, VMs, and caches (see `excludes.txt`). It also writes manifests (`brew bundle dump`, Dock persistent-apps, `mise ls --current`, `/Applications` listing) on every real (non-dry-run) backup, since those aren't backed up as files but need to be restorable.

## Project Structure

```text
main.py                              # thin ad hoc entry point: `from macos_backup.cli import run`
src/macos_backup/
  __init__.py                        # re-exports main, run
  cli.py                             # everything: Settings, arg builders, cmd_* handlers, run()
  templates/restic-backup.plist.j2   # launchd template, packaged as data
pyproject.toml                       # uv_build backend + `mb` entry point
excludes.txt                         # restic --exclude-file
.env.example                         # config key reference
tests/test_main.py                   # pytest suite
```

## Commands

```bash
mb init                                      # one-time: initialize the restic repo
mb backup --dry-run                          # preview what would be sent, no manifests written
mb backup                                    # real backup: preflight -> manifests -> restic backup -> prune (Sundays only)
mb snapshots                                 # list snapshots
mb check                                     # restic check --read-data-subset=5%
mb forget                                    # apply keep policy + prune, on demand
mb restore <snapshotID> --target <dir> [restic flags...]
mb install                                   # render launchd plist from .j2, write + bootstrap it
mb <any other restic subcommand>             # passed straight through with repo/password/compression flags (unlock, stats, find, diff, tag, dump, ...)

# Ad hoc, from a checkout, no install:
./main.py backup --dry-run
uv run main.py backup --dry-run
```

Any command not matching a `cmd_*` case falls through to `cmd_passthrough`, which forwards to `restic` with the same global args (`--repo`, `-o sftp.command=...`, password, `--compression`) as every other command.

### Tests

```bash
uv run pytest tests/
```

Tests are pure-function unit tests against `macos_backup.cli` (settings loading, arg-building, parsing, signal handling, plist rendering) — no live restic/SSH calls. Run a single test with `uv run pytest tests/test_main.py::TestClassName::test_name -k test_name`.

### Lint

```bash
ruff format --check --diff .
ruff format .
```

`ruff.toml` sets `fix-only = true` for lint, line-length 130, and ignores `E501`, `F401`, `F841`, etc. — don't fight the config.

## Architecture

Everything lives in `src/macos_backup/cli.py`, organized top-to-bottom as:

1. **`Settings`** (frozen dataclass) + `load_settings()` — config precedence is `.env` in the current directory (via python-decouple, overridable with `DOTENV_FILE`) → environment variables → inline defaults on each `config(..., default=...)` call. `ENV_KEYS` is a plain list of every key `Settings` reads, kept in sync for tests that need to clear the environment. Path-like settings (`BACKUP_ROOT`, `EXCLUDE_FILE`, `MANIFEST_DIR`, `LOG_FILE`, `DOCK_PLIST_PATH`, `FDA_PROBE_PATH`) go through `resolve_path()`, which expands `$VARS`/`~` (decouple does neither) and resolves relative paths against the cwd, not the installed package location — once installed via `uv tool install`, `cli.py` lives in site-packages, so there's no script directory to anchor on. `mb install` pins `WorkingDirectory` to the cwd it was run from, so launchd's cwd matches this at run time (mirrors `icarus/src/icarus/config.py`'s cwd-based config lookup).

2. **Arg builders** (`restic_global_args`, `backup_args`, `forget_args`) — pure functions that turn `Settings` into restic CLI argument lists. Keep these pure and tested; they're the main unit-test surface.

3. **Subprocess execution** (`run_with_interrupt_drain`, `make_restic`) — wraps `sh` with `_bg=True, _bg_exc=False, _tty_out=False`. `_bg_exc=False` defers exceptions to `process.wait()` instead of letting `sh`'s background thread dump a raw traceback. `_tty_out=False` stops `sh` from allocating a pty that would corrupt `--json` output with ANSI sequences. SIGINT/SIGTERM handlers (`_handle_termination_signal`) forward the exact signal to the tracked `_current_process` (a module-level global) so restic exits for the real reason instead of a severed pipe.

4. **Command handlers** (`cmd_init`, `cmd_backup`, `cmd_forget`, `cmd_check`, `cmd_snapshots`, `cmd_passthrough`, `cmd_install`) — one per CLI subcommand, dispatched from `main()`'s `match` statement. `cmd_backup` is the complex one: preflight checks → manifest writing → streaming restic JSON output through `rich.Progress` → retry loop on presumed-transient exit code 1 (`is_retryable_backup_error`, with delays from `BACKUP_RETRY_DELAYS`) → partial-success handling on exit code 3 (`is_partial_backup_exit`) → top-10-largest-files/dirs table (computed client-side since `restic ls` carries no aggregate directory size — see `accumulate_dir_size`/`push_top_file`) → Sunday-only prune (`should_prune`).

5. **launchd install** (`build_launchd_path_env`, `render_launchd_plist`, `cmd_install`) — renders the packaged `macos_backup/templates/restic-backup.plist.j2` via Jinja2's `PackageLoader` into a machine-specific plist, writes it straight to `~/Library/LaunchAgents/`, and bootstraps it with `launchctl`. `ProgramArguments` points at the installed `mb` binary (resolved via `shutil.which`, so `cmd_install` fails loudly if it isn't on PATH yet), not a `uv run --script` invocation. `WorkingDirectory` is the cwd `mb install` was run from — normally the checkout, since that's where `.env`, `excludes.txt`, and `.tool-versions` live. The template takes no machine-specific paths directly; all of that is injected by Python at render time.

6. **Entry points** — `run()` wraps `main()` with the top-level KeyboardInterrupt/`sh.ErrorReturnCode` handling and is what both `[project.scripts] mb` and `main.py`'s `__main__` block call.

### Key operational details worth knowing before touching this code

- **No NFS mount dependency.** Backs up over SFTP, not `/Volumes/shares`, because that mount isn't in `autofs`/fstab and can't be relied on by a scheduled job. The Synology chroots SFTP per-share, so the repo path is `/shares/Mac/Backup`, not `/volume1/shares/Mac/Backup`.
- **`~/.ssh/config`'s `Host syn` forces `RequestTTY force`**, which breaks the SFTP subsystem — restic must be told to run its own ssh with `-o RequestTTY=no` via `SFTP_COMMAND` in `.env`/defaults.
- **Password handling**: either `RESTIC_PASSWORD` (direct) or `RESTIC_PASSWORD_COMMAND` (default reads the login keychain via `security`) — never both, never the literal password on argv. When `RESTIC_PASSWORD` is set, `--password-command` is omitted entirely so restic falls back to reading the env var itself.
- **Full Disk Access** is required by whichever Python binary `mb` resolves to (the uv tool venv's interpreter once installed), checked in `preflight()` by probing `FDA_PROBE_PATH` (default `~/Library/Mail`) and reported with `sys.executable`'s resolved path. Must be re-granted after a `uv tool install --force`/upgrade changes that interpreter's path.
- **`mise` pins the toolchain** (`.tool-versions`): restic, uv, python, ruff, prek, jsonschema — the launchd PATH (`build_launchd_path_env`) puts mise shims first so the scheduled job resolves the same pinned `restic` as interactive use.

<!-- BACKLOG.MD MCP GUIDELINES START -->
<!-- backlog.md-instructions-version: 1.48.0 -->

<CRITICAL_INSTRUCTION>

## BACKLOG WORKFLOW INSTRUCTIONS

This project uses Backlog.md MCP for all task and project management activities.

**CRITICAL GUIDANCE**

- If your client supports MCP resources, read `backlog://workflow/overview` to understand when and how to use Backlog for this project.
- If your client only supports tools or the above request fails, call `backlog.get_backlog_instructions()` to load the tool-oriented overview. Use the `instruction` selector when you need `task-creation`, `task-execution`, or `task-finalization`.

- **First time working here?** Read the overview resource IMMEDIATELY to learn the workflow
- **Already familiar?** You should have the overview cached ("## Backlog.md Overview (MCP)")
- **When to read it**: BEFORE creating tasks, or when you're unsure whether to track work

These guides cover:
- Decision framework for when to create tasks
- Search-first workflow to avoid duplicates
- Links to detailed guides for task creation, execution, and finalization
- MCP tools reference

You MUST read the overview resource to understand the complete workflow. The information is NOT summarized here.

</CRITICAL_INSTRUCTION>

<!-- BACKLOG.MD MCP GUIDELINES END -->
