#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [switch]$Restore
)

$ErrorActionPreference = "Stop"

$RepoDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$SrcDir = Join-Path $RepoDir "src"
$RepoLogsDir = Join-Path $RepoDir "logs"
$RepoPython = Join-Path $RepoDir ".venv\Scripts\python.exe"

$RuntimeDir = Join-Path $env:USERPROFILE ".vibeaway"
$RuntimeServiceDir = Join-Path $RuntimeDir "service"
$RuntimeLogDir = Join-Path $RuntimeDir "logs"
$RuntimeBotLog = Join-Path $RuntimeLogDir "bot.log"
$LockFile = Join-Path $RuntimeServiceDir "bot.lock"

$TaskName = "VibeAway"
$TaskNames = @($TaskName)

$StdoutLog = Join-Path $RepoLogsDir "local-bot-stdout.log"
$StderrLog = Join-Path $RepoLogsDir "local-bot-stderr.log"


function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Yellow
}


function Write-Info([string]$Message) {
    Write-Host "    $Message" -ForegroundColor Gray
}


function Get-TaskIfExists([string]$TaskName) {
    Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}


function Set-TaskEnabledState([string]$TaskName, [bool]$Enabled) {
    $task = Get-TaskIfExists $TaskName
    if (-not $task) {
        Write-Info "Task '$TaskName' not found."
        return
    }

    if (-not $Enabled) {
        try {
            Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        } catch {
            Write-Info "Stop task '$TaskName': $($_.Exception.Message)"
        }
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
        Write-Info "Task '$TaskName' disabled."
        return
    }

    Enable-ScheduledTask -TaskName $TaskName | Out-Null
    Write-Info "Task '$TaskName' enabled."
}


function Get-BotProcesses() {
    $repoPattern = [regex]::Escape($RepoDir)
    $runtimePattern = [regex]::Escape($RuntimeDir)

    Get-CimInstance Win32_Process | Where-Object {
        $name = [string]$_.Name
        $cmd = [string]$_.CommandLine
        $exe = [string]$_.ExecutablePath

        ($name -match '^python(w)?\.exe$') -and (
            $cmd -match 'vibeaway' -or
            $cmd -match 'bot_watchdog' -or
            $cmd -match $repoPattern -or
            $cmd -match $runtimePattern -or
            $exe -match $repoPattern -or
            $exe -match $runtimePattern
        )
    } | Sort-Object ProcessId
}


function Stop-BotProcesses() {
    $procs = @(Get-BotProcesses)
    if (-not $procs) {
        Write-Info "No bot/watchdog processes to stop."
        return
    }

    foreach ($proc in $procs) {
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
            Write-Info ("Killed PID {0}: {1}" -f $proc.ProcessId, $proc.Name)
        } catch {
            Write-Info ("Failed to kill PID {0}: {1}" -f $proc.ProcessId, $_.Exception.Message)
        }
    }

    Start-Sleep -Seconds 2
}


function Remove-WatchdogLock() {
    if (Test-Path $LockFile) {
        Remove-Item $LockFile -Force
        Write-Info "Lock file removed: $LockFile"
        return
    }

    Write-Info "No lock file to remove."
}


function Start-LocalBot() {
    if (-not (Test-Path $RepoPython)) {
        throw "Local Python not found: $RepoPython"
    }
    if (-not (Test-Path $SrcDir)) {
        throw "src directory not found: $SrcDir"
    }

    if (-not (Test-Path $RepoLogsDir)) {
        New-Item -ItemType Directory -Path $RepoLogsDir | Out-Null
    }

    foreach ($path in @($StdoutLog, $StderrLog)) {
        if (Test-Path $path) {
            Remove-Item $path -Force
        }
    }

    $previousPythonPath = $env:PYTHONPATH
    if ([string]::IsNullOrWhiteSpace($previousPythonPath)) {
        $env:PYTHONPATH = $SrcDir
    } else {
        $env:PYTHONPATH = "$SrcDir;$previousPythonPath"
    }

    try {
        $proc = Start-Process `
            -FilePath $RepoPython `
            -ArgumentList @("-m", "vibeaway") `
            -WorkingDirectory $RepoDir `
            -RedirectStandardOutput $StdoutLog `
            -RedirectStandardError $StderrLog `
            -WindowStyle Hidden `
            -PassThru
    } finally {
        if ([string]::IsNullOrWhiteSpace($previousPythonPath)) {
            Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        } else {
            $env:PYTHONPATH = $previousPythonPath
        }
    }

    Start-Sleep -Seconds 6
    $running = Get-Process -Id $proc.Id -ErrorAction SilentlyContinue
    if (-not $running) {
        throw "Local bot exited immediately. Check $StderrLog"
    }

    Write-Info "Local bot started with PID $($proc.Id)"
    return $proc
}


function Show-Status() {
    Write-Host ""
    Write-Host "=== Scheduled Tasks ===" -ForegroundColor Cyan
    foreach ($taskName in $TaskNames) {
        $task = Get-TaskIfExists $taskName
        if ($task) {
            Write-Host ("  {0}: {1}" -f $task.TaskName, $task.State)
        } else {
            Write-Host ("  {0}: <not present>" -f $taskName)
        }
    }

    Write-Host ""
    Write-Host "=== Bot Processes ===" -ForegroundColor Cyan
    $procs = @(Get-BotProcesses)
    if ($procs) {
        foreach ($proc in $procs) {
            Write-Host ("  PID {0} | {1}" -f $proc.ProcessId, $proc.CommandLine)
        }
    } else {
        Write-Host "  <none>"
    }

    Write-Host ""
    Write-Host "=== Logs ===" -ForegroundColor Cyan
    Write-Host "  Runtime bot log : $RuntimeBotLog"
    Write-Host "  Local stdout    : $StdoutLog"
    Write-Host "  Local stderr    : $StderrLog"

    if (Test-Path $StderrLog) {
        $stderr = Get-Content $StderrLog -Tail 20
        if ($stderr) {
            Write-Host ""
            Write-Host "--- local stderr tail ---" -ForegroundColor DarkYellow
            $stderr | ForEach-Object { Write-Host $_ }
        }
    }

    if (Test-Path $RuntimeBotLog) {
        $botTail = Get-Content $RuntimeBotLog -Tail 20
        if ($botTail) {
            Write-Host ""
            Write-Host "--- runtime bot.log tail ---" -ForegroundColor DarkYellow
            $botTail | ForEach-Object { Write-Host $_ }
        }
    }
}


if ($Restore) {
    Write-Step "Restoring scheduled task mode"
    Stop-BotProcesses
    Remove-WatchdogLock
    Set-TaskEnabledState -TaskName $TaskName -Enabled $true

    $task = Get-TaskIfExists $TaskName
    if ($task) {
        Start-ScheduledTask -TaskName $TaskName
        Write-Info "Task '$TaskName' started."
        Start-Sleep -Seconds 8
    } else {
        Write-Info "Task '$TaskName' not found: nothing to start."
    }

    Show-Status
    exit 0
}


Write-Step "Switching to local test mode"
Set-TaskEnabledState -TaskName $TaskName -Enabled $false
Stop-BotProcesses
Remove-WatchdogLock
$null = Start-LocalBot
Show-Status
