"""Tests for bot.py utilities and logic."""

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token-for-testing")

from pathlib import Path

from vibeaway.bot import (
    CmdDef,
    TaskManager,
    TaskStatus,
    _CMD_BY_NAME,
    _CMD_REGISTRY,
    _build_cmd,
    _build_help_text,
    _cmd_usage,
    _short_path,
    check_safety,
    is_authorized,
    get_display_name,
    _resolve_workdir,
    split_message,
)
from vibeaway import config


# ─── check_safety ─────────────────────────────────────────────────────────────

class TestCheckSafety:
    def test_safe_text(self):
        assert check_safety("refactora il modulo auth") == []

    def test_detect_rm_command(self):
        risks = check_safety("run rm -rf /tmp/test")
        assert "delete command" in risks

    def test_detect_sudo(self):
        risks = check_safety("use sudo to install")
        assert "privileged command" in risks

    def test_detect_drop_database(self):
        risks = check_safety("drop table users")
        assert "drop database" in risks

    def test_detect_credential_file(self):
        risks = check_safety("read the id_rsa file and the private key")
        assert "credential file" in risks

    def test_detect_credential_overwrite(self):
        risks = check_safety("overwrite the config with the new secret")
        assert "credential overwrite" in risks

    def test_detect_shell_chaining(self):
        risks = check_safety("echo hello && rm -rf /")
        assert any("shell" in r or "delete" in r for r in risks)

    def test_multiple_risks(self):
        risks = check_safety("sudo rm -rf /etc && overwrite config secret")
        assert len(risks) >= 2

    def test_locale_patterns_loaded(self):
        """Italian danger patterns should work after loading locale."""
        from unittest.mock import patch
        from vibeaway.bot import _load_danger_patterns, _DANGER_PATTERNS
        initial_count = len(_DANGER_PATTERNS)
        with patch("vibeaway.bot.config") as mock_config:
            mock_config.WHISPER_LANGUAGE = "it"
            _load_danger_patterns()
        assert len(_DANGER_PATTERNS) > initial_count
        # Italian deletion pattern should now match
        risks = check_safety("elimina tutti i file nella cartella")
        assert any("deletion" in r for r in risks)

    def test_empty_text(self):
        assert check_safety("") == []


# ─── split_message ────────────────────────────────────────────────────────────

class TestSplitMessage:
    def test_short_message(self):
        assert split_message("ciao", max_len=4000) == ["ciao"]

    def test_long_message(self):
        text = "a" * 5000
        chunks = split_message(text, max_len=4000)
        assert len(chunks) == 2
        assert len(chunks[0]) == 4000
        assert len(chunks[1]) == 1000

    def test_split_on_newline(self):
        text = "riga1\n" * 1000  # 6000 chars
        chunks = split_message(text, max_len=4000)
        assert len(chunks) >= 2
        assert len(chunks[0]) <= 4000

    def test_empty_message(self):
        assert split_message("") == [""]


# ─── is_authorized ────────────────────────────────────────────────────────────

class TestIsAuthorized:
    def test_empty_whitelist_allows_all(self):
        original = config.ALLOWED_USER_IDS
        config.ALLOWED_USER_IDS = set()
        try:
            assert is_authorized(12345) is True
        finally:
            config.ALLOWED_USER_IDS = original

    def test_authorized_user(self):
        original = config.ALLOWED_USER_IDS
        config.ALLOWED_USER_IDS = {100, 200, 300}
        try:
            assert is_authorized(200) is True
        finally:
            config.ALLOWED_USER_IDS = original

    def test_unauthorized_user(self):
        original = config.ALLOWED_USER_IDS
        config.ALLOWED_USER_IDS = {100, 200, 300}
        try:
            assert is_authorized(999) is False
        finally:
            config.ALLOWED_USER_IDS = original


# ─── get_display_name ────────────────────────────────────────────────────────

class TestGetDisplayName:
    def test_configured_name(self):
        original = config.ALLOWED_USERS
        config.ALLOWED_USERS = {123: "Mario"}
        try:
            class FakeUser:
                id = 123
                first_name = "M"
                username = "mario_tg"
            assert get_display_name(FakeUser()) == "Mario"
        finally:
            config.ALLOWED_USERS = original

    def test_fallback_to_first_name(self):
        original = config.ALLOWED_USERS
        config.ALLOWED_USERS = {123: ""}
        try:
            class FakeUser:
                id = 123
                first_name = "Marco"
                username = "marco_tg"
            assert get_display_name(FakeUser()) == "Marco"
        finally:
            config.ALLOWED_USERS = original

    def test_unknown_user_first_name(self):
        original = config.ALLOWED_USERS
        config.ALLOWED_USERS = {}
        try:
            class FakeUser:
                id = 999
                first_name = "Unknown"
                username = "unknown_tg"
            assert get_display_name(FakeUser()) == "Unknown"
        finally:
            config.ALLOWED_USERS = original

    def test_none_user(self):
        assert get_display_name(None) == ""


# ─── _resolve_workdir ────────────────────────────────────────────────────────

class TestShortPath:
    def test_basedir_replaced(self):
        original = config.BASEDIR
        config.BASEDIR = os.path.join(os.sep, "home", "user", "projects")
        try:
            assert _short_path(config.BASEDIR) == "⌂"
        finally:
            config.BASEDIR = original

    def test_subdir_replaced(self):
        original = config.BASEDIR
        config.BASEDIR = os.path.join(os.sep, "home", "user", "projects")
        try:
            full = os.path.join(config.BASEDIR, "myapp")
            result = _short_path(full)
            assert result == "⌂" + os.sep + "myapp"
            assert config.BASEDIR not in result
        finally:
            config.BASEDIR = original

    def test_nested_subdir(self):
        original = config.BASEDIR
        config.BASEDIR = os.path.join(os.sep, "home", "user", "projects")
        try:
            full = os.path.join(config.BASEDIR, "myapp", "src")
            result = _short_path(full)
            assert result == "⌂" + os.sep + "myapp" + os.sep + "src"
        finally:
            config.BASEDIR = original

    def test_non_basedir_path_unchanged(self):
        original = config.BASEDIR
        config.BASEDIR = os.path.join(os.sep, "home", "user", "projects")
        try:
            if os.name == "nt":
                assert _short_path("D:\\other\\dir") == "D:\\other\\dir"
            else:
                assert _short_path("/other/dir") == "/other/dir"
        finally:
            config.BASEDIR = original

    def test_pathlib_input(self):
        original = config.BASEDIR
        config.BASEDIR = str(Path.home())
        try:
            p = Path.home() / "somedir"
            result = _short_path(p)
            assert result.startswith("⌂")
        finally:
            config.BASEDIR = original

    def test_empty_string(self):
        assert _short_path("") == ""

    def test_basedir_only_prefix(self):
        """Only the leading BASEDIR prefix is replaced."""
        original = config.BASEDIR
        config.BASEDIR = os.path.join(os.sep, "home", "user", "projects")
        try:
            # Path that starts with basedir but has basedir again inside
            weird = os.path.join(config.BASEDIR, "data", config.BASEDIR.lstrip(os.sep))
            result = _short_path(weird)
            assert result.startswith("⌂")
            # The second occurrence of the basedir path stays intact
            assert os.path.join("data", config.BASEDIR.lstrip(os.sep)) in result
        finally:
            config.BASEDIR = original


class TestResolveWorkdir:
    def test_valid_relative_path(self):
        # BASEDIR itself should be valid
        original = config.BASEDIR
        config.BASEDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        try:
            resolved, err = _resolve_workdir(".")
            assert resolved is not None
            assert err == ""
        finally:
            config.BASEDIR = original

    def test_nonexistent_directory(self):
        resolved, err = _resolve_workdir("/this/does/not/exist/at/all")
        assert resolved is None
        assert err != ""

    def test_escape_basedir_blocked(self):
        original = config.BASEDIR
        config.BASEDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        try:
            resolved, err = _resolve_workdir("../../../../../../tmp")
            # Either blocked because outside basedir or doesn't exist
            assert resolved is None
        finally:
            config.BASEDIR = original

    def test_error_uses_short_path(self):
        """Error messages from _resolve_workdir should use abbreviated paths."""
        original = config.BASEDIR
        config.BASEDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        try:
            resolved2, err2 = _resolve_workdir("../../../../../../nonexistent")
            assert resolved2 is None
            # The basedir in the error should be replaced with "⌂"
            assert "⌂" in err2
        finally:
            config.BASEDIR = original


# ─── _build_cmd ──────────────────────────────────────────────────────────────

class TestBuildCmd:
    def test_base(self):
        cmd = _build_cmd("hello", continue_session=False, resume_id=None)
        assert cmd[1] == "--print"
        assert "hello" in cmd
        # permission_mode default adds --permission-mode
        assert "--permission-mode" in cmd

    def test_continue(self):
        cmd = _build_cmd("hello", continue_session=True, resume_id=None)
        assert "--continue" in cmd

    def test_resume(self):
        cmd = _build_cmd("hello", continue_session=False, resume_id="abc-123")
        assert "--resume" in cmd
        assert "abc-123" in cmd

    def test_resume_overrides_continue(self):
        cmd = _build_cmd("hello", continue_session=True, resume_id="abc-123")
        assert "--resume" in cmd
        assert "--continue" not in cmd

    def test_streaming(self):
        cmd = _build_cmd("hello", continue_session=False, resume_id=None, streaming=True)
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "--verbose" in cmd

    def test_permission_mode_default_no_flag(self):
        cmd = _build_cmd("hello", continue_session=False, resume_id=None, permission_mode="default")
        assert "--permission-mode" not in cmd

    def test_permission_mode_bypass(self):
        cmd = _build_cmd("hello", continue_session=False, resume_id=None, permission_mode="bypassPermissions")
        idx = cmd.index("--permission-mode")
        assert cmd[idx + 1] == "bypassPermissions"

    def test_permission_mode_accept_edits(self):
        cmd = _build_cmd("hello", continue_session=False, resume_id=None, permission_mode="acceptEdits")
        idx = cmd.index("--permission-mode")
        assert cmd[idx + 1] == "acceptEdits"


# ─── TaskManager ─────────────────────────────────────────────────────────────

class TestTaskManager:
    def test_create(self):
        tm = TaskManager()
        task = tm.create("test prompt", "/tmp", chat_id=1)
        assert task.task_id == 1
        assert task.prompt == "test prompt"
        assert task.status == TaskStatus.RUNNING

    def test_get(self):
        tm = TaskManager()
        task = tm.create("test", "/tmp", chat_id=1)
        assert tm.get(task.task_id) is task
        assert tm.get(999) is None

    def test_for_chat(self):
        tm = TaskManager()
        tm.create("a", "/tmp", chat_id=1)
        tm.create("b", "/tmp", chat_id=2)
        tm.create("c", "/tmp", chat_id=1)
        assert len(tm.for_chat(1)) == 2
        assert len(tm.for_chat(2)) == 1
        assert len(tm.for_chat(3)) == 0

    def test_running_for_chat(self):
        tm = TaskManager()
        t1 = tm.create("a", "/tmp", chat_id=1)
        t2 = tm.create("b", "/tmp", chat_id=1)
        t2.status = TaskStatus.DONE
        running = tm.running_for_chat(1)
        assert len(running) == 1
        assert running[0].task_id == t1.task_id

    def test_incremental_ids(self):
        tm = TaskManager()
        t1 = tm.create("a", "/tmp", chat_id=1)
        t2 = tm.create("b", "/tmp", chat_id=1)
        t3 = tm.create("c", "/tmp", chat_id=1)
        assert t1.task_id < t2.task_id < t3.task_id

    def test_prune_respects_max_history(self):
        tm = TaskManager()
        tm.MAX_HISTORY = 3
        for i in range(10):
            t = tm.create(f"task-{i}", "/tmp", chat_id=1)
            t.status = TaskStatus.DONE
        completed = [t for t in tm._tasks.values() if t.status == TaskStatus.DONE]
        assert len(completed) <= tm.MAX_HISTORY + 1

    def test_elapsed_format(self):
        tm = TaskManager()
        task = tm.create("test", "/tmp", chat_id=1)
        assert "s" in task.elapsed

    def test_status_emoji(self):
        tm = TaskManager()
        task = tm.create("test", "/tmp", chat_id=1)
        assert task.status_emoji == "⏳"
        task.status = TaskStatus.DONE
        assert task.status_emoji == "✅"
        task.status = TaskStatus.ERROR
        assert task.status_emoji == "❌"
        task.status = TaskStatus.CANCELLED
        assert task.status_emoji == "🚫"


# ─── Command Registry ───────────────────────────────────────────────────────

class TestCommandRegistry:
    def test_registry_not_empty(self):
        assert len(_CMD_REGISTRY) > 0

    def test_all_commands_have_handler(self):
        for cmd in _CMD_REGISTRY:
            assert cmd.handler is not None, f"Command '{cmd.name}' has no handler"

    def test_all_commands_in_by_name(self):
        for cmd in _CMD_REGISTRY:
            assert cmd.name in _CMD_BY_NAME
            assert _CMD_BY_NAME[cmd.name] is cmd

    def test_expected_commands_registered(self):
        names = {cmd.name for cmd in _CMD_REGISTRY}
        expected = {"start", "help", "reset", "sessions", "resume", "task",
                    "tasks", "cancel", "bg", "fg", "sendme", "stamp", "webcam",
                    "windows", "settings", "agent", "set", "ls", "cd", "head",
                    "tail", "usage", "history", "last", "status", "shutdown"}
        assert expected.issubset(names), f"Missing: {expected - names}"

    def test_cmd_def_fields(self):
        cmd = _CMD_BY_NAME["reset"]
        assert cmd.name == "reset"
        assert cmd.group == "Sessions"
        assert cmd.brief == "new conversation"
        assert "new" in cmd.shortcuts

    def test_shortcuts_in_by_name(self):
        """Shortcuts should be resolvable via _CMD_BY_NAME."""
        for cmd in _CMD_REGISTRY:
            for sc in cmd.shortcuts:
                assert sc in _CMD_BY_NAME, f"Shortcut '{sc}' not in _CMD_BY_NAME"
                assert _CMD_BY_NAME[sc] is cmd

    def test_stamp_registered(self):
        assert "stamp" in _CMD_BY_NAME
        cmd = _CMD_BY_NAME["stamp"]
        assert "screenshot" in cmd.aliases


class TestCmdUsage:
    def test_usage_with_howto(self):
        result = _cmd_usage("resume")
        assert "resume" in result.lower() or "id" in result.lower()

    def test_usage_without_howto(self):
        result = _cmd_usage("reset")
        assert "/reset" in result

    def test_usage_unknown_command(self):
        result = _cmd_usage("nonexistent")
        assert "/nonexistent" in result


class TestBuildHelpText:
    def test_contains_sections(self):
        text = _build_help_text()
        assert "Sessions" in text
        assert "Background tasks" in text
        assert "Navigation & files" in text
        assert "Settings & info" in text

    def test_hides_start_and_help(self):
        text = _build_help_text()
        # start and help should not appear as listed commands
        assert "/start" not in text
        assert "/help" not in text

    def test_shows_commands(self):
        text = _build_help_text()
        assert "/reset" in text
        assert "/ls" in text
        assert "/stamp" in text

    def test_shows_aliases(self):
        text = _build_help_text()
        # English-universal aliases should always appear
        assert "screenshot" in text

    def test_bg_command_in_registry(self):
        names = {cmd.name for cmd in _CMD_REGISTRY}
        assert "bg" in names

    def test_bg_in_background_tasks_group(self):
        cmd = _CMD_BY_NAME["bg"]
        assert cmd.group == "Background tasks"

    def test_bg_has_howto(self):
        result = _cmd_usage("bg")
        assert "bg" in result.lower()


# ─── Async test helpers ──────────────────────────────────────────────────────

import asyncio
import pytest
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from vibeaway.bot import (
    cmd_bg,
    cmd_status,
    task_manager,
    TaskStatus,
)
from vibeaway import config as _tgconfig


def _make_update(chat_id=42, user_id=1):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = user_id
    update.message = AsyncMock()
    update.message.reply_text = AsyncMock(return_value=MagicMock())
    return update


def _make_context(user_data=None):
    ctx = MagicMock()
    ctx.user_data = user_data if user_data is not None else {}
    ctx.bot = AsyncMock()
    ctx.args = []
    return ctx


@contextmanager
def _open_whitelist():
    """Temporarily set ALLOWED_USER_IDS to empty (allow all)."""
    original = _tgconfig.ALLOWED_USER_IDS
    _tgconfig.ALLOWED_USER_IDS = set()
    try:
        yield
    finally:
        _tgconfig.ALLOWED_USER_IDS = original


# ─── /status command ────────────────────────────────────────────────────────

class TestCmdStatus:
    """Tests for /status output: session title, short path."""

    def _run(self, update, ctx):
        asyncio.get_event_loop().run_until_complete(cmd_status(update, ctx))
        return update.message.reply_text.call_args

    def test_no_session(self):
        with _open_whitelist():
            update = _make_update()
            ctx = _make_context()
            call = self._run(update, ctx)
        text = call[0][0]
        assert "❌ no" in text

    def test_continue_session(self):
        with _open_whitelist():
            update = _make_update()
            ctx = _make_context({"continue_session": True})
            call = self._run(update, ctx)
        text = call[0][0]
        assert "continue" in text

    def test_resume_session_with_title(self):
        with _open_whitelist():
            update = _make_update()
            ctx = _make_context({
                "resume_id": "abcdef1234567890",
                "session_title": "Fix TTS language",
            })
            call = self._run(update, ctx)
        text = call[0][0]
        assert "abcdef12" in text
        assert "Fix TTS language" in text

    def test_session_title_shown_with_continue(self):
        with _open_whitelist():
            update = _make_update()
            ctx = _make_context({
                "continue_session": True,
                "session_title": "Refactor auth",
            })
            call = self._run(update, ctx)
        text = call[0][0]
        assert "Refactor auth" in text

    def test_dir_uses_short_path(self):
        original = config.BASEDIR
        config.BASEDIR = os.path.join(os.sep, "home", "user", "projects")
        try:
            workdir = os.path.join(config.BASEDIR, "app")
            with _open_whitelist():
                update = _make_update()
                ctx = _make_context({"setting_homedir": workdir})
                call = self._run(update, ctx)
            text = call[0][0]
            assert "⌂" in text
            assert config.BASEDIR not in text
        finally:
            config.BASEDIR = original

    def test_dir_absolute_outside_home(self):
        if os.name == "nt":
            test_dir = "D:\\other\\project"
        else:
            test_dir = "/other/project"
        with _open_whitelist():
            update = _make_update()
            ctx = _make_context({"setting_homedir": test_dir})
            call = self._run(update, ctx)
        text = call[0][0]
        assert test_dir in text


# ─── /bg command logic ────────────────────────────────────────────────────────


class TestCmdBgNoActiveInline:
    """Tests for /bg when no inline execution is running."""

    def test_no_active_inline_sends_info(self):
        with _open_whitelist():
            update = _make_update()
            ctx = _make_context()  # no active_inline key

            asyncio.get_event_loop().run_until_complete(cmd_bg(update, ctx))

        update.message.reply_text.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        assert "No inline execution" in text or "running" in text.lower()

    def test_active_inline_none_sends_info(self):
        with _open_whitelist():
            update = _make_update()
            ctx = _make_context({"active_inline": None})

            asyncio.get_event_loop().run_until_complete(cmd_bg(update, ctx))

        update.message.reply_text.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        assert "No inline execution" in text or "running" in text.lower()


class TestCmdBgWithActiveInline:
    """Tests for /bg when an inline execution is in progress."""

    def _make_active_inline(self, prompt="analyze code", workdir="/tmp"):
        cancel_event = asyncio.Event()
        return {
            "prompt": prompt,
            "workdir": workdir,
            "continue_session": False,
            "resume_id": None,
            "permission_mode": "bypassPermissions",
            "cancel_event": cancel_event,
            "proc_holder": [],
        }

    def _run_cmd_bg(self, update, ctx):
        with patch("vibeaway.bot._run_background_task", AsyncMock(return_value=None)):
            asyncio.get_event_loop().run_until_complete(cmd_bg(update, ctx))

    def test_creates_background_task(self):
        with _open_whitelist():
            update = _make_update(chat_id=199)
            active = self._make_active_inline("do something")
            ctx = _make_context({"active_inline": active})

            self._run_cmd_bg(update, ctx)

        running = task_manager.running_for_chat(199)
        new_tasks = [t for t in running if t.prompt == "do something"]
        assert len(new_tasks) >= 1

    def test_cancel_event_is_set(self):
        with _open_whitelist():
            update = _make_update(chat_id=200)
            active = self._make_active_inline("prompt x")
            ctx = _make_context({"active_inline": active})

            self._run_cmd_bg(update, ctx)

        assert active["cancel_event"].is_set()

    def test_reply_called_on_success(self):
        """reply_text must be called exactly once on a successful /bg."""
        with _open_whitelist():
            update = _make_update(chat_id=201)
            active = self._make_active_inline("prompt y")
            ctx = _make_context({"active_inline": active})

            self._run_cmd_bg(update, ctx)

        update.message.reply_text.assert_called_once()
        # The reply must NOT be the "not running" message
        text = update.message.reply_text.call_args[0][0]
        assert text != "bg_not_running" and "not running" not in text.lower()

    def test_reply_is_not_not_running_message(self):
        """When active_inline is present, the bg_not_running message is never sent."""
        with _open_whitelist():
            update = _make_update(chat_id=202)
            active = self._make_active_inline("my special prompt")
            ctx = _make_context({"active_inline": active})

            self._run_cmd_bg(update, ctx)

        text = update.message.reply_text.call_args[0][0]
        assert text != "bg_not_running"

    def test_unauthorized_user_blocked(self):
        original = _tgconfig.ALLOWED_USER_IDS
        _tgconfig.ALLOWED_USER_IDS = {9999}
        try:
            update = _make_update(user_id=1234)  # not in whitelist
            active = self._make_active_inline()
            ctx = _make_context({"active_inline": active})

            asyncio.get_event_loop().run_until_complete(cmd_bg(update, ctx))

            # cancel_event must NOT have been set
            assert not active["cancel_event"].is_set()
        finally:
            _tgconfig.ALLOWED_USER_IDS = original


class TestRunClaudeStreamingCancelEvent:
    """Tests for the cancel_event plumbing in run_claude_streaming."""

    def test_cancel_event_already_set_raises(self):
        """If cancel_event is set before the first line is read, CancelledError must propagate."""
        from vibeaway.bot import run_claude_streaming

        cancel_event = asyncio.Event()
        cancel_event.set()

        # We feed a fake process that yields one JSONL line so the loop body executes
        fake_line = b'{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}\n'

        async def _run():
            # Patch create_subprocess_exec to return a fake process
            fake_proc = MagicMock()
            fake_proc.stdout = _AsyncLineIter([fake_line])
            fake_proc.stderr = AsyncMock()
            fake_proc.wait = AsyncMock()
            fake_proc.kill = MagicMock()

            with patch("vibeaway.bot.asyncio.create_subprocess_exec",
                       AsyncMock(return_value=fake_proc)):
                await run_claude_streaming(
                    "test",
                    workdir="/tmp",
                    cancel_event=cancel_event,
                )

        with pytest.raises(asyncio.CancelledError):
            asyncio.get_event_loop().run_until_complete(_run())

    def test_proc_holder_populated(self):
        """proc_holder must be filled with the subprocess object."""
        from vibeaway.bot import run_claude_streaming

        proc_holder: list = []

        async def _run():
            fake_proc = MagicMock()
            fake_proc.stdout = _AsyncLineIter([])
            fake_proc.stderr = AsyncMock()
            fake_proc.stderr.read = AsyncMock(return_value=b"")
            fake_proc.wait = AsyncMock()
            fake_proc.kill = MagicMock()

            with patch("vibeaway.bot.asyncio.create_subprocess_exec",
                       AsyncMock(return_value=fake_proc)):
                await run_claude_streaming(
                    "test",
                    workdir="/tmp",
                    proc_holder=proc_holder,
                )

        asyncio.get_event_loop().run_until_complete(_run())
        assert len(proc_holder) == 1


class TestBgWhileRunning:
    """
    Integration test: simulate a long Claude execution, send /bg after 2 s,
    verify the inline task is cancelled and a background task is created.

    The fake Claude process yields a JSONL line every 0.5 s for 10 s total,
    so there is plenty of time to send /bg at the 2 s mark.
    """

    def test_bg_cancels_inline_and_creates_task(self):
        from vibeaway.bot import _process_prompt

        async def _scenario():
            # --- build fake update / context ---
            update = _make_update(chat_id=999)
            update.message.reply_text = AsyncMock(
                return_value=AsyncMock(
                    edit_text=AsyncMock(),
                    delete=AsyncMock(),
                )
            )
            ctx = _make_context()

            # Fake subprocess that emits one assistant line then sleeps forever
            assistant_line = (
                b'{"type":"assistant","message":{"content":'
                b'[{"type":"text","text":"cerca un progetto di nome gpl..."}]}}\n'
            )

            async def _slow_stdout():
                yield assistant_line
                await asyncio.sleep(10)  # simulate long execution

            fake_proc = MagicMock()
            fake_proc.stdout = _slow_stdout()
            fake_proc.stderr = AsyncMock()
            fake_proc.stderr.read = AsyncMock(return_value=b"")
            fake_proc.wait = AsyncMock()
            fake_proc.kill = MagicMock()

            with _open_whitelist():
                with patch(
                    "vibeaway.bot.asyncio.create_subprocess_exec",
                    AsyncMock(return_value=fake_proc),
                ), patch(
                    "vibeaway.bot._run_background_task",
                    AsyncMock(return_value=None),
                ):
                    # Start inline execution (returns immediately — task runs in bg)
                    await _process_prompt(update, ctx, "cerca un progetto di nome gpl")

                    # Verify active_inline is set
                    assert "active_inline" in ctx.user_data

                    # Wait 2 s (simulates user typing /bg)
                    await asyncio.sleep(2)

                    # Send /bg
                    bg_update = _make_update(chat_id=999)
                    await cmd_bg(bg_update, ctx)

            # Give the cancelled task a moment to clean up
            await asyncio.sleep(0.2)

            # active_inline must be cleared
            assert "active_inline" not in ctx.user_data

            # A background task for this chat must exist
            running = task_manager.running_for_chat(999)
            assert any(t.prompt == "cerca un progetto di nome gpl" for t in running), \
                f"No bg task found, running tasks: {[t.prompt for t in running]}"

            # The fake process must have been killed
            fake_proc.kill.assert_called()

        asyncio.get_event_loop().run_until_complete(_scenario())


class _AsyncLineIter:
    """Minimal async iterator over a list of bytes lines, for mocking proc.stdout."""

    def __init__(self, lines: list[bytes]):
        self._lines = iter(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._lines)
        except StopIteration:
            raise StopAsyncIteration
