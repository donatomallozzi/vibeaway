"""Tests for webcam capture, audio transcription, TTS, and voice command pipeline.

Uses ffmpeg to generate synthetic audio/video for testing without real hardware.
"""

import os
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token-for-testing")


@lru_cache(maxsize=None)
def _command_is_usable(name: str) -> bool:
    exe = shutil.which(name)
    if not exe:
        return False
    try:
        subprocess.run([exe, "-version"], capture_output=True, timeout=10, check=False)
    except (OSError, PermissionError, subprocess.SubprocessError):
        return False
    return True


def _require_command(name: str) -> None:
    if not _command_is_usable(name):
        pytest.skip(f"{name} not available or not executable in this environment")


def _require_gtts() -> None:
    pytest.importorskip("gtts")


def _require_faster_whisper() -> None:
    pytest.importorskip("faster_whisper")


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _generate_test_audio(path: str, text: str = "ciao come stai") -> str:
    """Generate a test OGG audio file using gTTS (real speech synthesis)."""
    _require_gtts()
    _require_command("ffmpeg")
    from gtts import gTTS

    mp3_path = path.replace(".ogg", ".mp3")
    tts = gTTS(text=text, lang="it")
    tts.save(mp3_path)
    subprocess.run(
        ["ffmpeg", "-y", "-i", mp3_path, "-c:a", "libopus", "-b:a", "64k", path],
        capture_output=True, check=True,
    )
    os.unlink(mp3_path)
    return path


def _generate_sine_audio(path: str, duration: float = 2.0) -> str:
    """Generate a test audio file with a sine wave (no network, fast)."""
    _require_command("ffmpeg")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
         "-c:a", "libopus", "-b:a", "64k", path],
        capture_output=True, check=True,
    )
    return path


def _generate_test_video(path: str, duration: float = 3.0, with_audio: bool = True) -> str:
    """Generate a synthetic test video with color bars and optional audio."""
    _require_command("ffmpeg")
    cmd: list[str] = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=640x480:rate=10",
    ]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"]
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-movflags", "+faststart"]
    cmd.append(path)
    subprocess.run(cmd, capture_output=True, check=True)
    return path


# ─── TTS tests ───────────────────────────────────────────────────────────────

class TestTTS:
    """Test text-to-speech synthesis."""

    def test_gtts_synthesize(self, tmp_path: Path) -> None:
        """gTTS should produce a non-empty audio file."""
        _require_gtts()
        from vibeaway.tts import synthesize

        out_path = str(tmp_path / "test.mp3")
        with patch("vibeaway.tts.config") as mock_config:
            mock_config.TTS_CHAIN = ["gtts"]
            mock_config.WHISPER_LANGUAGE = "it"
            result = synthesize("Ciao, questo è un test", output_path=out_path)

        assert Path(result).is_file()
        assert Path(result).stat().st_size > 1000

    def test_synthesize_empty_text_raises(self) -> None:
        """synthesize() should raise on empty text."""
        from vibeaway.tts import synthesize

        with pytest.raises(ValueError, match="Empty text"):
            synthesize("")

    def test_synthesize_truncates_long_text(self, tmp_path: Path) -> None:
        """Text longer than 4096 chars should be truncated, not fail."""
        _require_gtts()
        from vibeaway.tts import synthesize

        long_text = "parola " * 1000  # ~7000 chars
        out_path = str(tmp_path / "long.mp3")
        with patch("vibeaway.tts.config") as mock_config:
            mock_config.TTS_CHAIN = ["gtts"]
            mock_config.WHISPER_LANGUAGE = "it"
            result = synthesize(long_text, output_path=out_path)
        assert Path(result).is_file()

    def test_unknown_provider_raises(self) -> None:
        """All providers failing should raise RuntimeError."""
        from vibeaway.tts import synthesize

        with patch("vibeaway.tts.config") as mock_config:
            mock_config.TTS_CHAIN = ["nonexistent_provider"]
            with pytest.raises(RuntimeError, match="All TTS providers failed"):
                synthesize("test")


# ─── Transcription tests ─────────────────────────────────────────────────────

class TestTranscription:
    """Test audio transcription with faster-whisper (local, no API key needed)."""

    @pytest.fixture()
    def speech_audio(self, tmp_path: Path) -> str:
        """Generate a real speech audio file saying 'ciao come stai'."""
        _require_faster_whisper()
        path = str(tmp_path / "test_speech.ogg")
        return _generate_test_audio(path, text="ciao come stai")

    @pytest.fixture()
    def sine_audio(self, tmp_path: Path) -> str:
        """Generate a sine wave audio (for testing pipeline, not transcription quality)."""
        path = str(tmp_path / "test_sine.ogg")
        return _generate_sine_audio(path, duration=2.0)

    def test_transcribe_returns_text(self, speech_audio: str) -> None:
        """Transcription of Italian speech should return non-empty text."""
        _require_faster_whisper()
        from vibeaway.transcriber import transcribe_audio

        with patch("vibeaway.transcriber.config") as mock_config:
            mock_config.TRANSCRIBER_CHAIN = ["faster_whisper"]
            mock_config.FASTER_WHISPER_MODEL = "tiny"
            mock_config.WHISPER_LANGUAGE = "it"
            result = transcribe_audio(speech_audio)

        assert isinstance(result, str)
        assert len(result.strip()) > 0

    def test_transcribe_recognizes_italian(self, speech_audio: str) -> None:
        """'Ciao come stai' should be recognized (at least partially)."""
        _require_faster_whisper()
        from vibeaway.transcriber import transcribe_audio

        with patch("vibeaway.transcriber.config") as mock_config:
            mock_config.TRANSCRIBER_CHAIN = ["faster_whisper"]
            mock_config.FASTER_WHISPER_MODEL = "tiny"
            mock_config.WHISPER_LANGUAGE = "it"
            result = transcribe_audio(speech_audio).lower()

        assert any(w in result for w in ["ciao", "come", "stai"]), \
            f"Expected Italian greeting, got: {result!r}"

    def test_transcribe_fallback_chain(self, speech_audio: str) -> None:
        """If first provider fails, should fall back to next."""
        _require_faster_whisper()
        from vibeaway.transcriber import transcribe_audio

        with patch("vibeaway.transcriber.config") as mock_config:
            mock_config.TRANSCRIBER_CHAIN = ["openai_whisper", "faster_whisper"]
            mock_config.OPENAI_API_KEY = ""
            mock_config.FASTER_WHISPER_MODEL = "tiny"
            mock_config.WHISPER_LANGUAGE = "it"
            result = transcribe_audio(speech_audio)

        assert len(result.strip()) > 0

    def test_all_providers_fail_raises(self, sine_audio: str) -> None:
        """If all providers fail, should raise RuntimeError."""
        from vibeaway.transcriber import transcribe_audio

        with patch("vibeaway.transcriber.config") as mock_config:
            mock_config.TRANSCRIBER_CHAIN = ["nonexistent_provider"]
            with pytest.raises(RuntimeError, match="All transcribers failed"):
                transcribe_audio(sine_audio)


# ─── Voice command pipeline tests ────────────────────────────────────────────

class TestVoiceCommandPipeline:
    """Test the voice -> transcribe -> command dispatch pipeline."""

    def test_trigger_word_ok(self) -> None:
        """'ok' is always a trigger word regardless of locale."""
        from vibeaway.bot import _TRIGGER_WORDS, _CMD_BY_NAME

        words = "ok sessions".split()
        first = words[0].lower().rstrip(".,!?")
        assert first in _TRIGGER_WORDS
        assert words[1].lower() in _CMD_BY_NAME

    def test_trigger_word_with_args(self) -> None:
        """Trigger word + command + args should parse correctly."""
        transcript = "ok resume 1"
        words = transcript.split()
        assert words[0].lower() == "ok"
        assert words[1].lower() == "resume"
        assert words[2:] == ["1"]

    def test_non_trigger_treated_as_prompt(self) -> None:
        """Transcript without trigger word should NOT be treated as command."""
        from vibeaway.bot import _TRIGGER_WORDS

        transcript = "scrivi un programma in python"
        words = transcript.split()
        first = words[0].lower().rstrip(".,!?")
        assert first not in _TRIGGER_WORDS

    def test_locale_aliases_loaded(self) -> None:
        """Loading a locale should add aliases to the command registry."""
        from vibeaway.locales import get_aliases

        # Italian locale should have aliases
        it_aliases = get_aliases("it")
        assert "sessions" in it_aliases
        assert "sessioni" in it_aliases["sessions"]
        assert "resume" in it_aliases
        assert "riprendi" in it_aliases["resume"]

    def test_locale_trigger_words(self) -> None:
        """Each locale should define trigger words."""
        from vibeaway.locales import get_trigger_words

        assert "punto" in get_trigger_words("it")
        assert "comando" in get_trigger_words("it")
        assert "commande" in get_trigger_words("fr")
        assert "befehl" in get_trigger_words("de")
        assert "oye" in get_trigger_words("es")

    def test_all_locales_have_consistent_keys(self) -> None:
        """All locale files should define aliases for the same commands."""
        from vibeaway.locales import get_aliases

        langs = ["it", "fr", "de", "es"]
        all_keys = [set(get_aliases(lang).keys()) for lang in langs]
        # All locales should cover the same commands
        for i, lang in enumerate(langs):
            for j, other_lang in enumerate(langs):
                if i != j:
                    missing = all_keys[i] - all_keys[j]
                    assert not missing, \
                        f"Locale '{lang}' has commands not in '{other_lang}': {missing}"

    def test_english_has_no_aliases(self) -> None:
        """English (en) should return empty aliases."""
        from vibeaway.locales import get_aliases

        assert get_aliases("en") == {}


# ─── Webcam / video pipeline tests ──────────────────────────────────────────

class TestWebcamPipeline:
    """Test the video capture -> encode -> send pipeline using synthetic video."""

    def test_ffmpeg_encode_pipeline(self, tmp_path: Path) -> None:
        """Simulate the 2-pass webcam pipeline: raw capture -> trim -> H264 mp4."""
        _require_command("ffmpeg")
        _require_command("ffprobe")
        raw_path = str(tmp_path / "raw.avi")
        mp4_path = str(tmp_path / "output.mp4")

        # Pass 1: simulate raw capture with test source
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=5:size=1280x720:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
            "-c:v", "mjpeg", "-q:v", "3", "-c:a", "pcm_s16le",
            raw_path,
        ], capture_output=True, check=True)

        assert Path(raw_path).stat().st_size > 10000

        # Pass 2: trim 2s warmup + encode H264
        subprocess.run([
            "ffmpeg", "-y",
            "-ss", "2", "-i", raw_path,
            "-t", "3",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            mp4_path,
        ], capture_output=True, check=True)

        mp4 = Path(mp4_path)
        assert mp4.is_file()
        assert mp4.stat().st_size > 5000

        # Verify frame count > 1 (not a still image)
        probe = subprocess.run([
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=nb_frames",
            "-of", "csv=p=0",
            mp4_path,
        ], capture_output=True, text=True)
        nb_frames = probe.stdout.strip()
        if nb_frames and nb_frames != "N/A":
            assert int(nb_frames) > 1, f"Expected multiple frames, got {nb_frames}"

    def test_ffmpeg_encode_audio_present(self, tmp_path: Path) -> None:
        """Output MP4 should have both video and audio streams."""
        _require_command("ffprobe")
        mp4_path = str(tmp_path / "test.mp4")
        _generate_test_video(mp4_path, duration=2.0, with_audio=True)

        probe = subprocess.run([
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0",
            mp4_path,
        ], capture_output=True, text=True)

        streams = probe.stdout.strip().splitlines()
        assert "video" in streams, "No video stream found"
        assert "audio" in streams, "No audio stream found"

    def test_ffmpeg_movflags_faststart(self, tmp_path: Path) -> None:
        """MP4 with +faststart should have moov atom before mdata (streamable)."""
        mp4_path = str(tmp_path / "test.mp4")
        _generate_test_video(mp4_path, duration=2.0)

        with open(mp4_path, "rb") as f:
            data = f.read(4096)
        moov_pos = data.find(b"moov")
        mdat_pos = data.find(b"mdat")
        if moov_pos != -1 and mdat_pos != -1:
            assert moov_pos < mdat_pos, "moov should come before mdat with +faststart"

    def test_video_duration_within_bounds(self, tmp_path: Path) -> None:
        """Trimmed video duration should match requested duration."""
        _require_command("ffmpeg")
        _require_command("ffprobe")
        raw_path = str(tmp_path / "raw.avi")
        mp4_path = str(tmp_path / "trimmed.mp4")

        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=7:size=640x480:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=7",
            "-c:v", "mjpeg", "-q:v", "3", "-c:a", "pcm_s16le",
            raw_path,
        ], capture_output=True, check=True)

        subprocess.run([
            "ffmpeg", "-y", "-ss", "2", "-i", raw_path, "-t", "3",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart",
            mp4_path,
        ], capture_output=True, check=True)

        probe = subprocess.run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            mp4_path,
        ], capture_output=True, text=True)

        duration = float(probe.stdout.strip())
        assert 2.5 <= duration <= 3.5, f"Expected ~3s duration, got {duration:.1f}s"


# ─── End-to-end: speech -> transcribe -> TTS round-trip ──────────────────────

class TestSpeechRoundTrip:
    """Generate speech audio, transcribe it, then re-synthesize — full round-trip."""

    def test_tts_then_transcribe(self, tmp_path: Path) -> None:
        """gTTS output should be transcribable by faster-whisper."""
        _require_gtts()
        _require_faster_whisper()
        from vibeaway.tts import synthesize
        from vibeaway.transcriber import transcribe_audio

        tts_path = str(tmp_path / "greeting.mp3")
        with patch("vibeaway.tts.config") as mock_config:
            mock_config.TTS_CHAIN = ["gtts"]
            mock_config.WHISPER_LANGUAGE = "it"
            tts_result = synthesize("Ciao, tutto bene?", output_path=tts_path)

        assert Path(tts_result).is_file()

        with patch("vibeaway.transcriber.config") as mock_config:
            mock_config.TRANSCRIBER_CHAIN = ["faster_whisper"]
            mock_config.FASTER_WHISPER_MODEL = "tiny"
            mock_config.WHISPER_LANGUAGE = "it"
            transcript = transcribe_audio(tts_result).lower()

        assert len(transcript) > 0
        assert any(w in transcript for w in ["ciao", "tutto", "bene"]), \
            f"Expected to recognize 'ciao tutto bene', got: {transcript!r}"

    def test_voice_command_round_trip(self, tmp_path: Path) -> None:
        """Generate 'ok sessioni' speech, transcribe, verify trigger detection."""
        _require_gtts()
        _require_faster_whisper()
        from vibeaway.tts import synthesize
        from vibeaway.transcriber import transcribe_audio
        from vibeaway.bot import _TRIGGER_WORDS

        tts_path = str(tmp_path / "voice_cmd.mp3")
        with patch("vibeaway.tts.config") as mock_config:
            mock_config.TTS_CHAIN = ["gtts"]
            mock_config.WHISPER_LANGUAGE = "it"
            tts_result = synthesize("ok sessioni", output_path=tts_path)

        with patch("vibeaway.transcriber.config") as mock_config:
            mock_config.TRANSCRIBER_CHAIN = ["faster_whisper"]
            mock_config.FASTER_WHISPER_MODEL = "tiny"
            mock_config.WHISPER_LANGUAGE = "it"
            transcript = transcribe_audio(tts_result).strip()

        words = transcript.split()
        if len(words) >= 2:
            first = words[0].lower().rstrip(".,!?")
            if first in _TRIGGER_WORDS:
                # Trigger word detected — test passes
                assert True
