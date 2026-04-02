"""
Bot configuration. Reads environment variables from .env file
(or from system environment if already exported).
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from vibeaway.paths import ENV_FILE

load_dotenv(ENV_FILE if ENV_FILE.exists() else None)

logger = logging.getLogger(__name__)


# ─── Telegram ──────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
"""Bot token from @BotFather."""

_raw_ids = os.getenv("ALLOWED_USER_IDS", "")
# Format: "123456:Mario,789012:Luigi" or legacy "123456,789012"
ALLOWED_USERS: dict[int, str] = {}
for _entry in _raw_ids.split(","):
    _entry = _entry.strip()
    if not _entry:
        continue
    if ":" in _entry:
        _uid, _name = _entry.split(":", 1)
        ALLOWED_USERS[int(_uid.strip())] = _name.strip()
    else:
        ALLOWED_USERS[int(_entry)] = ""
ALLOWED_USER_IDS: set[int] = set(ALLOWED_USERS.keys())
"""
Whitelist of authorized Telegram user_ids.
Format: ALLOWED_USER_IDS=123456:Mario,789012:Luigi
or legacy: ALLOWED_USER_IDS=123456,789012
Leave empty to allow anyone (local testing only!).
"""


# ─── Claude Code CLI ───────────────────────────────────────────────────────────

BASEDIR: str = os.getenv("BASEDIR", os.getenv("WORKDIR", str(Path.home())))
"""Root directory boundary. Workdir can only change within this."""

WORKDIR: str = os.getenv("WORKDIR", BASEDIR)
"""Default working directory for Claude Code CLI."""

DEFAULT_AGENT: str = os.getenv("DEFAULT_AGENT", "claude")
"""Default CLI agent used by the bot (claude, codex, copilot)."""

CLAUDE_TIMEOUT_SECONDS: int = int(os.getenv("CLAUDE_TIMEOUT_SECONDS", "300"))
"""Max seconds to wait for an inline (synchronous) Claude response."""

CLAUDE_TASK_TIMEOUT_SECONDS: int = int(os.getenv("CLAUDE_TASK_TIMEOUT_SECONDS", "1800"))
"""Max seconds to wait for a background /task response (default: 30 min)."""


# ─── Audio transcription ─────────────────────────────────────────────────────

TRANSCRIBER: str = os.getenv("TRANSCRIBER", "openai_whisper")
"""
Transcription engine for voice messages. Comma-separated list = fallback chain.
  groq_whisper    → Groq Whisper API (fastest, requires GROQ_API_KEY)
  openai_whisper  → OpenAI Whisper API (requires OPENAI_API_KEY)
  faster_whisper  → local model with faster-whisper (no key needed, requires GPU/CPU)
Example: TRANSCRIBER=groq_whisper,openai_whisper,faster_whisper
"""
TRANSCRIBER_CHAIN: list[str] = [t.strip() for t in TRANSCRIBER.split(",") if t.strip()]

GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
"""Required only if TRANSCRIBER=groq_whisper. Free key from console.groq.com."""

OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
"""Required only if TRANSCRIBER=openai_whisper."""

FASTER_WHISPER_MODEL: str = os.getenv("FASTER_WHISPER_MODEL", "base")
"""
faster-whisper model: tiny, base, small, medium, large-v3.
'base' is the best speed/quality tradeoff on CPU.
"""

WHISPER_LANGUAGE: str = os.getenv("WHISPER_LANGUAGE", "")
"""Expected language for voice messages (ISO 639-1 code)."""


# ─── Text-to-Speech ──────────────────────────────────────────────────────────

TTS_PROVIDER: str = os.getenv("TTS_PROVIDER", "openai_tts,gtts")
"""
TTS provider. Comma-separated list = fallback chain.
  openai_tts  → OpenAI TTS API (requires OPENAI_API_KEY)
  gtts        → Google TTS (free, no key)
Example: TTS_PROVIDER=openai_tts,gtts
"""
TTS_CHAIN: list[str] = [t.strip() for t in TTS_PROVIDER.split(",") if t.strip()]

TTS_VOICE: str = os.getenv("TTS_VOICE", "alloy")
"""OpenAI TTS voice: alloy, echo, fable, onyx, nova, shimmer."""

TTS_MODEL: str = os.getenv("TTS_MODEL", "tts-1")
"""OpenAI TTS model: tts-1 (fast) or tts-1-hd (higher quality)."""


# ─── Webcam ──────────────────────────────────────────────────────────────────

WEBCAM_VIDEO_DEVICE: str = os.getenv("WEBCAM_VIDEO_DEVICE", "")
"""DirectShow video device name (e.g. "Integrated Camera"). Leave empty for auto-detect."""

WEBCAM_AUDIO_DEVICE: str = os.getenv("WEBCAM_AUDIO_DEVICE", "")
"""DirectShow audio device name. Leave empty for auto-detect."""

WEBCAM_RESOLUTION: str = os.getenv("WEBCAM_RESOLUTION", "1280x720")
"""Webcam capture resolution (WxH)."""

WEBCAM_FRAMERATE: int = int(os.getenv("WEBCAM_FRAMERATE", "30"))
"""Webcam capture framerate."""


logger.info(
    "Config loaded: BASEDIR=%s, WORKDIR=%s, TRANSCRIBER=%s, ALLOWED_USERS=%d, WHISPER_LANG=%s",
    BASEDIR, WORKDIR, TRANSCRIBER, len(ALLOWED_USER_IDS), WHISPER_LANGUAGE,
)
