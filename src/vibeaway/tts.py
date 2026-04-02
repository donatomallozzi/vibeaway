"""
Text-to-Speech module.
Supports multiple backends with automatic fallback via TTS_PROVIDER config.
Value can be a comma-separated list: first success wins.

  openai_tts  → OpenAI TTS API (requires OPENAI_API_KEY)
  gtts        → Google TTS (free, no key)
"""

import logging
import re
import tempfile
from collections.abc import Callable

from vibeaway import config


def _detect_language(text: str) -> str:
    """Detect the language of *text*, returning an ISO 639-1 code.

    Uses ``langdetect`` when available; falls back to
    ``config.WHISPER_LANGUAGE`` or ``"en"``.
    """
    try:
        from langdetect import detect
        return detect(text)
    except Exception:
        return config.WHISPER_LANGUAGE or "en"


def _strip_markdown(text: str) -> str:
    """Remove common Markdown/code formatting so TTS reads clean prose."""
    # Fenced code blocks (``` ... ```)
    text = re.sub(r"```[a-z]*\n?", "", text)
    # Inline code (`...`)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    # Bold / italic markers (* and _)
    text = re.sub(r"\*{1,2}(.+?)\*{1,2}", r"\1", text)
    text = re.sub(r"_{1,2}(.+?)_{1,2}", r"\1", text)
    # Headings (# ... )
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Links [text](url) → text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Images ![alt](url) → alt
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    # Horizontal rules
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)
    # List bullets (-, *, +) at line start
    text = re.sub(r"^[\s]*[-*+]\s+", "", text, flags=re.MULTILINE)
    # Numbered lists
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)
    # Collapse multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

logger = logging.getLogger(__name__)

_BACKENDS: dict[str, Callable[..., str]] = {}


def _backend(name: str):
    """Decorator to register a TTS backend."""
    def decorator(func):
        _BACKENDS[name] = func
        return func
    return decorator


def synthesize(text: str, output_path: str | None = None) -> str:
    """
    Convert text to speech audio file.
    Tries each provider in config.TTS_CHAIN order.
    Returns the path to the generated audio file.
    """
    text = _strip_markdown(text)

    if not text:
        raise ValueError("Empty text")

    # TTS APIs have char limits
    max_chars = 4096
    if len(text) > max_chars:
        text = text[:max_chars] + "..."

    if output_path:
        out = output_path
    else:
        tmp = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
        out = tmp.name
        tmp.close()
    chain = config.TTS_CHAIN
    last_error = None

    for provider in chain:
        func = _BACKENDS.get(provider)
        if not func:
            logger.warning("Unknown TTS provider: %s (skipping)", provider)
            continue
        try:
            return func(text, out)
        except Exception as exc:
            logger.warning("TTS '%s' failed: %s", provider, exc)
            last_error = exc

    raise RuntimeError(f"All TTS providers failed ({', '.join(chain)}): {last_error}")


# ─── Backend: OpenAI TTS ─────────────────────────────────────────────────────

@_backend("openai_tts")
def _synthesize_openai(text: str, output_path: str) -> str:
    from openai import OpenAI

    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not configured")

    client = OpenAI(api_key=config.OPENAI_API_KEY)
    lang = _detect_language(text)
    logger.info("OpenAI TTS: voice=%s model=%s lang=%s chars=%d", config.TTS_VOICE, config.TTS_MODEL, lang, len(text))

    # Build API kwargs; use 'instructions' for language hint when the model
    # supports it (gpt-4o-mini-tts and newer), otherwise fall back to a
    # plain create() call and let the model infer from the input text.
    _LANG_NAMES = {
        "it": "Italian", "en": "English", "fr": "French", "de": "German",
        "es": "Spanish", "pt": "Portuguese", "nl": "Dutch", "pl": "Polish",
        "ru": "Russian", "ja": "Japanese", "ko": "Korean", "zh": "Chinese",
    }
    kwargs: dict = dict(
        model=config.TTS_MODEL,
        voice=config.TTS_VOICE,
        input=text,
        response_format="opus",
    )
    lang_name = _LANG_NAMES.get(lang)
    if lang_name:
        try:
            kwargs["instructions"] = f"Speak in {lang_name}. Read all text using {lang_name} pronunciation."
            response = client.audio.speech.create(**kwargs)
        except Exception:
            # Model doesn't support 'instructions' param — retry without it
            logger.debug("OpenAI TTS: 'instructions' not supported by %s, retrying without", config.TTS_MODEL)
            kwargs.pop("instructions", None)
            response = client.audio.speech.create(**kwargs)
    else:
        response = client.audio.speech.create(**kwargs)

    response.stream_to_file(output_path)
    logger.info("OpenAI TTS complete: %s (lang=%s)", output_path, lang)
    return output_path


# ─── Backend: gTTS (Google) ──────────────────────────────────────────────────

@_backend("gtts")
def _synthesize_gtts(text: str, output_path: str) -> str:
    from gtts import gTTS

    lang = _detect_language(text)
    logger.info("gTTS: lang=%s (auto-detected) chars=%d", lang, len(text))

    tts = gTTS(text=text, lang=lang)
    # gTTS outputs MP3; Telegram accepts it as voice
    mp3_path = output_path.replace(".ogg", ".mp3") if output_path.endswith(".ogg") else output_path
    tts.save(mp3_path)
    logger.info("gTTS complete: %s", mp3_path)
    return mp3_path
