"""
Watchdog for vibeaway — single-instance guard + auto-restart + heartbeat check.

Cross-platform: Windows (Task Scheduler), Linux (systemd), macOS (launchd).
Deployed to ~/.vibeaway/service/ by install.py.

- Uses a lock file to prevent duplicate instances.
- Restarts the bot automatically after a crash (10s delay).
- Monitors heartbeat file: if bot doesn't update it for HEARTBEAT_TIMEOUT
  seconds, it's considered hung and gets killed + restarted.
- Detects crash loops (5 restarts in 5 minutes) and sends Telegram alert.
- On Windows, prevents automatic standby while the bot is running.
- Logs to ~/.vibeaway/logs/watchdog.log with rotation.
"""

import signal
import subprocess
import sys
import time
import os
import logging
import platform
import tempfile
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import quote

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

# ── Prevent sleep (platform-specific) ───────────────────────────────────────

def _prevent_sleep() -> None:
    """Prevent OS automatic sleep/standby while the bot is running."""
    if IS_WINDOWS:
        try:
            import ctypes
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        except Exception:
            pass
    elif IS_MACOS:
        # Launch caffeinate in background (prevents sleep until killed)
        try:
            global _caffeinate_proc
            _caffeinate_proc = subprocess.Popen(["caffeinate", "-i"])
        except Exception:
            pass
    # Linux: systemd-inhibit or similar — typically handled by the service manager

_caffeinate_proc: subprocess.Popen | None = None


def _allow_sleep() -> None:
    """Restore normal sleep behavior."""
    if IS_WINDOWS:
        try:
            import ctypes
            ES_CONTINUOUS = 0x80000000
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        except Exception:
            pass
    elif IS_MACOS:
        if _caffeinate_proc and _caffeinate_proc.poll() is None:
            _caffeinate_proc.terminate()


# ── Paths ─────────────────────────────────────────────────────────────────────
RUNTIME_DIR = Path.home() / ".vibeaway"
SERVICE_DIR = RUNTIME_DIR / "service"
LOG_DIR = RUNTIME_DIR / "logs"
LOCK_FILE = SERVICE_DIR / "bot.lock"
HEARTBEAT_FILE = Path(tempfile.gettempdir()) / "tgbot_heartbeat"
ENV_FILE = RUNTIME_DIR / ".env"

# Find python executable in the same venv as this script
_exe_dir = Path(sys.executable).resolve().parent
if IS_WINDOWS:
    PYTHON_EXE = _exe_dir / "python.exe"
else:
    PYTHON_EXE = _exe_dir / "python"

RESTART_DELAY = 10          # seconds before restarting after crash
HEARTBEAT_TIMEOUT = 120     # seconds without heartbeat = hung
HEARTBEAT_CHECK_INTERVAL = 15  # seconds between heartbeat checks

# Crash loop detection
CRASH_LOOP_COUNT = 5        # number of restarts
CRASH_LOOP_WINDOW = 300     # within this many seconds
CRASH_LOOP_COOLDOWN = 600   # seconds to wait before resuming after crash loop alert

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("watchdog")
logger.setLevel(logging.INFO)
_handler = RotatingFileHandler(LOG_DIR / "watchdog.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_handler)


# ── Telegram alerts ───────────────────────────────────────────────────────────

def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if not ENV_FILE.exists():
        return env
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def _get_telegram_config() -> tuple[str, list[int]]:
    env = _load_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat_ids: list[int] = []
    raw = env.get("ALLOWED_USER_IDS", "")
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        uid = entry.split(":")[0].strip()
        try:
            chat_ids.append(int(uid))
        except ValueError:
            pass
    return token, chat_ids


def send_telegram_alert(message: str) -> None:
    token, chat_ids = _get_telegram_config()
    if not token or not chat_ids:
        logger.warning("Cannot send Telegram alert: missing token or chat IDs")
        return
    for chat_id in chat_ids:
        url = f"https://api.telegram.org/bot{token}/sendMessage?chat_id={chat_id}&text={quote(message)}"
        try:
            req = Request(url, method="GET")
            urlopen(req, timeout=10)
            logger.info("Telegram alert sent to %d", chat_id)
        except Exception as exc:
            logger.warning("Failed to send Telegram alert to %d: %s", chat_id, exc)


# ── Process utilities ─────────────────────────────────────────────────────────

def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def acquire_lock() -> bool:
    SERVICE_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            old_pid = int(LOCK_FILE.read_text().strip())
            if is_pid_alive(old_pid):
                logger.warning("Another instance is running (PID %d). Exiting.", old_pid)
                return False
            else:
                logger.info("Stale lock file (PID %d dead). Taking over.", old_pid)
        except (ValueError, OSError):
            logger.info("Invalid lock file. Overwriting.")
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def get_heartbeat_age() -> float | None:
    try:
        ts = float(HEARTBEAT_FILE.read_text().strip())
        return time.time() - ts
    except (OSError, ValueError):
        return None


def clear_heartbeat() -> None:
    try:
        HEARTBEAT_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def kill_process_tree(pid: int) -> None:
    """Kill a process and all its children (cross-platform)."""
    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=10,
            )
        except Exception as exc:
            logger.warning("taskkill failed for PID %d: %s", pid, exc)
    else:
        # Unix: send SIGTERM to process group
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (OSError, ProcessLookupError) as exc:
            logger.warning("killpg failed for PID %d: %s", pid, exc)
            try:
                os.kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    if not acquire_lock():
        return

    _prevent_sleep()
    logger.info("Watchdog started (PID %d, python=%s, platform=%s, sleep=blocked)",
                os.getpid(), PYTHON_EXE, platform.system())
    restart_times: deque[float] = deque()
    proc: subprocess.Popen | None = None

    try:
        while True:
            clear_heartbeat()
            logger.info("Starting bot via: %s -m vibeaway", PYTHON_EXE)

            # On Unix, start in new process group so we can kill the whole tree
            popen_kwargs: dict = {}
            if not IS_WINDOWS:
                popen_kwargs["preexec_fn"] = os.setsid

            proc = subprocess.Popen(
                [str(PYTHON_EXE), "-m", "vibeaway"],
                **popen_kwargs,
            )
            logger.info("Bot started (PID %d)", proc.pid)

            reason = "exited"
            while True:
                exit_code = proc.poll()
                if exit_code is not None:
                    logger.warning("Bot exited with code %d", exit_code)
                    if exit_code == 42:
                        logger.info("Exit code 42 = clean shutdown. Not restarting.")
                        return
                    reason = f"exited with code {exit_code}"
                    break

                age = get_heartbeat_age()
                if age is not None and age > HEARTBEAT_TIMEOUT:
                    logger.error(
                        "Heartbeat stale (%.0fs old, timeout=%ds). Bot is hung — killing.",
                        age, HEARTBEAT_TIMEOUT,
                    )
                    kill_process_tree(proc.pid)
                    proc.wait(timeout=10)
                    reason = f"hung (no heartbeat for {int(age)}s)"
                    break

                time.sleep(HEARTBEAT_CHECK_INTERVAL)

            # Crash loop detection
            now = time.time()
            restart_times.append(now)
            while restart_times and (now - restart_times[0]) > CRASH_LOOP_WINDOW:
                restart_times.popleft()

            if len(restart_times) >= CRASH_LOOP_COUNT:
                msg = (
                    f"\U0001F6A8 CRASH LOOP DETECTED\n\n"
                    f"Bot restarted {len(restart_times)} times in "
                    f"{int(now - restart_times[0])}s.\n"
                    f"Last failure: {reason}\n\n"
                    f"Pausing for {CRASH_LOOP_COOLDOWN // 60} minutes before retrying."
                )
                logger.error(msg)
                send_telegram_alert(msg)
                restart_times.clear()
                time.sleep(CRASH_LOOP_COOLDOWN)
            else:
                logger.info("Restarting in %d seconds...", RESTART_DELAY)
                time.sleep(RESTART_DELAY)

    except KeyboardInterrupt:
        logger.info("Watchdog interrupted. Terminating bot...")
        if proc and proc.poll() is None:
            proc.terminate()
    except Exception:
        logger.exception("Watchdog fatal error")
    finally:
        _allow_sleep()
        release_lock()
        logger.info("Watchdog stopped.")


if __name__ == "__main__":
    main()
