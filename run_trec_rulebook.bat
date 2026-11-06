@echo off
REM Backward-compatible name for the launcher.
call "%~dp0start.bat" %*
exit /b %errorlevel%
