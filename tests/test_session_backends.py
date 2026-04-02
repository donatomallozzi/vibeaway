"""Tests for Codex and Copilot session backends."""

import json
import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token-for-testing")

from vibeaway.session_backends import CodexSessionBackend, CopilotSessionBackend


def _write_jsonl(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


class TestCodexSessionBackend:
    def test_list_and_load_state(self, tmp_path, monkeypatch):
        session_id = "019d1f1b-ecf4-7803-be9d-d89139a73cfc"
        sessions_dir = tmp_path / ".codex" / "sessions"
        index_path = tmp_path / ".codex" / "session_index.jsonl"
        jsonl_path = sessions_dir / "2026" / "03" / "24" / f"rollout-2026-03-24T10-10-17-{session_id}.jsonl"

        monkeypatch.setattr(
            CodexSessionBackend,
            "sessions_dir",
            property(lambda self: sessions_dir),
        )
        monkeypatch.setattr(
            CodexSessionBackend,
            "session_index",
            property(lambda self: index_path),
        )

        _write_jsonl(
            index_path,
            [
                {
                    "id": session_id,
                    "thread_name": "Analyze repo state",
                    "updated_at": "2026-03-24T09:24:33.620463Z",
                }
            ],
        )
        _write_jsonl(
            jsonl_path,
            [
                {
                    "type": "session_meta",
                    "payload": {"id": session_id, "cwd": "C:\\repo"},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Analyze repo state"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Sure."}],
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "input_tokens": 11,
                                "output_tokens": 7,
                                "cached_input_tokens": 3,
                            }
                        },
                    },
                },
            ],
        )

        backend = CodexSessionBackend()
        sessions = backend.list_sessions(workdir="C:\\repo", limit=10)

        assert len(sessions) == 1
        assert sessions[0].session_id == session_id
        assert sessions[0].title == "Analyze repo state"
        assert sessions[0].project_dir == "C:\\repo"

        state = backend.load_session_state(session_id, "C:\\repo")
        assert state is not None
        assert state.prompt == "Analyze repo state"
        assert state.response == "Sure."
        assert state.usage["session_id"] == session_id
        assert state.usage["usage"]["input_tokens"] == 11


class TestCopilotSessionBackend:
    def test_list_and_load_state(self, tmp_path, monkeypatch):
        session_id = "f6a3bcd3-8a49-4a7d-a4b4-b20589bcfa54"
        sessions_dir = tmp_path / ".copilot" / "session-state"
        jsonl_path = sessions_dir / f"{session_id}.jsonl"

        monkeypatch.setattr(
            CopilotSessionBackend,
            "sessions_dir",
            property(lambda self: sessions_dir),
        )

        _write_jsonl(
            jsonl_path,
            [
                {
                    "type": "session.start",
                    "data": {"sessionId": session_id},
                },
                {
                    "type": "user.message",
                    "data": {"content": "/cost", "attachments": []},
                },
                {
                    "type": "session.error",
                    "data": {"message": "Execution failed: No model available"},
                },
            ],
        )

        backend = CopilotSessionBackend()
        sessions = backend.list_sessions(limit=10)

        assert len(sessions) == 1
        assert sessions[0].session_id == session_id
        assert sessions[0].title == "/cost"
        assert sessions[0].project_dir == "(unknown)"

        state = backend.load_session_state(session_id, "C:\\repo")
        assert state is not None
        assert state.prompt == "/cost"
        assert "No model available" in state.response
