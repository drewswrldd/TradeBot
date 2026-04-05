# ATS Bot Startup Task Scheduler Installation Script
# Run this script as Administrator to install the startup task

#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"

# Configuration
$TaskName = "ATSBotStartup"
$TaskDescription = "Starts ngrok tunnel for ATS Trading Bot webhook reception"
$BatPath = "C:\Users\duckm\TradeBot\startup\start_atsbot.bat"
$Username = $env:USERNAME

Write-Host "Installing ATS Bot startup task..." -ForegroundColor Cyan

# Check if batch file exists
if (-not (Test-Path $BatPath)) {
    Write-Host "ERROR: Batch file not found at $BatPath" -ForegroundColor Red
    exit 1
}

# Remove existing task if it exists
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Write-Host "Removing existing task '$TaskName'..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# Create the scheduled task action (run minimized via cmd /c start /min)
$Action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c start /min `"`" `"$BatPath`""

# Trigger: at user logon
$Trigger = New-ScheduledTaskTrigger -AtLogon -User $Username

# Settings: allow task to run on demand, don't stop if on battery
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

# Principal: run only when user is logged on (no elevation needed for ngrok)
$Principal = New-ScheduledTaskPrincipal `
    -UserId $Username `
    -LogonType Interactive `
    -RunLevel Limited

# Register the task
Register-ScheduledTask `
    -TaskName $TaskName `
    -Description $TaskDescription `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal

Write-Host ""
Write-Host "SUCCESS: Task '$TaskName' installed!" -ForegroundColor Green
Write-Host ""
Write-Host "The ngrok tunnel will start automatically at login." -ForegroundColor White
Write-Host "To run manually: schtasks /run /tn '$TaskName'" -ForegroundColor Gray
Write-Host "To remove: Unregister-ScheduledTask -TaskName '$TaskName'" -ForegroundColor Gray
Write-Host ""
