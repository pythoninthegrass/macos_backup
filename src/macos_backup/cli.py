"""
restic backup wrapper: macOS -> NAS over SFTP.

Usage:
    mb init
    mb backup [--dry-run]
    mb forget
    mb check
    mb snapshots
    mb restore <snapshotID> --target <dir> [restic restore flags...]
    mb install

Any other restic subcommand (unlock, stats, find, diff, tag, dump, ...) is
passed straight through to restic with the same repo/password/compression
flags as every command above.

Config is read from `.env` in the current directory, falling back to
environment variables, falling back to the defaults below. See
.env.example for the full key list.
"""

import contextlib
import datetime
import heapq
import json
import logging
import logging.handlers
import os
import re
import sh
import shutil
import signal
import sys
import time
from dataclasses import dataclass
from jinja2 import Environment, PackageLoader
from pathlib import Path
from rich.console import Console
from rich.filesize import decimal as human_bytes
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

# Plain list of the env var names Settings reads, used by tests to clear the
# environment before asserting on defaults. Each default itself lives inline
# on its `config("KEY", default=...)` call in load_settings, matching the
# python-decouple idiom in hello_pep_723 (config("HELLO", default="world")).
ENV_KEYS = [
    "RESTIC_REPOSITORY",
    "RESTIC_PASSWORD",
    "RESTIC_PASSWORD_COMMAND",
    "SFTP_COMMAND",
    "RESTIC_BIN",
    "BACKUP_ROOT",
    "EXCLUDE_FILE",
    "MANIFEST_DIR",
    "LOG_FILE",
    "RESTIC_COMPRESSION",
    "BACKUP_TAG",
    "BACKUP_HOST",
    "EXCLUDE_IF_PRESENT",
    "CHECK_READ_DATA_SUBSET",
    "DOCK_PLIST_PATH",
    "FDA_PROBE_PATH",
    "LAUNCHD_LABEL",
    "KEEP_DAILY",
    "KEEP_WEEKLY",
    "KEEP_MONTHLY",
    "PRUNE_WEEKDAY",
    "BACKUP_RETRY_DELAYS",
]


@dataclass(frozen=True)
class Settings:
    restic_repository: str
    restic_password: str
    restic_password_command: str
    sftp_command: str
    restic_bin: str
    backup_root: str
    exclude_file: str
    manifest_dir: str
    log_file: str
    restic_compression: str
    backup_tag: str
    backup_host: str
    exclude_if_present: str
    check_read_data_subset: str
    dock_plist_path: str
    fda_probe_path: str
    launchd_label: str
    keep_daily: int
    keep_weekly: int
    keep_monthly: int
    prune_weekday: int
    backup_retry_delays: str


def resolve_path(value: str, base: Path) -> str:
    """Expand $VARS/~ (python-decouple does neither) and resolve a relative path
    against `base` rather than the process's cwd, which launchd sets unpredictably
    (`cmd_install` pins `WorkingDirectory` to the cwd `install` was run from, so by
    the time a relative path is resolved here, launchd's cwd equals `base` again)."""
    expanded = Path(os.path.expandvars(value)).expanduser()
    if not expanded.is_absolute():
        expanded = base / expanded
    return str(expanded)


def load_settings(env_path: Path | None = None) -> Settings:
    """Load settings from `.env` in the current directory (or `DOTENV_FILE`, if set)
    if present, or environment variables, else the inline defaults below — each via
    python-decouple's own default=, e.g. config("HELLO", default="world").

    cwd rather than package location: once installed via `uv tool install`, this
    module lives in site-packages, so there's no script directory to anchor on
    (mirrors icarus's src/icarus/config.py)."""
    base = Path.cwd()
    env_path = env_path if env_path is not None else Path(os.environ.get("DOTENV_FILE", str(base / ".env")))

    if env_path.exists():
        from decouple import Config, RepositoryEnv

        config = Config(RepositoryEnv(str(env_path)))
    else:
        from decouple import config

    def path(key: str, default: str) -> str:
        return resolve_path(config(key, default=default), base)

    return Settings(
        restic_repository=config("RESTIC_REPOSITORY", default="sftp:syn:/shares/Mac/Backup"),
        restic_password=config("RESTIC_PASSWORD", default=""),
        restic_password_command=config(
            "RESTIC_PASSWORD_COMMAND",
            default="security find-generic-password -a restic -s restic-my-mac -w",
        ),
        sftp_command=config("SFTP_COMMAND", default="ssh -T -o RequestTTY=no syn -s sftp"),
        restic_bin=config("RESTIC_BIN", default="restic"),
        backup_root=path("BACKUP_ROOT", str(Path.home())),
        exclude_file=path("EXCLUDE_FILE", str(base / "excludes.txt")),
        manifest_dir=path("MANIFEST_DIR", str(Path.home() / ".local/state/macos_backup/manifest")),
        log_file=path("LOG_FILE", str(Path.home() / "Library/Logs/macos_backup.log")),
        restic_compression=config("RESTIC_COMPRESSION", default="fastest"),
        backup_tag=config("BACKUP_TAG", default="scheduled"),
        backup_host=config("BACKUP_HOST", default="my-mac"),
        exclude_if_present=config("EXCLUDE_IF_PRESENT", default=".nobackup"),
        check_read_data_subset=config("CHECK_READ_DATA_SUBSET", default="5%"),
        dock_plist_path=path("DOCK_PLIST_PATH", str(Path.home() / "Library/Preferences/com.apple.dock.plist")),
        fda_probe_path=path("FDA_PROBE_PATH", str(Path.home() / "Library/Mail")),
        launchd_label=config("LAUNCHD_LABEL", default="com.pythoninthegrass.restic-backup"),
        keep_daily=config("KEEP_DAILY", default=7, cast=int),
        keep_weekly=config("KEEP_WEEKLY", default=4, cast=int),
        keep_monthly=config("KEEP_MONTHLY", default=12, cast=int),
        prune_weekday=config("PRUNE_WEEKDAY", default=6, cast=int),
        backup_retry_delays=config("BACKUP_RETRY_DELAYS", default="30,120,300"),
    )


def restic_global_args(settings: Settings) -> list[str]:
    """Flags shared by every restic invocation: repo, sftp transport, password source.

    Password: RESTIC_PASSWORD (direct) or RESTIC_PASSWORD_COMMAND (keychain, etc.) —
    either works. restic's own precedence always prefers --password-command over its
    RESTIC_PASSWORD env-var fallback, so when a direct password is set we omit the
    flag entirely and let restic read RESTIC_PASSWORD from the subprocess environment
    itself (sh inherits the parent's env, including a RESTIC_PASSWORD set here or in
    the real shell/launchd environment) — the password never goes on argv either way.
    """
    args = [
        "--repo",
        settings.restic_repository,
        "-o",
        f"sftp.command={settings.sftp_command}",
    ]
    if not settings.restic_password:
        args += ["--password-command", settings.restic_password_command]
    args += ["--compression", settings.restic_compression]
    return args


def backup_args(settings: Settings, dry_run: bool) -> list[str]:
    args = [
        "backup",
        settings.backup_root,
        "--exclude-file",
        settings.exclude_file,
        "--exclude-caches",
        "--exclude-if-present",
        settings.exclude_if_present,
        "--one-file-system",
        "--tag",
        settings.backup_tag,
        "--host",
        settings.backup_host,
    ]
    if dry_run:
        args.append("--dry-run")
    return args


def forget_args(settings: Settings) -> list[str]:
    return [
        "forget",
        "--keep-daily",
        str(settings.keep_daily),
        "--keep-weekly",
        str(settings.keep_weekly),
        "--keep-monthly",
        str(settings.keep_monthly),
        "--prune",
    ]


def should_prune(today: datetime.date, settings: Settings) -> bool:
    return today.weekday() == settings.prune_weekday


DOCK_URL_RE = re.compile(r'"_CFURLString"\s*=\s*"file://(.*?)";')


def parse_dock_apps(defaults_output: str) -> list[str]:
    """Extract app bundle paths from `defaults read com.apple.dock persistent-apps` output."""
    return [path for path in DOCK_URL_RE.findall(defaults_output)]


def make_restic(settings: Settings):
    return sh.Command(settings.restic_bin).bake(*restic_global_args(settings), "--json")


def setup_logging(settings: Settings) -> logging.Logger:
    """All verbose detail (raw restic JSON, stderr chatter) goes here, never to stdout."""
    log_path = Path(settings.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("macos_backup")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=3)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def parse_json_line(line: str) -> dict | None:
    """Decode one line of restic's --json stream; None for blank/non-JSON lines."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def format_status(status: dict) -> str:
    files_done = status.get("files_done", 0)
    total_files = status.get("total_files", 0)
    bytes_done = status.get("bytes_done", 0)
    total_bytes = status.get("total_bytes", 0)
    return f"{files_done:,}/{total_files:,} files · {human_bytes(bytes_done)}/{human_bytes(total_bytes)}"


def format_backup_summary(summary: dict | None, error_count: int, dry_run: bool = False) -> str:
    label = "Dry run" if dry_run else "Backup"
    error_clause = f"{error_count} error{'s' if error_count != 1 else ''}"
    if summary is None:
        return f"{label} ended without a summary ({error_clause})"

    files_new = summary.get("files_new", 0)
    files_changed = summary.get("files_changed", 0)
    data_added = human_bytes(summary.get("data_added", 0))
    duration = summary.get("total_duration", 0)
    return f"{label}: {files_new:,} new, {files_changed:,} changed, {data_added} added in {duration:.0f}s ({error_clause})"


def is_partial_backup_exit(exit_code: int) -> bool:
    """restic's own documented exit code for 'backup completed, but some source
    files could not be read (incomplete snapshot created)' — a normal partial
    success, not a failure. See doc/075_scripting.rst in restic's own repo."""
    return exit_code == 3


def format_error_notice(error_count: int, timestamp: str, log_file: str) -> str:
    return f"⚠ {error_count} error{'s' if error_count != 1 else ''} at {timestamp} — see {log_file}"


def is_retryable_backup_error(exit_code: int) -> bool:
    """Exit 1 is restic's generic fatal error. restic gives config/lock/auth
    problems their own dedicated codes (10 repo missing, 11 lock failed, 12
    wrong password), and preflight() already proved ssh reachability and FDA
    before the backup started — so a bare exit 1 mid-backup is presumed to be
    a dropped connection rather than a config problem, and worth retrying."""
    return exit_code == 1


def parse_retry_delays(spec: str) -> list[int]:
    return [int(part.strip()) for part in spec.split(",")]


def push_top_file(heap: list[tuple[int, str, str]], size: int, name: str, path: str, limit: int = 10) -> None:
    """Keep only the `limit` largest (size, name, path) entries seen so far, via a bounded min-heap."""
    entry = (size, name, path)
    if len(heap) < limit:
        heapq.heappush(heap, entry)
    elif size > heap[0][0]:
        heapq.heapreplace(heap, entry)


def accumulate_dir_size(dir_sizes: dict[str, int], file_path: str, file_size: int, root: str) -> None:
    """restic's ls output carries no aggregate directory size, so add this file's
    size to every ancestor directory from its parent up to (and including) root."""
    root_path = Path(root)
    current = Path(file_path).parent
    while True:
        key = str(current)
        dir_sizes[key] = dir_sizes.get(key, 0) + file_size
        if current == root_path or current == current.parent:
            break
        current = current.parent


def top_entries_table_rows(
    top_files: list[tuple[int, str, str]], dir_sizes: dict[str, int], limit: int = 10
) -> list[tuple[str, str, int]]:
    """Merge the largest files with the largest directories (by rows of name, path, size)."""
    rows = [(name, path, size) for size, name, path in top_files]
    rows += [(Path(path).name, path, size) for path, size in dir_sizes.items()]
    rows.sort(key=lambda row: row[2], reverse=True)
    return rows[:limit]


_current_process = None  # the restic subprocess a signal should be forwarded to, if any; see _handle_termination_signal


def _handle_termination_signal(signum, _frame) -> None:
    """Forward the exact signal we received to the running restic subprocess — so
    it cancels for the real reason (SIGINT vs SIGTERM) instead of guessing from a
    severed pipe or an orphaned, un-signaled child — then raise KeyboardInterrupt
    so every existing SIGINT code path (run_with_interrupt_drain, cmd_backup, the
    top-level handler) also runs for SIGTERM. Python only does this conversion
    automatically for SIGINT; SIGTERM's default disposition is to die immediately
    with no cleanup at all, which is exactly what launchctl/shutdown send.
    """
    if _current_process is not None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(_current_process.pid, signum)
    raise KeyboardInterrupt


def install_signal_handlers() -> None:
    signal.signal(signal.SIGINT, _handle_termination_signal)
    signal.signal(signal.SIGTERM, _handle_termination_signal)


def run_with_interrupt_drain(command, *args, logger: logging.Logger, out_handler=None, err_handler=None):
    """Run a sh command in the background, draining it to completion even on Ctrl-C.

    restic receives SIGINT directly (same foreground process group) and does its own
    cleanup; we just need to not abandon the wait so the repo lock is released cleanly,
    and to turn the resulting KeyboardInterrupt into a clean re-raise instead of a
    traceback from inside sh's os.waitpid plumbing.

    _bg_exc=False is required: without it, sh's own background thread raises
    ErrorReturnCode internally the moment the process exits non-zero, and since
    nothing catches it there, Python dumps a raw traceback from that thread
    regardless of any try/except around process.wait() in the calling thread.
    With it, the exception is deferred to wait(), where we actually handle it.

    _tty_out=False is required: sh allocates a pty for stdout by default, so
    restic thinks it's attached to a terminal and interleaves ANSI clear-line
    sequences into stdout even with --json, corrupting it as a parse target.
    """
    global _current_process
    kwargs = {"_bg": True, "_bg_exc": False, "_tty_out": False}
    if out_handler is not None:
        kwargs["_out"] = out_handler
        kwargs["_out_bufsize"] = 1
    if err_handler is not None:
        kwargs["_err"] = err_handler
        kwargs["_err_bufsize"] = 1

    process = command(*args, **kwargs)
    _current_process = process
    try:
        try:
            process.wait()
        except KeyboardInterrupt:
            logger.warning("Interrupted by user; waiting for restic to finish cleaning up")
            try:
                process.wait()
            except KeyboardInterrupt:
                logger.warning("Second interrupt; giving up waiting on restic")
            except sh.ErrorReturnCode:
                pass
            raise
        return process
    finally:
        _current_process = None


def preflight(settings: Settings, console: Console, logger: logging.Logger) -> None:
    with console.status("Checking SSH reachability to syn ...", spinner="dots"):
        try:
            sh.ssh("-T", "syn", "true")
        except sh.ErrorReturnCode as e:
            logger.error("ssh preflight to syn failed: %s", e)
            console.print(f"[red]✗[/red] Cannot reach syn over ssh (see {settings.log_file})")
            sys.exit(1)

    probe_dir = Path(settings.fda_probe_path)
    try:
        list(probe_dir.iterdir())
    except PermissionError:
        console.print(
            f"[red]✗[/red] Full Disk Access is not granted to {Path(sys.executable).resolve()}. "
            "Grant it in System Settings > Privacy & Security > Full Disk Access, then retry."
        )
        sys.exit(1)
    except FileNotFoundError:
        pass  # no Mail.app configured; not a permissions problem
    console.print("[green]✓[/green] Pre-flight checks passed")


def write_manifests(settings: Settings, console: Console, logger: logging.Logger) -> None:
    manifest_dir = Path(settings.manifest_dir)
    manifest_dir.mkdir(parents=True, exist_ok=True)

    with console.status("Writing manifests (Brewfile, Dock, mise, Applications) ...", spinner="dots"):
        try:
            # describe is brew's default now; passing --describe explicitly errors ("disabled! Use the default behaviour instead").
            sh.brew(
                "bundle",
                "dump",
                "--force",
                f"--file={manifest_dir / 'Brewfile'}",
            )
        except sh.ErrorReturnCode as e:
            logger.warning("brew bundle dump failed: %s", e)

        try:
            dock_raw = str(sh.defaults("read", "com.apple.dock", "persistent-apps"))
            apps = parse_dock_apps(dock_raw)
            (manifest_dir / "dock-apps.txt").write_text("\n".join(apps) + "\n")
        except sh.ErrorReturnCode as e:
            logger.warning("could not read dock persistent-apps: %s", e)

        dock_plist = Path(settings.dock_plist_path)
        if dock_plist.exists():
            sh.cp(str(dock_plist), str(manifest_dir / "com.apple.dock.plist"))

        try:
            (manifest_dir / "mise-current.txt").write_text(str(sh.mise("ls", "--current")))
        except (sh.ErrorReturnCode, sh.CommandNotFound) as e:
            logger.warning("mise ls --current failed: %s", e)
        (manifest_dir / "applications.txt").write_text(str(sh.ls("-1", "/Applications")))
    console.print("[green]✓[/green] Manifests written")


def _log_stderr_line(logger: logging.Logger) -> callable:
    def handler(line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        msg = parse_json_line(stripped)
        # restic's per-file archival errors arrive on stderr as JSON with a "during" key.
        if msg and msg.get("during") == "archival":
            logger.warning("restic could not read %s: %s", msg.get("item"), msg.get("error", {}).get("message"))
        else:
            logger.debug("restic stderr: %s", stripped)

    return handler


def cmd_init(settings: Settings, console: Console, logger: logging.Logger) -> None:
    with console.status("Initializing repository ...", spinner="dots"):
        process = run_with_interrupt_drain(make_restic(settings), "init", logger=logger, err_handler=_log_stderr_line(logger))
    result = json.loads(str(process))
    console.print(f"[green]✓[/green] Repository initialized: {result.get('id', '?')[:12]}")


def print_top_entries_table(console: Console, restic, settings: Settings, logger: logging.Logger, snapshot_id: str) -> None:
    """Rank the 10 largest files/directories in the snapshot just created.
    restic's ls output carries no aggregate directory size, so it's computed
    here by streaming every file node and accumulating into its ancestors."""
    top_files: list[tuple[int, str, str]] = []
    dir_sizes: dict[str, int] = {}

    def handle_node(line: str) -> None:
        node = parse_json_line(line)
        if node is None or node.get("message_type") != "node" or node.get("type") != "file":
            return
        size = node.get("size", 0)
        path = node.get("path", "")
        push_top_file(top_files, size, node.get("name", "?"), path)
        accumulate_dir_size(dir_sizes, path, size, settings.backup_root)

    with console.status("Ranking the largest files and directories ...", spinner="dots"):
        try:
            run_with_interrupt_drain(
                restic, "ls", snapshot_id, logger=logger, out_handler=handle_node, err_handler=_log_stderr_line(logger)
            )
        except sh.ErrorReturnCode as e:
            logger.warning("restic ls %s failed: %s", snapshot_id, e)
            return

    table = Table(title="Top 10 largest files/directories")
    table.add_column("Name")
    table.add_column("Path", overflow="fold")
    table.add_column("Size", justify="right")
    for name, path, size in top_entries_table_rows(top_files, dir_sizes):
        table.add_row(name, path, human_bytes(size))
    console.print(table)


def cmd_backup(settings: Settings, dry_run: bool, console: Console, logger: logging.Logger) -> None:
    preflight(settings, console, logger)
    if not dry_run:
        write_manifests(settings, console, logger)

    restic = make_restic(settings)
    args = backup_args(settings, dry_run)
    logger.info("restic command: %s", " ".join(args))

    error_count = 0
    last_summary = None

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Starting backup ...", total=None)
        done_description = "Dry-run complete" if dry_run else "Backup complete"

        def handle_stdout(line: str) -> None:
            nonlocal error_count, last_summary
            msg = parse_json_line(line)
            if msg is None:
                logger.debug("restic: %s", line.strip())
                return
            logger.debug("restic %s: %s", msg.get("message_type"), line.strip())

            match msg.get("message_type"):
                case "status":
                    # error_count lives on restic's own status messages, not the stdout "error" type.
                    error_count = msg.get("error_count", error_count)
                    progress.update(
                        task,
                        total=msg.get("total_bytes") or None,
                        completed=msg.get("bytes_done", 0),
                        description=format_status(msg),
                    )
                case "summary":
                    last_summary = msg
                    progress.update(task, completed=1, total=1, description=done_description)

        delays = parse_retry_delays(settings.backup_retry_delays)
        attempt = 0
        while True:
            try:
                run_with_interrupt_drain(
                    restic,
                    *args,
                    logger=logger,
                    out_handler=handle_stdout,
                    err_handler=_log_stderr_line(logger),
                )
                break
            except KeyboardInterrupt:
                console.print("[yellow]Backup interrupted by user.[/yellow]")
                raise
            except sh.ErrorReturnCode as e:
                if is_partial_backup_exit(e.exit_code):
                    logger.warning("restic exited %d: %d source file(s) could not be read", e.exit_code, error_count)
                    progress.update(task, description=done_description)
                    break
                if is_retryable_backup_error(e.exit_code) and attempt < len(delays):
                    delay = delays[attempt]
                    attempt += 1
                    logger.warning(
                        "restic exited %d (presumed network loss) — retry %d/%d in %ds",
                        e.exit_code,
                        attempt,
                        len(delays),
                        delay,
                    )
                    progress.update(task, description=f"Connection lost — retrying in {delay}s (attempt {attempt}/{len(delays)})")
                    time.sleep(delay)
                    continue
                raise

    console.print(format_backup_summary(last_summary, error_count, dry_run=dry_run))

    if error_count > 0:
        when = (last_summary or {}).get("backup_end") or datetime.datetime.now().astimezone().isoformat()
        console.print(f"[yellow]{format_error_notice(error_count, when, settings.log_file)}[/yellow]")

    if not dry_run and last_summary and last_summary.get("snapshot_id"):
        print_top_entries_table(console, restic, settings, logger, last_summary["snapshot_id"])

    if not dry_run and should_prune(datetime.date.today(), settings):
        with console.status("Prune day: running forget --prune ...", spinner="dots"):
            process = run_with_interrupt_drain(
                restic, *forget_args(settings), logger=logger, err_handler=_log_stderr_line(logger)
            )
        logger.debug("forget output: %s", str(process))
        console.print("[green]✓[/green] forget --prune complete")


def cmd_forget(settings: Settings, console: Console, logger: logging.Logger) -> None:
    restic = make_restic(settings)
    with console.status("Running forget --prune ...", spinner="dots"):
        process = run_with_interrupt_drain(restic, *forget_args(settings), logger=logger, err_handler=_log_stderr_line(logger))
    logger.debug("forget output: %s", str(process))
    console.print("[green]✓[/green] forget --prune complete")


def cmd_check(settings: Settings, console: Console, logger: logging.Logger) -> None:
    restic = make_restic(settings)
    with console.status(f"Checking repository ({settings.check_read_data_subset} data read) ...", spinner="dots"):
        try:
            process = run_with_interrupt_drain(
                restic,
                "check",
                f"--read-data-subset={settings.check_read_data_subset}",
                logger=logger,
                err_handler=_log_stderr_line(logger),
            )
        except sh.ErrorReturnCode as e:
            logger.error("restic check failed: %s", e)
            console.print(f"[red]✗[/red] Check failed (see {settings.log_file})")
            sys.exit(e.exit_code)
    logger.debug("check output: %s", str(process))
    console.print("[green]✓[/green] Check passed")


def cmd_snapshots(settings: Settings, console: Console, logger: logging.Logger) -> None:
    restic = make_restic(settings)
    with console.status("Fetching snapshots ...", spinner="dots"):
        process = run_with_interrupt_drain(restic, "snapshots", logger=logger, err_handler=_log_stderr_line(logger))
    snapshots = json.loads(str(process))
    console.print(f"[green]✓[/green] {len(snapshots)} snapshot(s)")
    for snap in snapshots:
        console.print(f"  {snap.get('short_id', '?')}  {snap.get('time', '?')}  {snap.get('hostname', '?')}")


def cmd_passthrough(settings: Settings, console: Console, logger: logging.Logger, args: list[str]) -> None:
    """Forward any restic subcommand without its own cmd_* handler above (restore,
    unlock, stats, find, diff, tag, dump, ...) straight through, with the same
    global args (repo, password, compression) as every other invocation."""
    restic = make_restic(settings)
    logger.info("restic command: %s", " ".join(args))
    try:
        run_with_interrupt_drain(
            restic,
            *args,
            logger=logger,
            out_handler=lambda line: console.print(line.rstrip()),
            err_handler=_log_stderr_line(logger),
        )
    except sh.ErrorReturnCode as e:
        logger.error("restic %s failed: %s", args[0], e)
        console.print(f"[red]✗[/red] restic {' '.join(args)} failed (see {settings.log_file})")
        sys.exit(e.exit_code)
    console.print(f"[green]✓[/green] restic {' '.join(args)} complete")


def build_launchd_path_env() -> str:
    """PATH for the LaunchAgent's subprocess env: mise shims first (so sh.mise/
    sh.restic-via-RESTIC_BIN resolve the project-pinned versions), then mise's
    own bin, then homebrew's bin (resolved, not hardcoded — Apple silicon and
    Intel differ), then the standard system dirs."""
    dirs = [
        str(Path.home() / ".local/share/mise/shims"),
        str(Path.home() / ".local/bin"),
    ]
    brew = shutil.which("brew")
    if brew:
        dirs.append(str(Path(brew).parent))
    dirs += ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return ":".join(dirs)


def render_launchd_plist(
    settings: Settings, program: str, working_directory: str, path_env: str, hour: int = 12, minute: int = 30
) -> str:
    """Render the packaged restic-backup.plist.j2 against this machine's actual
    paths, so the committed template carries no machine- or user-specific absolute
    path. `program` is the installed `mb` binary; `working_directory` is where
    launchd will cd before running it, which is also where `.env` and
    `excludes.txt` are read from (see load_settings)."""
    env = Environment(loader=PackageLoader("macos_backup", "templates"), autoescape=True)
    template = env.get_template("restic-backup.plist.j2")
    return template.render(
        label=settings.launchd_label,
        program=program,
        working_directory=working_directory,
        log_file=settings.log_file,
        path_env=path_env,
        hour=hour,
        minute=minute,
    )


def cmd_install(settings: Settings, console: Console, _logger: logging.Logger) -> None:
    program = shutil.which("mb")
    if program is None:
        console.print("[red]✗[/red] `mb` not found on PATH. Run `uv tool install .` (or the git URL) first.")
        sys.exit(1)

    rendered = render_launchd_plist(
        settings, program=program, working_directory=str(Path.cwd()), path_env=build_launchd_path_env()
    )

    plist_name = f"{settings.launchd_label}.plist"
    dst = Path.home() / "Library/LaunchAgents" / plist_name
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(rendered)
    console.print(f"[green]✓[/green] Rendered {dst}")

    uid = os.getuid()
    try:
        sh.launchctl("bootout", f"gui/{uid}/{settings.launchd_label}")
    except sh.ErrorReturnCode:
        pass  # not loaded yet
    sh.launchctl("bootstrap", f"gui/{uid}", str(dst))
    console.print("[green]✓[/green] LaunchAgent installed and bootstrapped")


def main() -> None:
    install_signal_handlers()
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return

    command = args[0]
    settings = load_settings()
    console = Console()
    logger = setup_logging(settings)

    match command:
        case "init":
            cmd_init(settings, console, logger)
        case "backup":
            cmd_backup(settings, dry_run="--dry-run" in args, console=console, logger=logger)
        case "forget":
            cmd_forget(settings, console, logger)
        case "check":
            cmd_check(settings, console, logger)
        case "snapshots":
            cmd_snapshots(settings, console, logger)
        case "restore":
            cmd_passthrough(settings, console, logger, args)
        case "install":
            cmd_install(settings, console, logger)
        case "-h" | "--help" | "help":
            print(__doc__)
        case _:
            cmd_passthrough(settings, console, logger, args)


def run() -> None:
    """Entry point for both the `mb` console script and `./main.py`."""
    try:
        main()
    except KeyboardInterrupt:
        Console().print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)
    except sh.ErrorReturnCode as e:
        Console().print(f"[red]Failed:[/red] exit code {e.exit_code} (see the log for details)")
        sys.exit(e.exit_code)


if __name__ == "__main__":
    run()
