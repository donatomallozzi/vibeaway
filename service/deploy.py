"""
Safe deploy for vibeaway — updates code, then restarts with minimal downtime.

Sends Telegram notifications at each step so the operator knows what happened
even if the bot is temporarily down.

Sequence:
  1. git pull                          (bot still running)
  2. syntax check on key modules       (bot still running)
  3. notify via Telegram: "deploying…" (bot still running)
  4. stop watchdog + bot               (downtime starts)
  5. install.py --update               (pip install + watchdog copy)
  6. start watchdog                    (downtime ends — bot sends its own startup message)

If any step before 4 fails, the bot is never stopped and the operator is
notified of the failure.  If step 5 or 6 fails, the operator is notified
and can intervene manually while the bot is down.

Usage:
  python service/deploy.py            # full deploy
  python service/deploy.py --no-pull  # skip git pull (already pulled)

Platforms: Windows (Task Scheduler), Linux (systemd), macOS (launchd).
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform == "linux"
IS_MACOS = sys.platform == "darwin"

REPO_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = Path.home() / ".vibeaway"
ENV_FILE = RUNTIME_DIR / ".env"
LOCK_FILE = RUNTIME_DIR / "service" / "bot.lock"
INSTALL_SCRIPT = REPO_DIR / "service" / "install.py"

# Key source modules to syntax-check before deploying
CHECK_MODULES = [
    REPO_DIR / "src" / "vibeaway" / "bot.py",
    REPO_DIR / "src" / "vibeaway" / "agents.py",
    REPO_DIR / "src" / "vibeaway" / "config.py",
    REPO_DIR / "service" / "bot_watchdog.pyw",
]


# ── Telegram notifications ───────────────────────────────────────────────────

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
    for entry in env.get("ALLOWED_USER_IDS", "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        uid = entry.split(":")[0].strip()
        try:
            chat_ids.append(int(uid))
        except ValueError:
            pass
    return token, chat_ids


def notify(message: str) -> None:
    """Send a Telegram message to all allowed users."""
    token, chat_ids = _get_telegram_config()
    if not token or not chat_ids:
        print(f"  [WARN] Cannot send Telegram notification (no token/users)")
        return
    for chat_id in chat_ids:
        url = (
            f"https://api.telegram.org/bot{token}/sendMessage"
            f"?chat_id={chat_id}&text={quote(message)}"
        )
        try:
            urlopen(Request(url, method="GET"), timeout=10)
        except Exception as exc:
            print(f"  [WARN] Telegram notify failed for {chat_id}: {exc}")


# ── Step runners ─────────────────────────────────────────────────────────────

def step_git_pull() -> bool:
    """Pull latest changes from remote."""
    print("\n[1/6] git pull")
    result = subprocess.run(
        ["git", "pull"], cwd=str(REPO_DIR), capture_output=True, text=True,
    )
    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip()
        print(f"  [FAIL] git pull failed:\n{err[:500]}")
        notify(f"\u274c Deploy ABORTED — git pull failed:\n{err[:300]}")
        return False
    output = result.stdout.strip()
    print(f"  {output}")
    if "Already up to date" in output:
        print("  [INFO] No changes to deploy.")
    return True


def step_syntax_check() -> bool:
    """Compile-check key modules to catch syntax errors before stopping the bot."""
    print("\n[2/6] Syntax check")
    for path in CHECK_MODULES:
        if not path.exists():
            print(f"  [WARN] {path.name} not found, skipping")
            continue
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(path)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            err = result.stderr.strip()
            print(f"  [FAIL] {path.name}: {err[:300]}")
            notify(f"\u274c Deploy ABORTED — syntax error in {path.name}:\n{err[:300]}")
            return False
        print(f"  [OK] {path.name}")
    return True


def step_notify_deploy() -> None:
    """Notify users that deploy is starting (bot is about to go down)."""
    print("\n[3/6] Notify: deploying")
    notify("\U0001f504 Deploy in progress — the bot will restart shortly.")


def step_stop_bot() -> bool:
    """Stop the watchdog and bot processes."""
    print("\n[4/6] Stop bot & watchdog")
    if IS_WINDOWS:
        # Stop scheduled task
        subprocess.run(
            ["powershell", "-Command", "Stop-ScheduledTask -TaskName VibeAway"],
            capture_output=True, timeout=30,
        )
        # Kill processes (exclude our own PID and parent)
        my_pid = os.getpid()
        parent_pid = os.getppid()
        subprocess.run(
            ["powershell", "-Command",
             "Get-CimInstance Win32_Process | "
             "Where-Object { $_.Name -match '^python(w)?\\.exe$' -and "
             "$_.CommandLine -match 'vibeaway|bot_watchdog' -and "
             f"$_.ProcessId -ne {my_pid} -and $_.ProcessId -ne {parent_pid}" " } | "
             "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
            capture_output=True, timeout=30,
        )
    elif IS_LINUX:
        subprocess.run(
            ["systemctl", "--user", "stop", "vibeaway"],
            capture_output=True, timeout=30,
        )
    elif IS_MACOS:
        subprocess.run(
            ["launchctl", "unload",
             str(Path.home() / "Library" / "LaunchAgents" / "com.vibeaway.plist")],
            capture_output=True, timeout=30,
        )

    # Remove stale lock
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass

    # Brief wait for processes to die
    time.sleep(2)
    print("  [OK] Bot stopped")
    return True


def step_install_update() -> bool:
    """Run install.py --update (pip install + watchdog copy)."""
    print("\n[5/6] install.py --update")
    result = subprocess.run(
        [sys.executable, str(INSTALL_SCRIPT), "--update"],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip()
        print(f"  [FAIL] install.py --update failed:\n{err[:500]}")
        notify(
            f"\u274c Deploy FAILED — install.py --update error.\n"
            f"Bot is DOWN. Manual intervention required.\n\n{err[:300]}"
        )
        return False
    print(f"  [OK] Install complete")
    return True


def step_start_bot() -> bool:
    """Restart the watchdog (which starts the bot)."""
    print("\n[6/6] Start bot")
    if IS_WINDOWS:
        result = subprocess.run(
            ["powershell", "-Command", "Start-ScheduledTask -TaskName VibeAway"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            err = result.stderr.strip()
            print(f"  [FAIL] Could not start task: {err[:300]}")
            notify(
                f"\u274c Deploy FAILED — could not start scheduled task.\n"
                f"Bot is DOWN. Manual intervention required.\n\n{err[:200]}"
            )
            return False
    elif IS_LINUX:
        subprocess.run(
            ["systemctl", "--user", "start", "vibeaway"],
            capture_output=True, timeout=30,
        )
    elif IS_MACOS:
        subprocess.run(
            ["launchctl", "load",
             str(Path.home() / "Library" / "LaunchAgents" / "com.vibeaway.plist")],
            capture_output=True, timeout=30,
        )

    print("  [OK] Bot starting (startup message will arrive on Telegram)")
    return True


# ── Main ─────────────────────────────────────────────────────────────────────

def deploy(skip_pull: bool = False) -> bool:
    print("=" * 60)
    print("vibeaway deploy")
    print(f"  repo:    {REPO_DIR}")
    print(f"  runtime: {RUNTIME_DIR}")
    print("=" * 60)

    # Phase 1: prepare (bot still running)
    if not skip_pull:
        if not step_git_pull():
            return False
    else:
        print("\n[1/6] git pull — skipped (--no-pull)")

    if not step_syntax_check():
        return False

    step_notify_deploy()

    # Phase 2: restart (minimal downtime window)
    if not step_stop_bot():
        return False

    if not step_install_update():
        return False

    if not step_start_bot():
        return False

    print("\n" + "=" * 60)
    print("Deploy complete!")
    print("=" * 60)
    return True


def main():
    parser = argparse.ArgumentParser(description="Safe deploy for vibeaway")
    parser.add_argument("--no-pull", action="store_true",
                        help="Skip git pull (code already updated)")
    args = parser.parse_args()
    success = deploy(skip_pull=args.no_pull)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
