"""Tests for session_manager.py."""

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token-for-testing")

from vibeaway.session_manager import (
    Session,
    SessionState,
    _encode_project_path,
    _project_dir_matches,
    _is_uuid,
    _truncate,
    find_session,
    format_session_list,
    load_session_state,
    read_last_interaction,
)
from datetime import datetime, timedelta


# ─── _is_uuid ─────────────────────────────────────────────────────────────────

class TestIsUuid:
    def test_valid_uuid(self):
        assert _is_uuid("a1b2c3d4-e5f6-7890-abcd-ef1234567890") is True

    def test_uppercase_uuid(self):
        assert _is_uuid("A1B2C3D4-E5F6-7890-ABCD-EF1234567890") is True

    def test_not_uuid(self):
        assert _is_uuid("non-un-uuid") is False

    def test_empty_string(self):
        assert _is_uuid("") is False

    def test_uuid_no_dashes(self):
        assert _is_uuid("a1b2c3d4e5f67890abcdef1234567890") is False


# ─── _truncate ────────────────────────────────────────────────────────────────

class TestTruncate:
    def test_short_text(self):
        assert _truncate("ciao", max_len=10) == "ciao"

    def test_long_text(self):
        result = _truncate("a" * 100, max_len=10)
        assert len(result) == 11  # 10 + "…"
        assert result.endswith("…")

    def test_collapses_whitespace(self):
        assert _truncate("hello   world  test") == "hello world test"

    def test_exact_length(self):
        text = "a" * 60
        assert _truncate(text, max_len=60) == text


# ─── _encode_project_path ────────────────────────────────────────────────────

class TestEncodeProjectPath:
    def test_simple_path(self):
        assert _encode_project_path("/home/user/project") == "-home-user-project"

    def test_windows_path(self):
        result = _encode_project_path("C:\\Users\\test\\project")
        assert result == "C--Users-test-project"

    def test_path_with_spaces(self):
        result = _encode_project_path("/home/user/my project")
        assert " " not in result
        assert result == "-home-user-my-project"

    def test_alphanumeric_preserved(self):
        assert _encode_project_path("abc123") == "abc123"


# ─── _project_dir_matches ────────────────────────────────────────────────────

class TestProjectDirMatches:
    def test_exact_match(self):
        encoded = _encode_project_path("/home/user/project")
        assert _project_dir_matches(encoded, "/home/user/project") is True

    def test_no_match(self):
        encoded = _encode_project_path("/home/user/other")
        assert _project_dir_matches(encoded, "/home/user/project") is False

    def test_case_insensitive(self):
        encoded = _encode_project_path("C:\\Users\\Test")
        assert _project_dir_matches(encoded.upper(), "C:\\Users\\Test") is True


# ─── Session dataclass ───────────────────────────────────────────────────────

class TestSession:
    def _make_session(self, **kwargs):
        defaults = dict(
            session_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            title="Test session",
            project_dir="/home/user/project",
            last_modified=datetime.now(),
        )
        defaults.update(kwargs)
        return Session(**defaults)

    def test_short_id(self):
        s = self._make_session()
        assert s.short_id == "a1b2c3d4"

    def test_age_str_minutes(self):
        s = self._make_session(last_modified=datetime.now() - timedelta(minutes=5))
        assert "m ago" in s.age_str

    def test_age_str_hours(self):
        s = self._make_session(last_modified=datetime.now() - timedelta(hours=3))
        assert "h ago" in s.age_str

    def test_age_str_days(self):
        s = self._make_session(last_modified=datetime.now() - timedelta(days=2))
        assert "d ago" in s.age_str

    def test_open_badge_not_open(self):
        s = self._make_session(is_open=False)
        assert s.open_badge == ""

    def test_open_badge_exact(self):
        s = self._make_session(is_open=True, open_pid=1234, open_certainty="exact")
        assert "open" in s.open_badge
        assert "1234" in s.open_badge

    def test_open_badge_inferred(self):
        s = self._make_session(is_open=True, open_pid=5678, open_certainty="inferred")
        assert "probably open" in s.open_badge


# ─── format_session_list ─────────────────────────────────────────────────────

class TestFormatSessionList:
    def test_empty_list(self):
        result = format_session_list([])
        assert "No sessions found" in result

    def test_list_with_sessions(self):
        s = Session(
            session_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            title="Test project",
            project_dir="/home/user/project",
            last_modified=datetime.now(),
            message_count=5,
        )
        result = format_session_list([s])
        assert "a1b2c3d4" in result
        assert "Test project" in result
        assert "#1 -" in result  # ordinal

    def test_current_session_marked(self):
        sid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        s = Session(
            session_id=sid,
            title="Test",
            project_dir="/tmp",
            last_modified=datetime.now(),
        )
        result = format_session_list([s], current_session_id=sid)
        assert "bot" in result

    def test_multiple_sessions_numbered(self):
        sessions = [
            Session(
                session_id=f"a1b2c3d4-e5f6-7890-abcd-ef123456789{i}",
                title=f"Session {i}",
                project_dir="/tmp",
                last_modified=datetime.now() - timedelta(hours=i),
            )
            for i in range(3)
        ]
        result = format_session_list(sessions)
        assert "#1 -" in result
        assert "#2 -" in result
        assert "#3 -" in result

    def test_resume_hint(self):
        s = Session(
            session_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            title="Test",
            project_dir="/tmp",
            last_modified=datetime.now(),
        )
        result = format_session_list([s])
        assert "/resume" in result


# ─── find_session by ordinal ─────────────────────────────────────────────────

class TestFindSessionByOrdinal:
    """Test that find_session accepts numeric ordinals."""

    def _make_sessions(self):
        return [
            Session(
                session_id=f"aaaaaaaa-bbbb-cccc-dddd-{i:012d}",
                title=f"Session {i}",
                project_dir="/tmp",
                last_modified=datetime.now() - timedelta(hours=i),
            )
            for i in range(5)
        ]

    def test_find_by_ordinal_1(self):
        from unittest.mock import patch
        sessions = self._make_sessions()
        with patch("vibeaway.session_manager.list_sessions", return_value=sessions):
            result = find_session("1")
            assert result is not None
            assert result.session_id == sessions[0].session_id

    def test_find_by_ordinal_3(self):
        from unittest.mock import patch
        sessions = self._make_sessions()
        with patch("vibeaway.session_manager.list_sessions", return_value=sessions):
            result = find_session("3")
            assert result is not None
            assert result.session_id == sessions[2].session_id

    def test_find_by_ordinal_out_of_range(self):
        from unittest.mock import patch
        sessions = self._make_sessions()
        with patch("vibeaway.session_manager.list_sessions", return_value=sessions):
            result = find_session("99")
            assert result is None

    def test_find_by_title(self):
        from unittest.mock import patch
        sessions = self._make_sessions()
        with patch("vibeaway.session_manager.list_sessions", return_value=sessions):
            result = find_session("Session 2")
            assert result is not None
            assert "Session 2" in result.title

    def test_find_by_uuid_prefix(self):
        from unittest.mock import patch
        sessions = self._make_sessions()
        with patch("vibeaway.session_manager.list_sessions", return_value=sessions):
            result = find_session("aaaaaaaa")
            assert result is not None


class TestSessionStateWrappers:
    def test_load_session_state_delegates_to_backend(self):
        from unittest.mock import patch

        expected = SessionState(prompt="hi", response="hello", usage={"session_id": "abc"})
        with patch("vibeaway.session_manager._CLAUDE_BACKEND.load_session_state", return_value=expected):
            result = load_session_state("abc", "/tmp")
        assert result is expected

    def test_read_last_interaction_delegates_to_backend(self):
        from unittest.mock import patch

        with patch(
            "vibeaway.session_manager._CLAUDE_BACKEND.read_last_interaction",
            return_value=("prompt", "response"),
        ):
            prompt, response = read_last_interaction("/tmp", session_id="abc")

        assert prompt == "prompt"
        assert response == "response"
