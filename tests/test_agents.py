"""Tests for multi-agent execution adapters."""

import os
from pathlib import Path

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token-for-testing")

from vibeaway.agents import available_agents, get_agent


class TestAvailableAgents:
    def test_registry_contains_supported_agents(self):
        assert available_agents() == ("claude", "codex", "copilot")


class TestCodexAdapter:
    def test_resolve_executable_supports_env_override(self, monkeypatch, tmp_path):
        agent = get_agent("codex")
        fake_exe = tmp_path / "codex.exe"
        fake_exe.write_text("", encoding="utf-8")
        monkeypatch.setenv("CODEX_PATH", str(fake_exe))
        monkeypatch.setattr("vibeaway.agents.shutil.which", lambda *args, **kwargs: None)

        assert Path(agent.resolve_executable()) == fake_exe

    def test_build_cmd_streaming_full_auto(self):
        agent = get_agent("codex")

        cmd = agent.build_cmd(
            "analyze repo",
            continue_session=False,
            resume_id=None,
            streaming=True,
            permission_mode="acceptEdits",
        )

        assert cmd[:2] == [agent.resolve_executable(), "exec"]
        assert "--json" in cmd
        assert "--full-auto" in cmd
        assert "--skip-git-repo-check" in cmd
        assert cmd[-1] == "analyze repo"

    def test_build_cmd_resume_last(self):
        agent = get_agent("codex")

        cmd = agent.build_cmd(
            "continue work",
            continue_session=True,
            resume_id=None,
            permission_mode="default",
        )

        assert cmd[:3] == [agent.resolve_executable(), "exec", "resume"]
        assert "--last" in cmd
        assert cmd[-1] == "continue work"

    def test_extract_stream_text_from_agent_message(self):
        agent = get_agent("codex")

        text = agent._extract_stream_text(
            {
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "Inspecting repository state",
                },
            }
        )

        assert text == "Inspecting repository state"

    def test_extract_stream_text_from_assistant_message(self):
        agent = get_agent("codex")

        text = agent._extract_stream_text(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "Step 1 complete."},
                        {"type": "output_text", "text": "Preparing patch."},
                    ],
                },
            }
        )

        assert text == "Step 1 complete.\n\nPreparing patch."


class TestCopilotAdapter:
    def test_resolve_executable_supports_env_override(self, monkeypatch, tmp_path):
        agent = get_agent("copilot")
        fake_exe = tmp_path / "copilot.exe"
        fake_exe.write_text("", encoding="utf-8")
        monkeypatch.setenv("COPILOT_PATH", str(fake_exe))
        monkeypatch.setattr("vibeaway.agents.shutil.which", lambda *args, **kwargs: None)

        assert Path(agent.resolve_executable()) == fake_exe

    def test_build_cmd_default_non_interactive(self):
        agent = get_agent("copilot")

        cmd = agent.build_cmd(
            "fix tests",
            continue_session=False,
            resume_id=None,
            streaming=False,
            permission_mode="default",
        )

        assert cmd[0] == agent.resolve_executable()
        assert "--prompt" in cmd
        assert "--silent" in cmd
        assert "--stream" in cmd
        assert "off" in cmd
        assert "--allow-all-tools" in cmd

    def test_build_cmd_resume_bypass(self):
        agent = get_agent("copilot")

        cmd = agent.build_cmd(
            "continue work",
            continue_session=False,
            resume_id="abc-123",
            streaming=True,
            permission_mode="bypassPermissions",
        )

        assert "--resume" in cmd
        assert "abc-123" in cmd
        assert "--allow-all-tools" in cmd
        assert "--allow-all-paths" in cmd
        assert "on" in cmd
