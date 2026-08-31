# Undo deploy/install-windows.ps1: remove the startup task and let the machine
# sleep again. Leaves the repo, .env and all trading data untouched.
#
#   powershell -ExecutionPolicy Bypass -File .\deploy\uninstall-windows.ps1

$ErrorActionPreference = "Stop"
$TaskName = "AI Stockbroker"

$admin = ([Security.Principal.WindowsPrincipal] `
          [Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Write-Host "[X] Run in an Administrator PowerShell window." -ForegroundColor Red; exit 1 }

Write-Host "> Stopping and removing the task" -ForegroundColor Cyan
Stop-ScheduledTask       -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

Write-Host "> Restoring normal power behaviour" -ForegroundColor Cyan
powercfg /change standby-timeout-ac 30   | Out-Null
powercfg /change hibernate-timeout-ac 60 | Out-Null
powercfg /change disk-timeout-ac 20      | Out-Null
# Lid close on mains -> sleep (1), the Windows default
powercfg /setacvalueindex SCHEME_CURRENT `
  "4f971e89-eebd-4455-a8de-9e59040e7347" "5ca83367-6e45-459f-a27b-476b1d01c936" 1 | Out-Null
powercfg /setactive SCHEME_CURRENT | Out-Null

Write-Host ""
Write-Host "[OK] Removed. Your repo, .env and trading history are untouched." -ForegroundColor Green
Write-Host "     Run the bot by hand any time with:  .\run-local.bat"
Write-Host "     Power settings reset to 30min sleep / lid-close sleep on mains."
