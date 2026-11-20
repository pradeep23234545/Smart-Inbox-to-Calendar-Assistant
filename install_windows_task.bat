@echo off
REM ============================================================
REM install_windows_task.bat
REM ------------------------------------------------------------
REM Double-click to make the Smart Inbox-to-Calendar Assistant
REM run fully automatically in the background:
REM   - checks your inbox every 5 minutes and at every log-on
REM   - runs on battery too, catches up after sleep
REM   - turns emails with meetings, interviews, due dates and
REM     deadlines into Google Calendar reminders
REM
REM Run it again after moving the project folder.
REM To change the interval, e.g. 10 minutes:
REM   powershell -ExecutionPolicy Bypass -File install_windows_task.ps1 -IntervalMinutes 10
REM ============================================================
echo ============================================================
echo  Smart Inbox-to-Calendar Assistant - automatic background setup
echo ============================================================
echo.
echo One-time prerequisite (if not done yet):
echo     python "%~dp0src\agent.py" --authorize
echo     python "%~dp0src\agent.py" --once --enable
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_windows_task.ps1"
echo.
pause
