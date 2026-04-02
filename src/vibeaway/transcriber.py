"""
Audio transcription module.
Supports multiple backends with automatic fallback via TRANSCRIBER config.
Value can be a comma-separated list: first success wins.

  groq_whisper    → Groq Whisper API (cloud, fastest)
  openai_whisper  → OpenAI Whisper API (cloud)
  faster_whisper  → local model (CPU/GPU, no key needed)
"""

import logging
from collections.abc import Callable

from vibeaway import config

logger = logging.getLogger(__name__)

_BACKENDS: dict[str, Callable[..., str]] = {}


def _backend(name: str):
    """Decorator to register a transcription backend."""
    def decorator(func):
        _BACKENDS[name] = func
        return func
    return decorator


def transcribe_audio(file_path: str) -> str:
    """
    Transcribe audio file to text using the configured fallback chain.
    Tries each provider in order; raises on total failure.
    """
    chain = config.TRANSCRIBER_CHAIN
    last_error = None
    for provider in chain:
        func = _BACKENDS.get(provider)
        if not func:
            logger.warning("Unknown transcriber: %s (skipping)", provider)
            continue
        try:
            return func(file_path)
        except Exception as exc:
            logger.warning("Transcriber '%s' failed: %s", provider, exc)
            last_error = exc
    raise RuntimeError(f"All transcribers failed ({', '.join(chain)}): {last_error}")


# ─── Backend: Groq Whisper ────────────────────────────────────────────────────

@_backend("groq_whisper")
def _transcribe_groq(file_path: str) -> str:
    from groq import Groq

    if not config.GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY not configured")

    client = Groq(api_key=config.GROQ_API_KEY)
    logger.info("Groq whisper-large-v3-turbo (file=%s)", file_path)
    with open(file_path, "rb") as audio_file:
        transcript = client.audio.transcriptions.create(
            model="whisper-large-v3-turbo",
            file=audio_file,
            language=config.WHISPER_LANGUAGE or None,
        )
    logger.info("Groq transcription complete (%d chars)", len(transcript.text))
    return transcript.text


# ─── Backend: OpenAI Whisper ──────────────────────────────────────────────────

@_backend("openai_whisper")
def _transcribe_openai(file_path: str) -> str:
    from openai import OpenAI

    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not configured")

    client = OpenAI(api_key=config.OPENAI_API_KEY)
    logger.info("OpenAI Whisper (file=%s)", file_path)
    with open(file_path, "rb") as audio_file:
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language=config.WHISPER_LANGUAGE or None,
        )
    logger.info("OpenAI transcription complete (%d chars)", len(transcript.text))
    return transcript.text


# ─── Backend: faster-whisper (local) ─────────────────────────────────────────

_fw_model = None


def _get_faster_whisper_model():
    global _fw_model
    if _fw_model is None:
        from faster_whisper import WhisperModel
        logger.info("Loading faster-whisper model '%s'…", config.FASTER_WHISPER_MODEL)
        _fw_model = WhisperModel(
            config.FASTER_WHISPER_MODEL,
            device="cpu",
            compute_type="int8",
        )
    return _fw_model


@_backend("faster_whisper")
def _transcribe_faster_whisper(file_path: str) -> str:
    logger.info("faster-whisper (model=%s, file=%s)", config.FASTER_WHISPER_MODEL, file_path)
    model = _get_faster_whisper_model()

    lang = config.WHISPER_LANGUAGE or None
    segments, info = model.transcribe(file_path, language=lang, beam_size=5)

    text = " ".join(seg.text.strip() for seg in segments)
    logger.info("Detected language: %s (prob=%.2f)", info.language, info.language_probability)
    return text
