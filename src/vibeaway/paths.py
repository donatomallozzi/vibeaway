"""
Runtime directory layout.
All mutable runtime files live under ~/.vibeaway/.
"""
from pathlib import Path

RUNTIME_DIR = Path.home() / ".vibeaway"
ENV_FILE = RUNTIME_DIR / ".env"
VENV_DIR = RUNTIME_DIR / "venv"
LOG_DIR = RUNTIME_DIR / "logs"
SERVICE_DIR = RUNTIME_DIR / "service"
LOCK_FILE = SERVICE_DIR / "bot.lock"
BOT_LOG = LOG_DIR / "bot.log"
WATCHDOG_LOG = LOG_DIR / "watchdog.log"
