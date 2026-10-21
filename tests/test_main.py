"""Unit tests for macos_backup.cli's pure functions.

Run with:
    uv run pytest tests/
"""

import datetime
import plistlib
import pytest
from pathlib import Path

from macos_backup import cli as main


def make_settings(**overrides):
    base = dict(
        restic_repository="sftp:syn:/shares/Mac/Backup",
        restic_password="",
        restic_password_command="security find-generic-password -a restic -s restic-my-mac -w",
        sftp_command="ssh -T -o RequestTTY=no syn -s sftp",
        restic_bin="restic",
        backup_root=str(Path.home()),
        exclude_file=str(Path(__file__).resolve().parent.parent / "excludes.txt"),
        manifest_dir=str(Path.home() / ".local/state/macos_backup/manifest"),
        log_file=str(Path.home() / "Library/Logs/macos_backup.log"),
        restic_compression="fastest",
        backup_tag="scheduled",
        backup_host="my-mac",
        exclude_if_present=".nobackup",
        check_read_data_subset="5%",
        dock_plist_path=str(Path.home() / "Library/Preferences/com.apple.dock.plist"),
        fda_probe_path=str(Path.home() / "Library/Mail"),
        launchd_label="com.pythoninthegrass.restic-backup",
        keep_daily=7,
        keep_weekly=4,
        keep_monthly=12,
        prune_weekday=6,
        backup_retry_delays="30,120,300",
    )
    base.update(overrides)
    return main.Settings(**base)


class TestLoadSettings:
    def test_defaults_when_no_env(self, monkeypatch, tmp_path):
        for key in main.ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.chdir(tmp_path)
        settings = main.load_settings(env_path=Path("/nonexistent/.env"))
        assert settings.restic_repository == "sftp:syn:/shares/Mac/Backup"
        assert settings.restic_password == ""
        assert settings.restic_compression == "fastest"
        assert settings.backup_tag == "scheduled"
        assert settings.backup_host == "my-mac"
        assert settings.exclude_if_present == ".nobackup"
        assert settings.check_read_data_subset == "5%"
        assert settings.launchd_label == "com.pythoninthegrass.restic-backup"
        assert settings.keep_daily == 7
        assert settings.prune_weekday == 6
        assert settings.backup_retry_delays == "30,120,300"

    def test_expands_home_in_path_settings(self, monkeypatch):
        monkeypatch.setenv("BACKUP_ROOT", "$HOME/some/dir")
        monkeypatch.setenv("MANIFEST_DIR", "~/manifests")
        settings = main.load_settings(env_path=Path("/nonexistent/.env"))
        assert settings.backup_root == str(Path.home() / "some/dir")
        assert settings.manifest_dir == str(Path.home() / "manifests")
        assert "$HOME" not in settings.backup_root
        assert "~" not in settings.manifest_dir

    def test_resolves_relative_exclude_file_against_cwd(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("EXCLUDE_FILE", "./excludes.txt")
        settings = main.load_settings(env_path=Path("/nonexistent/.env"))
        assert settings.exclude_file == str(tmp_path / "excludes.txt")

    def test_default_exclude_file_is_under_cwd(self, monkeypatch, tmp_path):
        monkeypatch.delenv("EXCLUDE_FILE", raising=False)
        monkeypatch.chdir(tmp_path)
        settings = main.load_settings(env_path=Path("/nonexistent/.env"))
        assert settings.exclude_file == str(tmp_path / "excludes.txt")

    def test_default_env_path_is_cwd_env(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("DOTENV_FILE", raising=False)
        (tmp_path / ".env").write_text("BACKUP_TAG=from-cwd-env\n")
        settings = main.load_settings()
        assert settings.backup_tag == "from-cwd-env"

    def test_dotenv_file_env_var_overrides_default_location(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        other_env = tmp_path / "other" / ".env"
        other_env.parent.mkdir()
        other_env.write_text("BACKUP_TAG=from-dotenv-file\n")
        monkeypatch.setenv("DOTENV_FILE", str(other_env))
        settings = main.load_settings()
        assert settings.backup_tag == "from-dotenv-file"

    def test_env_override_of_formerly_hardcoded_constants(self, monkeypatch):
        monkeypatch.setenv("BACKUP_TAG", "manual")
        monkeypatch.setenv("BACKUP_HOST", "other-host")
        monkeypatch.setenv("CHECK_READ_DATA_SUBSET", "10%")
        monkeypatch.setenv("LAUNCHD_LABEL", "com.example.other")
        settings = main.load_settings(env_path=Path("/nonexistent/.env"))
        assert settings.backup_tag == "manual"
        assert settings.backup_host == "other-host"
        assert settings.check_read_data_subset == "10%"
        assert settings.launchd_label == "com.example.other"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("RESTIC_REPOSITORY", "sftp:other:/path")
        monkeypatch.setenv("KEEP_DAILY", "3")
        monkeypatch.setenv("BACKUP_RETRY_DELAYS", "10,20")
        settings = main.load_settings(env_path=Path("/nonexistent/.env"))
        assert settings.restic_repository == "sftp:other:/path"
        assert settings.keep_daily == 3
        assert settings.backup_retry_delays == "10,20"


class TestResticGlobalArgs:
    def test_includes_sftp_command_and_password_command(self):
        settings = make_settings()
        args = main.restic_global_args(settings)
        assert "--repo" in args
        assert "sftp:syn:/shares/Mac/Backup" in args
        assert "-o" in args
        idx = args.index("-o")
        assert args[idx + 1] == "sftp.command=ssh -T -o RequestTTY=no syn -s sftp"
        assert "--password-command" in args
        pc_idx = args.index("--password-command")
        assert args[pc_idx + 1] == settings.restic_password_command
        assert "--compression" in args
        comp_idx = args.index("--compression")
        assert args[comp_idx + 1] == "fastest"

    def test_omits_password_command_when_restic_password_is_set(self):
        """A direct RESTIC_PASSWORD should let restic fall back to its own
        native env-var lookup rather than forcing --password-command, which
        restic would otherwise always prefer over the env var."""
        settings = make_settings(restic_password="hunter2")
        args = main.restic_global_args(settings)
        assert "--password-command" not in args
        assert "hunter2" not in args  # never put the literal password on argv

    def test_uses_password_command_when_restic_password_is_empty(self):
        settings = make_settings(restic_password="")
        args = main.restic_global_args(settings)
        assert "--password-command" in args


class TestBackupArgs:
    def test_backup_args_normal(self):
        settings = make_settings()
        args = main.backup_args(settings, dry_run=False)
        assert args[0] == "backup"
        assert settings.backup_root in args
        assert "--exclude-file" in args
        assert "--exclude-caches" in args
        assert "--exclude-if-present" in args
        ei_idx = args.index("--exclude-if-present")
        assert args[ei_idx + 1] == ".nobackup"
        assert "--one-file-system" in args
        assert "--tag" in args
        tag_idx = args.index("--tag")
        assert args[tag_idx + 1] == "scheduled"
        assert "--host" in args
        host_idx = args.index("--host")
        assert args[host_idx + 1] == "my-mac"
        assert "--dry-run" not in args

    def test_backup_args_dry_run(self):
        settings = make_settings()
        args = main.backup_args(settings, dry_run=True)
        assert "--dry-run" in args


class TestForgetArgs:
    def test_forget_args_has_keep_policy_and_prune(self):
        settings = make_settings()
        args = main.forget_args(settings)
        assert args[0] == "forget"
        assert "--keep-daily" in args
        assert args[args.index("--keep-daily") + 1] == "7"
        assert "--keep-weekly" in args
        assert args[args.index("--keep-weekly") + 1] == "4"
        assert "--keep-monthly" in args
        assert args[args.index("--keep-monthly") + 1] == "12"
        assert "--prune" in args


class TestShouldPrune:
    def test_true_on_prune_weekday(self):
        settings = make_settings(prune_weekday=6)
        sunday = datetime.date(2026, 10, 25)
        assert sunday.weekday() == 6
        assert main.should_prune(sunday, settings) is True

    def test_false_on_other_weekday(self):
        settings = make_settings(prune_weekday=6)
        monday = datetime.date(2026, 10, 26)
        assert monday.weekday() == 0
        assert main.should_prune(monday, settings) is False


class TestParseDockApps:
    SAMPLE = """(
    {
        "GUID" = 4536715352;
        "tile-data" = {
            "bundle-identifier" = "com.googlecode.iterm2";
            "file-data" = {
                "_CFURLString" = "file:///Applications/iTerm.app/";
                "_CFURLStringType" = 15;
            };
            "file-label" = "iTerm";
        };
        "tile-type" = "file-tile";
    },
    {
        "GUID" = 4536715353;
        "tile-data" = {
            "bundle-identifier" = "com.apple.mail";
            "file-data" = {
                "_CFURLString" = "file:///System/Applications/Mail.app/";
                "_CFURLStringType" = 15;
            };
            "file-label" = "Mail";
        };
        "tile-type" = "file-tile";
    }
)"""

    def test_extracts_app_paths(self):
        apps = main.parse_dock_apps(self.SAMPLE)
        assert apps == [
            "/Applications/iTerm.app/",
            "/System/Applications/Mail.app/",
        ]

    def test_empty_input_returns_empty_list(self):
        assert main.parse_dock_apps("") == []


class TestParseJsonLine:
    def test_parses_valid_json_line(self):
        line = '{"message_type": "status", "percent_done": 0.5}\n'
        assert main.parse_json_line(line) == {"message_type": "status", "percent_done": 0.5}

    def test_returns_none_for_plain_text_line(self):
        assert main.parse_json_line("signal interrupt received, cleaning up\n") is None

    def test_returns_none_for_blank_line(self):
        assert main.parse_json_line("   \n") is None

    def test_returns_none_for_empty_string(self):
        assert main.parse_json_line("") is None


class TestFormatStatus:
    def test_includes_file_counts_and_byte_sizes(self):
        status = {
            "message_type": "status",
            "percent_done": 0.10758628722976203,
            "total_files": 490383,
            "files_done": 49429,
            "total_bytes": 122220693683,
            "bytes_done": 13149270656,
        }
        text = main.format_status(status)
        assert "49,429" in text
        assert "490,383" in text
        assert "GB" in text

    def test_handles_missing_fields_without_raising(self):
        text = main.format_status({"message_type": "status"})
        assert "0" in text


class TestFormatBackupSummary:
    def test_includes_counts_and_error_total(self):
        summary = {
            "message_type": "summary",
            "files_new": 120,
            "files_changed": 4,
            "files_unmodified": 490259,
            "data_added": 13149270656,
            "total_duration": 92.4,
        }
        text = main.format_backup_summary(summary, error_count=2)
        assert "120" in text
        assert "2 error" in text

    def test_no_errors_omits_error_clause_or_reports_zero(self):
        summary = {"message_type": "summary", "files_new": 0, "files_changed": 0, "data_added": 0}
        text = main.format_backup_summary(summary, error_count=0)
        assert "0 error" in text or "no error" in text.lower()

    def test_handles_none_summary(self):
        text = main.format_backup_summary(None, error_count=1)
        assert "1 error" in text

    def test_dry_run_wording_says_dry_run_not_backup(self):
        summary = {"files_new": 1, "files_changed": 0, "data_added": 0, "total_duration": 1.0}
        text = main.format_backup_summary(summary, error_count=0, dry_run=True)
        assert "dry run" in text.lower() or "dry-run" in text.lower()
        assert "backup complete" not in text.lower()

    def test_non_dry_run_wording_says_backup(self):
        summary = {"files_new": 1, "files_changed": 0, "data_added": 0, "total_duration": 1.0}
        text = main.format_backup_summary(summary, error_count=0, dry_run=False)
        assert "dry run" not in text.lower() and "dry-run" not in text.lower()


class TestIsPartialBackupExit:
    def test_exit_code_3_is_partial_not_fatal(self):
        assert main.is_partial_backup_exit(3) is True

    def test_other_nonzero_codes_are_fatal(self):
        assert main.is_partial_backup_exit(1) is False
        assert main.is_partial_backup_exit(130) is False
        assert main.is_partial_backup_exit(0) is False


class TestIsRetryableBackupError:
    def test_generic_fatal_exit_code_is_retryable(self):
        """Exit 1 is restic's generic fatal error. Preflight already proved ssh
        reachability and FDA before the backup started, so a mid-backup exit 1
        is presumed to be a dropped connection, not a config problem."""
        assert main.is_retryable_backup_error(1) is True

    def test_config_and_runtime_errors_are_not_retryable(self):
        assert main.is_retryable_backup_error(2) is False  # Go runtime error
        assert main.is_retryable_backup_error(10) is False  # repo missing
        assert main.is_retryable_backup_error(11) is False  # lock failed
        assert main.is_retryable_backup_error(12) is False  # wrong password

    def test_partial_success_and_cancellation_are_not_retryable(self):
        assert main.is_retryable_backup_error(3) is False
        assert main.is_retryable_backup_error(130) is False


class TestParseRetryDelays:
    def test_parses_comma_separated_seconds(self):
        assert main.parse_retry_delays("30,120,300") == [30, 120, 300]

    def test_single_value(self):
        assert main.parse_retry_delays("60") == [60]

    def test_strips_whitespace(self):
        assert main.parse_retry_delays(" 30, 120 , 300 ") == [30, 120, 300]


class TestFormatErrorNotice:
    def test_includes_count_timestamp_and_log_file(self):
        text = main.format_error_notice(3, "2026-10-20T16:36:29-05:00", "/Users/me/Library/Logs/macos_backup.log")
        assert "3 error" in text
        assert "2026-10-20T16:36:29-05:00" in text
        assert "/Users/me/Library/Logs/macos_backup.log" in text

    def test_singular_wording_for_one_error(self):
        text = main.format_error_notice(1, "2026-10-20T16:36:29-05:00", "/tmp/x.log")
        assert "1 error " in text or text.rstrip().endswith("1 error") is False
        assert "1 errors" not in text


class TestPushTopFile:
    def test_keeps_only_largest_n(self):
        heap = []
        for size, name in [(10, "a"), (50, "b"), (5, "c"), (100, "d"), (20, "e")]:
            main.push_top_file(heap, size, name, f"/x/{name}", limit=3)
        sizes = sorted((s for s, _, _ in heap), reverse=True)
        assert sizes == [100, 50, 20]

    def test_respects_limit_with_fewer_items_than_limit(self):
        heap = []
        main.push_top_file(heap, 10, "a", "/x/a", limit=5)
        main.push_top_file(heap, 20, "b", "/x/b", limit=5)
        assert len(heap) == 2


class TestAccumulateDirSize:
    def test_adds_size_to_every_ancestor_up_to_root(self):
        dir_sizes = {}
        main.accumulate_dir_size(dir_sizes, "/home/u/proj/src/a.py", 100, "/home/u/proj")
        main.accumulate_dir_size(dir_sizes, "/home/u/proj/src/b.py", 50, "/home/u/proj")
        assert dir_sizes["/home/u/proj/src"] == 150
        assert dir_sizes["/home/u/proj"] == 150
        assert "/home/u" not in dir_sizes  # stops at root, doesn't climb above it

    def test_separate_subtrees_are_independent(self):
        dir_sizes = {}
        main.accumulate_dir_size(dir_sizes, "/root/a/x.py", 10, "/root")
        main.accumulate_dir_size(dir_sizes, "/root/b/y.py", 20, "/root")
        assert dir_sizes["/root/a"] == 10
        assert dir_sizes["/root/b"] == 20
        assert dir_sizes["/root"] == 30


class TestTopEntriesTableRows:
    def test_merges_files_and_dirs_sorted_by_size_desc(self):
        top_files = [(100, "big.bin", "/root/big.bin"), (10, "small.txt", "/root/small.txt")]
        dir_sizes = {"/root/dirA": 500, "/root/dirB": 5}
        rows = main.top_entries_table_rows(top_files, dir_sizes, limit=10)
        sizes = [row[2] for row in rows]
        assert sizes == sorted(sizes, reverse=True)
        assert rows[0][2] == 500

    def test_respects_limit(self):
        top_files = [(i, f"f{i}", f"/root/f{i}") for i in range(20)]
        rows = main.top_entries_table_rows(top_files, {}, limit=10)
        assert len(rows) == 10


class FakeProcess:
    def __init__(self, pid=12345):
        self.waited = 0
        self.pid = pid

    def wait(self):
        self.waited += 1


class TestHandleTerminationSignal:
    def test_forwards_exact_signal_to_current_process_pid(self, monkeypatch):
        import signal

        fake_process = FakeProcess(pid=999)
        monkeypatch.setattr(main, "_current_process", fake_process)
        kill_calls = []
        monkeypatch.setattr(main.os, "kill", lambda pid, sig: kill_calls.append((pid, sig)))

        with pytest.raises(KeyboardInterrupt):
            main._handle_termination_signal(signal.SIGTERM, None)

        assert kill_calls == [(999, signal.SIGTERM)]

    def test_no_process_running_still_raises_without_erroring(self, monkeypatch):
        import signal

        monkeypatch.setattr(main, "_current_process", None)
        with pytest.raises(KeyboardInterrupt):
            main._handle_termination_signal(signal.SIGINT, None)

    def test_a_dead_process_pid_does_not_prevent_the_raise(self, monkeypatch):
        import signal

        fake_process = FakeProcess(pid=999)
        monkeypatch.setattr(main, "_current_process", fake_process)

        def raise_no_such_process(pid, sig):
            raise ProcessLookupError

        monkeypatch.setattr(main.os, "kill", raise_no_such_process)
        with pytest.raises(KeyboardInterrupt):
            main._handle_termination_signal(signal.SIGTERM, None)


class TestRunWithInterruptDrain:
    def test_always_disables_tty_out_and_bg_exc(self):
        """Regression test: sh allocates a pty for stdout by default, which makes
        restic interleave ANSI clear-line sequences into --json output even when
        nothing is wrong, corrupting it as a JSON parse target (see git history)."""
        captured_kwargs = {}

        def fake_command(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return FakeProcess()

        import logging

        logger = logging.getLogger("test")
        main.run_with_interrupt_drain(fake_command, "snapshots", logger=logger)
        assert captured_kwargs["_tty_out"] is False
        assert captured_kwargs["_bg_exc"] is False
        assert captured_kwargs["_bg"] is True

    def test_out_handler_sets_out_bufsize(self):
        captured_kwargs = {}

        def fake_command(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return FakeProcess()

        import logging

        logger = logging.getLogger("test")
        main.run_with_interrupt_drain(fake_command, "backup", logger=logger, out_handler=lambda line: None)
        assert captured_kwargs["_out_bufsize"] == 1
        assert "_out" in captured_kwargs

    def test_tracks_current_process_for_the_signal_handler(self):
        """The signal handler needs _current_process set while a restic
        subprocess is running, and cleared after, so a signal arriving outside
        any restic call doesn't try to forward it to a stale/finished process."""
        seen_during_wait = []

        class TrackingFakeProcess(FakeProcess):
            def wait(self):
                seen_during_wait.append(main._current_process)
                super().wait()

        def fake_command(*args, **kwargs):
            return TrackingFakeProcess()

        import logging

        logger = logging.getLogger("test")
        assert main._current_process is None
        main.run_with_interrupt_drain(fake_command, "snapshots", logger=logger)
        assert seen_during_wait[0] is not None
        assert main._current_process is None


def make_error_return_code(exit_code: int):
    """sh dynamically subclasses ErrorReturnCode per exit code internally; build
    an equivalent instance here since sh doesn't expose that factory publicly."""
    import sh

    cls = type(f"ErrorReturnCode_{exit_code}", (sh.ErrorReturnCode,), {"exit_code": exit_code})
    return cls(b"restic restore", b"", b"boom")


class TestCmdPassthrough:
    def test_streams_stdout_and_prints_success(self, monkeypatch):
        import io
        import logging
        from rich.console import Console

        def fake_command(*args, **kwargs):
            kwargs["_out"]("restoring snapshot abc123 ...")
            return FakeProcess()

        monkeypatch.setattr(main, "make_restic", lambda settings: fake_command)
        settings = make_settings()
        buf = io.StringIO()
        console = Console(file=buf, force_terminal=False)
        logger = logging.getLogger("test")

        main.cmd_passthrough(settings, console, logger, ["restore", "latest", "--target", "/tmp/x"])

        output = buf.getvalue()
        assert "restoring snapshot abc123" in output
        assert "restic restore latest --target /tmp/x complete" in output

    def test_fatal_error_exits_with_restic_exit_code(self, monkeypatch):
        import io
        import logging
        from rich.console import Console

        def fake_command(*args, **kwargs):
            raise make_error_return_code(12)

        monkeypatch.setattr(main, "make_restic", lambda settings: fake_command)
        settings = make_settings()
        console = Console(file=io.StringIO(), force_terminal=False)
        logger = logging.getLogger("test")

        with pytest.raises(SystemExit) as exc_info:
            main.cmd_passthrough(settings, console, logger, ["unlock"])
        assert exc_info.value.code == 12


class TestRenderLaunchdPlist:
    def test_renders_valid_plist_with_substituted_values(self):
        settings = make_settings(launchd_label="com.example.test", log_file="/tmp/test.log")
        rendered = main.render_launchd_plist(
            settings,
            program="/Users/me/.local/bin/mb",
            working_directory="/Users/me/git/macos_backup",
            path_env="/opt/uv/bin:/usr/bin:/bin",
        )
        parsed = plistlib.loads(rendered.encode())
        assert parsed["Label"] == "com.example.test"
        assert parsed["StandardOutPath"] == "/tmp/test.log"
        assert parsed["StandardErrorPath"] == "/tmp/test.log"
        assert parsed["ProgramArguments"] == ["/Users/me/.local/bin/mb", "backup"]
        assert parsed["WorkingDirectory"] == "/Users/me/git/macos_backup"
        assert parsed["EnvironmentVariables"]["PATH"] == "/opt/uv/bin:/usr/bin:/bin"
        assert parsed["StartCalendarInterval"]["Hour"] == 12
        assert parsed["StartCalendarInterval"]["Minute"] == 30

    def test_no_leftover_template_placeholders(self):
        settings = make_settings()
        rendered = main.render_launchd_plist(
            settings, program="/Users/me/.local/bin/mb", working_directory="/tmp", path_env="/usr/bin"
        )
        assert "{{" not in rendered
        assert "}}" not in rendered


class TestBuildLaunchdPathEnv:
    def test_includes_mise_shims_and_system_dirs(self):
        path_env = main.build_launchd_path_env()
        parts = path_env.split(":")
        assert str(Path.home() / ".local/share/mise/shims") in parts
        assert "/usr/bin" in parts
        assert "/bin" in parts
