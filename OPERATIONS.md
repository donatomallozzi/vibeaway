# Operations Guide

> **Rule #1**: Only one bot instance must run at a time.
> The watchdog enforces this via a lock file, but duplicate processes
> can still occur if the scheduled task is restarted while an old instance
> is alive. Always **stop first, then start**.

---

## Architecture

```
Scheduled Task (VibeAway)
  └── bot_watchdog.pyw        ← single-instance lock, heartbeat monitor
        └── python -m vibeaway   ← the bot process
```

The scheduled task launches the **watchdog**, which in turn launches and
monitors the **bot**. If the bot crashes, the watchdog restarts it.
The watchdog itself is kept alive by the scheduled task (at-login + on-wake triggers).

Runtime directory: `~/.vibeaway/`

---

## First-time install

### 1. Create runtime environment

```bash
python service/install.py
```

Creates `~/.vibeaway/` with venv, `.env` template, and watchdog.

### 2. Configure

```powershell
# Windows
notepad $env:USERPROFILE\.vibeaway\.env
```
```bash
# Linux / macOS
nano ~/.vibeaway/.env
```

### 3. Register the service

**Windows** (admin PowerShell — required for task registration/removal):
```powershell
.\service\install-service.ps1
```

> **Note**: The scheduled task is created with admin privileges. Any later
> rename, removal, or re-registration also requires an **admin PowerShell**.
> `deploy.py` can stop/start the task but cannot unregister or rename it.

**Linux / macOS**:
```bash
bash service/install-service.sh
```

---

## Update & restart (the safe way)

### Automated deploy (recommended)

The deploy script keeps the bot running as long as possible, notifies you
on Telegram at each step, and only stops/restarts at the very end:

```bash
cd <repo>
python service/deploy.py
```

The sequence is:

```
  Step    Bot state    What happens
  ─────   ──────────   ─────────────────────────────────────────
  1/6     RUNNING      git pull
  2/6     RUNNING      Syntax check on key modules
  3/6     RUNNING      Telegram notification: "deploying…"
  ─────   ──────────   ─────────────────────────────────────────
  4/6     STOPPING     Stop watchdog + kill processes + remove lock
  5/6     DOWN         install.py --update (pip install + watchdog)
  6/6     STARTING     Start watchdog → bot sends startup message
```

If anything fails **before step 4**, the bot is never stopped and you
get a Telegram notification describing the error. If something fails
**at step 5 or 6**, you get a Telegram alert saying the bot is down
and needs manual intervention.

Options:
```bash
python service/deploy.py --no-pull   # skip git pull (already pulled)
```

### Manual deploy

If you prefer to run each step manually:

<details>
<summary><b>Windows</b> (PowerShell, admin)</summary>

```powershell
# 1. Pull changes (bot still running)
cd <repo>
git pull

# 2. Verify syntax (bot still running)
python -m py_compile src\vibeaway\bot.py
python -m py_compile src\vibeaway\agents.py

# 3. Stop the scheduled task + kill processes
Stop-ScheduledTask -TaskName VibeAway
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -match '^python(w)?\.exe$' -and
    $_.CommandLine -match 'vibeaway|bot_watchdog' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Remove-Item ~\.vibeaway\service\bot.lock -Force -ErrorAction SilentlyContinue

# 4. Update
python service/install.py --update

# 5. Restart
Start-ScheduledTask -TaskName VibeAway
```

One-liner for step 3:
```powershell
Stop-ScheduledTask VibeAway; Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -match 'vibeaway|bot_watchdog' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; Remove-Item ~\.vibeaway\service\bot.lock -Force -ErrorAction SilentlyContinue
```
</details>

<details>
<summary><b>Linux</b> (systemd)</summary>

```bash
# 1. Pull + verify (bot still running)
cd <repo>
git pull
python -m py_compile src/vibeaway/bot.py

# 2. Stop + update + restart
systemctl --user stop vibeaway
python service/install.py --update
systemctl --user start vibeaway
```
</details>

<details>
<summary><b>macOS</b> (launchd)</summary>

```bash
# 1. Pull + verify (bot still running)
cd <repo>
git pull
python -m py_compile src/vibeaway/bot.py

# 2. Stop + update + restart
launchctl unload ~/Library/LaunchAgents/com.vibeaway.plist
python service/install.py --update
launchctl load ~/Library/LaunchAgents/com.vibeaway.plist
```
</details>

---

## Stop (without restart)

### Windows

```powershell
Stop-ScheduledTask -TaskName VibeAway
```

The watchdog will shut down, which kills the bot. No new instance starts
until the task is manually started or the next login/wake trigger fires.

To fully disable (no auto-start at login):

```powershell
Disable-ScheduledTask -TaskName VibeAway
```

### Linux

```bash
systemctl --user stop vibeaway
```

### macOS

```bash
launchctl unload ~/Library/LaunchAgents/com.vibeaway.plist
```

---

## Verify

### Check the scheduled task

```powershell
(Get-ScheduledTask -TaskName VibeAway).State
# Expected: Running
```

### Check running processes

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'vibeaway|bot_watchdog' } |
  Select-Object ProcessId, Name |
  Format-Table
```

On Windows you will see **four** processes — this is normal. The venv `python.exe`
is a launcher that spawns the real `C:\PythonXX\python.exe`, so each logical
process (watchdog + bot) appears as a parent-child pair:

```
pythonw.exe (venv launcher)     ← watchdog
  └── pythonw.exe (real python) ← watchdog (actual)
        └── python.exe (venv)   ← bot
              └── python.exe    ← bot (actual)
```

On Linux/macOS you see exactly **two** processes: one watchdog + one bot.

### Check logs

```powershell
Get-Content ~\.vibeaway\logs\bot.log -Tail 20
Get-Content ~\.vibeaway\logs\watchdog.log -Tail 20
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Multiple bot instances | Task restarted without killing old processes | Stop task, kill all processes, remove lock, restart |
| Bot not starting | Stale `bot.lock` | `Remove-Item ~\.vibeaway\service\bot.lock -Force` |
| "Conflict" Telegram error | Two bots polling same token | Kill all, verify single instance, restart |
| Watchdog running but no bot | Bot crashing on startup | Check `~/.vibeaway/logs/bot.log` |
| Task state "Ready" not "Running" | Task completed or was stopped | `Start-ScheduledTask VibeAway` |
| Deploy failed at step 5/6 | pip or watchdog copy error | Check deploy output, fix, re-run `python service/deploy.py --no-pull` |
| Deploy hangs at step 4 | deploy.py killed itself | Bug was fixed: deploy now excludes its own PID from the kill. Update `deploy.py` |
| Need to rename scheduled task | Old task name still registered | Requires admin: `Unregister-ScheduledTask -TaskName OldName -Confirm:$false` then `.\service\install-service.ps1` |
| `Unregister-ScheduledTask` access denied | Task was created by admin | Run the command in an **admin PowerShell** |
| 4 processes instead of 2 (Windows) | Venv launcher creates child process | Normal — each logical process is a parent-child pair (see Verify section) |
