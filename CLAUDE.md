# Project: vibeaway

Telegram bot that bridges text/voice messages to local coding CLIs (Claude Code, Codex, Copilot).

## Architecture

```
src/vibeaway/
  bot.py             Main file (~2700 lines): command registry, handlers, streaming, TTS
  agents.py          CLI adapters: ClaudeCodeAdapter, CodexAdapter, CopilotAdapter
  session_backends.py  Session store readers per CLI
  session_manager.py   Backward-compatible facade over session backends
  config.py          Reads .env, exports typed constants
  paths.py           Runtime paths (~/.vibeaway/)
  transcriber.py     Audio transcription (groq/openai/faster-whisper fallback chain)
  tts.py             Text-to-Speech (openai_tts/gtts fallback chain, markdown stripping)
  locales/           i18n: en.json, it.json, fr.json, de.json, es.json, pt.json
service/
  install.py         Idempotent installer for ~/.vibeaway/ runtime
  deploy.py          Safe deploy: pull, validate, notify, late restart
  install-service.ps1  Windows scheduled task registration (admin)
  install-service.sh   Linux systemd / macOS launchd registration
  bot_watchdog.pyw   Cross-platform watchdog: lock, heartbeat, crash loop detection
  run_bot.bat        Dev launcher (uses repo .venv, not runtime venv)
  use-local-bot.ps1  Dev helper: disables task, runs from repo
tests/               177 tests (pytest + pytest-asyncio)
```

## Key design patterns

- **Command registry**: `CmdDef` dataclass with `name`, `shortcuts`, `aliases`, `handler`. Single source of truth for commands, help, voice dispatch. Decorator `@_register_cmd` registers everything.
- **Shortcuts**: Short aliases (e.g. `/ss` for `/sessions`) registered as both Telegram CommandHandlers and in `_VOICE_CMD_MAP`.
- **Dot-prefix**: `.command` = `/command` in text messages. No space between dot and name. Bare `.` repeats last input. `!N` re-executes Nth history entry.
- **Locale system**: `locales/__init__.py` deep-merges user overrides from `~/.vibeaway/locales/`. `t()` function for all UI strings. `WHISPER_LANGUAGE` controls active locale.
- **Agent adapters**: Abstract `AgentAdapter` with `run_batch()` and `run_stream()`. Windows uses thread-pumped pipes for Codex streaming. Each adapter resolves its own CLI executable.
- **TTS**: `_strip_markdown()` cleans response text before synthesis. `_detect_language()` via langdetect for correct TTS pronunciation.

## Development workflow

```bash
# Install dev dependencies
pip install -e ".[dev,local-whisper,tts]"

# Run tests
pytest tests/ -v

# Run bot locally (dev mode)
python -m vibeaway
```

## Production operations

See OPERATIONS.md for the full guide. Key rule: **only one instance at a time**.

Update & restart (safe deploy with Telegram notifications):
```bash
python service/deploy.py
```
Pulls, validates, notifies via Telegram, then stops+restarts with minimal downtime.
See OPERATIONS.md for manual alternatives.

## Operational pitfalls

- **deploy.py must exclude its own PID** when killing bot processes (step 4), otherwise it terminates itself. The kill filter uses `os.getpid()` and `os.getppid()` to avoid this.
- **Scheduled task registration/removal requires admin PowerShell.** `deploy.py` can stop/start the task but cannot unregister or rename it. To rename: admin shell → `Unregister-ScheduledTask` → `.\service\install-service.ps1`.
- **Windows venv creates 2 OS processes per logical process** (launcher + real python). 4 processes in task manager is normal (watchdog pair + bot pair). Do not kill them thinking they are duplicates.
- **Renaming the project** requires updating: package dir, all imports, pyproject.toml, runtime dir path (`~/.vibeaway/`), scheduled task name, systemd/launchd service name, and re-running `install-service` from admin.

## Conventions

- All user-facing strings use `t("key", ...)` from the locale system, never hardcoded.
- Command option style: `-N` for numeric flags (Unix-style), positional for simple args. No long `--flags`.
- Output must be short for mobile (Telegram). Prefer concise one-line messages.
- English is the default language. `WHISPER_LANGUAGE` defaults to `""` (auto-detect). gTTS/TTS fallback language is `"en"`.
- All Italian strings in scripts/service files have been translated to English for open-source.
- Locale aliases must be consistent across all non-English locales (same command set in it/fr/de/es/pt).
- `pyproject.toml` is the single source of truth for dependencies. No `requirements.txt`.
- License: MIT.
