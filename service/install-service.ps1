# install-service.ps1 — Install bot as scheduled task with watchdog wrapper
# Run as Administrator: right-click > Run with PowerShell (as Admin)
#
# Prerequisite: python service/install.py  (creates ~/.vibeaway/ with venv)
#
# The watchdog lives in ~/.vibeaway/service/bot_watchdog.pyw
# The runtime venv lives in ~/.vibeaway/venv/
# The bot is launched via: python -m vibeaway

$ErrorActionPreference = "Stop"

$TaskName   = "VibeAway"
$RepoDir    = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$RuntimeDir = Join-Path $env:USERPROFILE ".vibeaway"
$VenvDir    = Join-Path $RuntimeDir "venv"
$ServiceDir = Join-Path $RuntimeDir "service"
$PythonW    = Join-Path $VenvDir "Scripts\pythonw.exe"
$Python     = Join-Path $VenvDir "Scripts\python.exe"
$Watchdog   = Join-Path $ServiceDir "bot_watchdog.pyw"

# 1. Run install.py if runtime doesn't exist yet
if (-not (Test-Path $PythonW)) {
    Write-Host "Runtime venv not found. Running install.py..." -ForegroundColor Yellow
    & python (Join-Path $RepoDir "service\install.py")
    if (-not (Test-Path $PythonW)) {
        Write-Host "Installation failed. pythonw.exe not found at $PythonW" -ForegroundColor Red
        exit 1
    }
} else {
    # Just update watchdog and package
    Write-Host "Updating runtime..." -ForegroundColor Yellow
    & $Python (Join-Path $RepoDir "service\install.py") --update
}

if (-not (Test-Path $Watchdog)) {
    Write-Host "Watchdog not found at $Watchdog" -ForegroundColor Red
    exit 1
}

# 2. Remove old scheduled task if it exists
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Write-Host "Removing old scheduled task '$TaskName'..." -ForegroundColor Yellow
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "  Old task removed." -ForegroundColor Green
}

# 3. Kill any running bot/watchdog processes
Write-Host "Killing any running bot / watchdog processes..." -ForegroundColor Yellow
Get-WmiObject Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*vibeaway*" -or $_.CommandLine -like "*bot_watchdog*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3

# 4. Remove stale lock file
$lockFile = Join-Path $ServiceDir "bot.lock"
if (Test-Path $lockFile) {
    Remove-Item $lockFile -Force
    Write-Host "  Stale lock file removed." -ForegroundColor Gray
}

# 5. Register new scheduled task (launches watchdog at login)
Write-Host "Registering scheduled task '$TaskName'..." -ForegroundColor Yellow

$action = New-ScheduledTaskAction `
    -Execute $PythonW `
    -Argument "`"$Watchdog`"" `
    -WorkingDirectory $ServiceDir

# Trigger 1: at login
$triggerLogin = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Trigger 2: on wake from standby (Power-Troubleshooter event ID 1)
$CIMTriggerClass = Get-CimClass -ClassName MSFT_TaskEventTrigger -Namespace Root/Microsoft/Windows/TaskScheduler
$triggerWake = New-CimInstance -CimClass $CIMTriggerClass -ClientOnly
$triggerWake.Subscription = @"
<QueryList>
  <Query Id="0" Path="System">
    <Select Path="System">*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]</Select>
  </Query>
</QueryList>
"@
$triggerWake.Delay = "PT10S"   # 10s delay after wake to let network come up
$triggerWake.Enabled = $true

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger @($triggerLogin, $triggerWake) `
    -Settings $settings `
    -Description 'VibeAway - watchdog with auto-restart (login + wake)' `
    -Force

Write-Host "  Task registered." -ForegroundColor Green

# 6. Start it now
Write-Host "Starting task..." -ForegroundColor Yellow
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 12

# 7. Verify
$state = (Get-ScheduledTask -TaskName $TaskName).State
$procs = Get-WmiObject Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*vibeaway*" -or $_.CommandLine -like "*bot_watchdog*" }

Write-Host ""
Write-Host "=== Task Status ===" -ForegroundColor Cyan
Write-Host "  Task:  $TaskName"
Write-Host "  State: $state"

Write-Host ""
Write-Host "=== Processes ===" -ForegroundColor Cyan
$procs | ForEach-Object { Write-Host "  PID $($_.ProcessId): $($_.CommandLine)" }

$logDir = Join-Path $RuntimeDir "logs"
if ($state -eq "Running" -and $procs.Count -ge 1) {
    Write-Host ""
    Write-Host "SUCCESS: Bot is running with watchdog!" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "WARNING: Check logs at $logDir" -ForegroundColor Red
}

Write-Host ""
Write-Host "Press any key to close..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
