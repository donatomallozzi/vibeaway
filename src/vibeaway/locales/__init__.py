"""
Locale support for command aliases, descriptions, and UI messages.

Each locale file (it.json, fr.json, de.json, es.json, ...) defines:
- danger_patterns: locale-specific safety detection patterns
- trigger_words: voice trigger words for that language
- aliases: voice/text aliases for commands
- descriptions: localized command descriptions for help text
- messages: all user-facing UI strings

Lookup order:
  1. ~/.vibeaway/locales/{lang}.json  (user customizations)
  2. <package>/locales/{lang}.json        (built-in defaults)

User files in ~/.vibeaway/locales/ are merged on top of built-in defaults,
so you only need to override the keys you want to change.

The active locale is determined by config.WHISPER_LANGUAGE.
English (en) is the default — commands use their English names.
"""

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_BUILTIN_DIR = Path(__file__).parent
_USER_DIR = Path.home() / ".vibeaway" / "locales"
_cache: dict[str, dict] = {}

# Active messages dict — populated by init_locale()
_messages: dict[str, str] = {}


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base (1 level deep: dicts are merged, lists are replaced)."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = {**result[key], **value}
        else:
            result[key] = value
    return result


def _load_json(path: Path) -> dict:
    """Load a JSON file, return empty dict on failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to load %s: %s", path, exc)
        return {}


def load_locale(lang: str) -> dict:
    """Load a locale. User file (~/.vibeaway/locales/) overrides built-in defaults."""
    if lang in _cache:
        return _cache[lang]

    # Load built-in
    builtin_file = _BUILTIN_DIR / f"{lang}.json"
    data = _load_json(builtin_file) if builtin_file.exists() else {}

    # Merge user overrides on top
    user_file = _USER_DIR / f"{lang}.json"
    if user_file.exists():
        user_data = _load_json(user_file)
        if user_data:
            data = _deep_merge(data, user_data)
            logger.info("Merged user locale '%s' from %s", lang, user_file)

    if data:
        logger.info("Loaded locale '%s' (%d aliases, %d messages)",
                     lang, len(data.get("aliases", {})), len(data.get("messages", {})))

    _cache[lang] = data
    return data


def init_locale(lang: str) -> None:
    """Initialize the active locale. Call once at startup."""
    _messages.clear()
    if lang:
        _messages.update(load_locale(lang).get("messages", {}))


def t(key: str, **kwargs: Any) -> str:
    """Translate a message key. Falls back to the key itself if not found.
    Supports {placeholder} substitution via kwargs."""
    template = _messages.get(key, key)
    if kwargs:
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError):
            return template
    return template


def get_aliases(lang: str) -> dict[str, list[str]]:
    """Return {command_name: [alias1, alias2, ...]} for the given language."""
    return load_locale(lang).get("aliases", {})


def get_descriptions(lang: str) -> dict[str, str]:
    """Return {command_name: localized_description} for the given language."""
    return load_locale(lang).get("descriptions", {})


def get_trigger_words(lang: str) -> list[str]:
    """Return trigger words for the given language."""
    return load_locale(lang).get("trigger_words", [])
