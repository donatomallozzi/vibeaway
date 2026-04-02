"""
Install / update vibeaway runtime environment.

Creates ~/.vibeaway/ with:
  .env          — copied from .env.example on first install
  venv/         — dedicated Python virtual environment with the bot installed
  logs/         — bot and watchdog logs
  service/      — watchdog script, lock file

Usage:
  python service/install.py            # full install (venv + package + watchdog)
  python service/install.py --update   # re-install package and watchdog only
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = Path.home() / ".vibeaway"
VENV_DIR = RUNTIME_DIR / "venv"
SERVICE_DIR = RUNTIME_DIR / "service"
LOG_DIR = RUNTIME_DIR / "logs"
LOCALES_DIR = RUNTIME_DIR / "locales"
ENV_EXAMPLE = REPO_DIR / ".env.example"
WATCHDOG_SRC = REPO_DIR / "service" / "bot_watchdog.pyw"

IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
    VENV_PIP = VENV_DIR / "Scripts" / "pip.exe"
else:
    VENV_PYTHON = VENV_DIR / "bin" / "python"
    VENV_PIP = VENV_DIR / "bin" / "pip"


def _run(cmd: list[str], desc: str) -> bool:
    """Run a command, print status, return success."""
    print(f"  [{desc}] {' '.join(str(c) for c in cmd[:3])}...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  [ERR] {desc} failed:")
        print(result.stderr[-500:] if result.stderr else "(no output)")
        return False
    return True


def install(update_only: bool = False):
    print(f"Runtime directory: {RUNTIME_DIR}")
    print()

    # 1. Create directory structure
    for d in (RUNTIME_DIR, SERVICE_DIR, LOG_DIR, LOCALES_DIR):
        d.mkdir(parents=True, exist_ok=True)
    print("  [OK] Directories created")

    # 2. Copy .env on first install
    env_dest = RUNTIME_DIR / ".env"
    if not env_dest.exists() and not update_only:
        if ENV_EXAMPLE.exists():
            shutil.copy2(ENV_EXAMPLE, env_dest)
            print(f"  [OK] .env copied from {ENV_EXAMPLE.name}")
            print(f"\n  *** Edit {env_dest} with your settings! ***\n")
        else:
            print(f"  [WARN] {ENV_EXAMPLE} not found — create {env_dest} manually")
    else:
        print(f"  [OK] .env {'already exists' if env_dest.exists() else 'skipped (--update)'}")

    # 3. Create venv if it doesn't exist
    if not VENV_PYTHON.exists():
        print(f"  Creating virtual environment in {VENV_DIR}...")
        result = subprocess.run(
            [sys.executable, "-m", "venv", str(VENV_DIR)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"  [ERR] Failed to create venv: {result.stderr[:300]}")
            return False
        print(f"  [OK] Virtual environment created")
    else:
        print(f"  [OK] Virtual environment exists")

    # 4. Install/update the package into the runtime venv
    print(f"  Installing vibeaway into runtime venv...")
    _run([str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip"], "Upgrade pip")
    if not _run([str(VENV_PYTHON), "-m", "pip", "install", str(REPO_DIR)], "Install package"):
        return False
    print(f"  [OK] Package installed")

    # 5. Install optional dependencies (local whisper, tts)
    # These are best-effort: don't fail if they can't install
    for extra in ("local-whisper", "tts"):
        _run([str(VENV_PYTHON), "-m", "pip", "install", f"{REPO_DIR}[{extra}]"], f"Install [{extra}]")

    # 6. Copy watchdog
    if WATCHDOG_SRC.exists():
        shutil.copy2(WATCHDOG_SRC, SERVICE_DIR / "bot_watchdog.pyw")
        print(f"  [OK] bot_watchdog.pyw deployed")
    else:
        print(f"  [ERR] {WATCHDOG_SRC} not found!")
        return False

    # Summary
    print()
    print("=" * 60)
    print("Installation complete!")
    print(f"  Runtime:  {RUNTIME_DIR}")
    print(f"  Venv:     {VENV_DIR}")
    print(f"  Config:   {env_dest}")
    print(f"  Logs:     {LOG_DIR}")
    print(f"  Watchdog: {SERVICE_DIR / 'bot_watchdog.pyw'}")
    print()
    if not update_only:
        print("Next steps:")
        print(f"  1. Edit {env_dest}")
        if IS_WINDOWS:
            print("  2. Register the service (admin PowerShell):")
            print("     .\\service\\install-service.ps1")
        else:
            print("  2. Register the service:")
            print("     bash service/install-service.sh")
    else:
        print("  Restart the watchdog to pick up changes.")
    print("=" * 60)
    return True


def main():
    parser = argparse.ArgumentParser(description="Install vibeaway runtime")
    parser.add_argument("--update", action="store_true",
                        help="Re-install package and watchdog (skip .env, keep venv)")
    args = parser.parse_args()
    success = install(update_only=args.update)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
