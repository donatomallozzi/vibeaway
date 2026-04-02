"""Tests for voice command trigger word, locale alias, and command parsing logic."""

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token-for-testing")


# ─── Voice trigger words ──────────────────────────────────────────────────────

class TestVoiceTriggerWords:
    """Test the trigger word detection logic used in handle_voice."""

    # Base trigger word — always available
    BASE_TRIGGERS = {"ok"}

    def _parse_voice(self, transcript: str, trigger_words: set[str] | None = None) -> tuple:
        """Simulate the voice parsing logic from handle_voice."""
        triggers = trigger_words or self.BASE_TRIGGERS
        words = transcript.split()
        first = words[0].lower().rstrip(".,!?") if words else ""
        if first in triggers and len(words) >= 2:
            cmd_name = words[1].lower().rstrip(".,!?")
            args = words[2:]
            return cmd_name, args
        return None, None

    def test_trigger_ok(self):
        cmd, _ = self._parse_voice("Ok reset")
        assert cmd == "reset"

    def test_trigger_with_locale(self):
        """Italian trigger words should work."""
        it_triggers = {"ok", "punto", "comando"}
        cmd, _ = self._parse_voice("Punto sessioni", it_triggers)
        assert cmd == "sessioni"

    def test_trigger_comando_with_args(self):
        it_triggers = {"ok", "punto", "comando"}
        cmd, args = self._parse_voice("Comando annulla 3", it_triggers)
        assert cmd == "annulla"
        assert args == ["3"]

    def test_trigger_with_args(self):
        cmd, args = self._parse_voice("Ok cd src/utils")
        assert cmd == "cd"
        assert args == ["src/utils"]

    def test_no_trigger_is_prompt(self):
        cmd, _ = self._parse_voice("refactora il modulo auth")
        assert cmd is None

    def test_trigger_alone_no_command(self):
        cmd, _ = self._parse_voice("Ok")
        assert cmd is None

    def test_trigger_with_punctuation(self):
        cmd, _ = self._parse_voice("Ok, reset")
        assert cmd == "reset"

    def test_non_trigger_word(self):
        cmd, _ = self._parse_voice("esegui reset")
        assert cmd is None

    def test_trigger_case_insensitive(self):
        it_triggers = {"ok", "punto", "comando"}
        cmd, _ = self._parse_voice("COMANDO sessions", it_triggers)
        assert cmd == "sessions"

    def test_multiple_args(self):
        cmd, args = self._parse_voice("Ok ls -t -r -20")
        assert cmd == "ls"
        assert args == ["-t", "-r", "-20"]

    def test_empty_transcript(self):
        cmd, _ = self._parse_voice("")
        assert cmd is None


# ─── Locale aliases ──────────────────────────────────────────────────────────

class TestLocaleAliases:
    """Verify that locale alias files are consistent and correct."""

    def test_italian_aliases(self):
        from vibeaway.locales import get_aliases
        aliases = get_aliases("it")
        assert aliases["sessions"] == ["sessioni"]
        assert aliases["resume"] == ["riprendi"]
        assert aliases["help"] == ["aiuto"]
        assert "cattura" in aliases["stamp"]

    def test_french_aliases(self):
        from vibeaway.locales import get_aliases
        aliases = get_aliases("fr")
        assert "aide" in aliases["help"]
        assert "seances" in aliases["sessions"]
        assert "reprendre" in aliases["resume"]

    def test_german_aliases(self):
        from vibeaway.locales import get_aliases
        aliases = get_aliases("de")
        assert "hilfe" in aliases["help"]
        assert "sitzungen" in aliases["sessions"]
        assert "fortsetzen" in aliases["resume"]

    def test_spanish_aliases(self):
        from vibeaway.locales import get_aliases
        aliases = get_aliases("es")
        assert "ayuda" in aliases["help"]
        assert "sesiones" in aliases["sessions"]
        assert "reanudar" in aliases["resume"]

    def test_english_no_aliases(self):
        from vibeaway.locales import get_aliases
        assert get_aliases("en") == {}

    def test_aliases_match_commands(self):
        """All alias keys should correspond to actual bot commands."""
        from vibeaway.bot import _CMD_BY_NAME
        from vibeaway.locales import get_aliases

        for lang in ["it", "fr", "de", "es"]:
            aliases = get_aliases(lang)
            for cmd_name in aliases:
                assert cmd_name in _CMD_BY_NAME, \
                    f"Locale '{lang}' has alias for unknown command '{cmd_name}'"


# ─── Dot-prefix text commands ────────────────────────────────────────────────

class TestDotPrefix:
    """Test that '.' prefix is parsed as a command."""

    def _parse_dot(self, text: str) -> tuple:
        if text.startswith(".") and len(text) > 1 and not text.startswith(".."):
            parts = text[1:].split(None, 1)
            cmd_name = parts[0].lower()
            args = parts[1].split() if len(parts) > 1 else []
            return cmd_name, args
        return None, None

    def test_dot_ls(self):
        cmd, args = self._parse_dot(".ls")
        assert cmd == "ls"
        assert args == []

    def test_dot_ls_with_args(self):
        cmd, args = self._parse_dot(".ls -t -20 src")
        assert cmd == "ls"
        assert args == ["-t", "-20", "src"]

    def test_dot_reset(self):
        cmd, _ = self._parse_dot(".reset")
        assert cmd == "reset"

    def test_dot_cd_with_path(self):
        cmd, args = self._parse_dot(".cd my-project")
        assert cmd == "cd"
        assert args == ["my-project"]

    def test_double_dot_not_command(self):
        cmd, _ = self._parse_dot("..something")
        assert cmd is None

    def test_single_dot_not_command(self):
        cmd, _ = self._parse_dot(".")
        assert cmd is None

    def test_normal_text_not_command(self):
        cmd, _ = self._parse_dot("hello world")
        assert cmd is None


# ─── Command chaining ────────────────────────────────────────────────────────

class TestCommandChaining:
    """Test ' .+ ' separator splits into segments."""

    def _split_chain(self, text: str) -> list:
        if " .+ " in text:
            return [s.strip() for s in text.split(" .+ ") if s.strip()]
        return [text]

    def test_single_command(self):
        assert self._split_chain(".ls") == [".ls"]

    def test_two_commands(self):
        segments = self._split_chain(".cd src .+ .ls")
        assert segments == [".cd src", ".ls"]

    def test_three_commands(self):
        segments = self._split_chain(".reset .+ .cd src .+ .ls -t")
        assert segments == [".reset", ".cd src", ".ls -t"]

    def test_no_separator(self):
        segments = self._split_chain("plain text prompt")
        assert segments == ["plain text prompt"]

    def test_mixed_prompt_and_command(self):
        segments = self._split_chain("analizza il codice .+ .ls")
        assert segments == ["analizza il codice", ".ls"]
