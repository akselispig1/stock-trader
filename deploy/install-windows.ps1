# Turn this machine into an always-on AI stockbroker server (Windows).
#
#   Right-click -> "Run with PowerShell as Administrator", or:
#   powershell -ExecutionPolicy Bypass -File .\deploy\install-windows.ps1
#
# Registers a scheduled task that starts the bot at sign-in and restarts it if
# it crashes, and stops the laptop sleeping (including on lid close).
# Re-running it is safe.

$ErrorActionPreference = "Stop"
$Repo     = (Resolve-Path "$PSScriptRoot\..").Path
$TaskName = "AI Stockbroker"

function Fail($msg) { Write-Host "[X] $msg" -ForegroundColor Red; exit 1 }
function Ok($msg)   { Write-Host "[OK] $msg" -ForegroundColor Green }
function Step($msg) { Write-Host "`n> $msg" -ForegroundColor Cyan }

$admin = ([Security.Principal.WindowsPrincipal] `
          [Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Fail "Run this in an Administrator PowerShell window." }

Write-Host "Installing AI Stockbroker as a startup task"
Write-Host "  repo: $Repo"
Write-Host "  user: $env:USERNAME"

# --- 1. Prerequisites ------------------------------------------------------
Step "Checking setup"
$envFile = Join-Path $Repo ".env"
if (-not (Test-Path $envFile)) {
  Fail ".env not found. Run run-local.bat first, fill in your three API keys, confirm it works, then re-run this."
}
$blank = Select-String -Path $envFile -Pattern '^(ANTHROPIC_API_KEY=(sk-ant-\.\.\.)?$|ALPACA_API_KEY=$|ALPACA_SECRET_KEY=$)'
if ($blank) { Fail ".env still has blank keys. Fill all three in, then re-run." }

$py = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
  Step "Creating the Python environment"
  & python -m venv (Join-Path $Repo ".venv")
  if (-not (Test-Path $py)) { Fail "Could not create the virtual environment. Is Python installed and on PATH?" }
}
Step "Installing dependencies"
& $py -m pip install -q --upgrade pip
& $py -m pip install -q -r (Join-Path $Repo "requirements.txt")
Ok "Dependencies ready"

# --- 2. The scheduled task -------------------------------------------------
# Triggered at sign-in rather than at boot: an at-boot task would have to store
# your Windows password to run while logged out. See SERVER.md for the
# auto-sign-in setup that makes this fully headless.
Step "Registering the '$TaskName' startup task"
$action  = New-ScheduledTaskAction -Execute $py -Argument "-m bot.serve --port 8080" -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -StartWhenAvailable -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero)   # never time out; it runs forever
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Settings $settings -Principal $principal `
  -Description "Runs the AI stockbroker trading loop and dashboard." | Out-Null
Ok "Task registered"

# --- 3. Don't sleep --------------------------------------------------------
# A laptop suspending on idle or lid-close is the most likely way this setup
# silently stops trading.
Step "Preventing sleep (this machine is a server now)"
powercfg /change standby-timeout-ac 0   | Out-Null
powercfg /change hibernate-timeout-ac 0 | Out-Null
powercfg /change disk-timeout-ac 0      | Out-Null
# Lid close on mains power -> do nothing (0)
$SUB_BUTTONS = "4f971e89-eebd-4455-a8de-9e59040e7347"
$LID_ACTION  = "5ca83367-6e45-459f-a27b-476b1d01c936"
powercfg /setacvalueindex SCHEME_CURRENT $SUB_BUTTONS $LID_ACTION 0 | Out-Null
powercfg /setactive SCHEME_CURRENT | Out-Null
Ok "Sleep, hibernate and lid-close suspend disabled on mains power"
Write-Host "     (On battery it will still sleep - keep it plugged in.)" -ForegroundColor Yellow

# --- 4. Start it now -------------------------------------------------------
Step "Starting"
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 6
try {
  $h = Invoke-RestMethod "http://localhost:8080/healthz" -TimeoutSec 5
  Ok "Running (uptime $($h.uptime_s)s)"
  if ($h.last_error) { Write-Host "     last error: $($h.last_error)" -ForegroundColor Yellow }
} catch {
  Write-Host "[!] Not answering yet. Give it a minute, then open http://localhost:8080/healthz" -ForegroundColor Yellow
  Write-Host "    If it stays down, check Task Scheduler -> '$TaskName' -> Last Run Result." -ForegroundColor Yellow
}

$ip = (Get-NetIPAddress -AddressFamily IPv4 |
       Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
       Select-Object -First 1).IPAddress
Write-Host ""
Write-Host "   Dashboard:  http://localhost:8080"
if ($ip) { Write-Host "   From another device on your wifi:  http://${ip}:8080" }
Write-Host "   Health:     http://localhost:8080/healthz"
Write-Host ""
Write-Host "   Stop:       Stop-ScheduledTask -TaskName '$TaskName'"
Write-Host "   Start:      Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "   Remove:     .\deploy\uninstall-windows.ps1"
