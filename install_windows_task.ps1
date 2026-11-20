# ============================================================
# install_windows_task.ps1
# ------------------------------------------------------------
# Registers the Smart Inbox-to-Calendar Assistant as a Windows
# Scheduled Task so it works fully automatically:
#
#   * checks your inbox every N minutes (default 5)
#   * also starts as soon as you log in
#   * runs on battery too (laptops), and catches up after sleep
#   * never runs two copies at once
#   * no window, no console - completely in the background
#
# Every new email with a meeting, interview, appointment, exam,
# payment due date or deadline is turned into a Google Calendar
# event with reminders, without you running any command.
#
# Usage (run once; run again after moving the project folder):
#   powershell -ExecutionPolicy Bypass -File install_windows_task.ps1
#   powershell -ExecutionPolicy Bypass -File install_windows_task.ps1 -IntervalMinutes 10
# ============================================================
param(
    [int]$IntervalMinutes = 5
)

$TaskName = "SmartInboxCalendarAssistant"
$LegacyTaskNames = @("GmailAIAgent")
$Script = Join-Path $PSScriptRoot "src\agent.py"

if (-not (Test-Path $Script)) {
    Write-Host "Cannot find $Script - run this file from the project folder." -ForegroundColor Red
    exit 1
}

$Python = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if ($null -eq $Python) { $Python = (Get-Command python -ErrorAction SilentlyContinue).Source }
if ($null -eq $Python) {
    Write-Host "Python was not found on PATH." -ForegroundColor Red
    exit 1
}

foreach ($old in $LegacyTaskNames) {
    if (Get-ScheduledTask -TaskName $old -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $old -Confirm:$false
        Write-Host "Removed old task '$old'."
    }
}

$action = New-ScheduledTaskAction -Execute $Python -Argument "`"$Script`" --once" `
          -WorkingDirectory (Join-Path $PSScriptRoot "src")

$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
          -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$logon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($repeat, $logon) `
    -Settings $settings -Principal $principal -Force `
    -Description "Smart Inbox-to-Calendar Assistant: turns emails with dates, deadlines and meetings into Google Calendar reminders." | Out-Null

Write-Host ""
Write-Host "SUCCESS: '$TaskName' now runs every $IntervalMinutes minutes and at every log-on." -ForegroundColor Green
Write-Host "Log file : $env:USERPROFILE\.smart_inbox_calendar_assistant\logs\assistant.log"
Write-Host "Run now  : schtasks /run /tn `"$TaskName`""
Write-Host "Status   : Get-ScheduledTaskInfo -TaskName `"$TaskName`""
Write-Host "Remove   : Unregister-ScheduledTask -TaskName `"$TaskName`" -Confirm:`$false"
