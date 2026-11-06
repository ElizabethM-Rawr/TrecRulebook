@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM Prefer a built one-file EXE, then a one-folder EXE, then Python source.
if exist "TRECRulebook.exe" (
    start "" "TRECRulebook.exe" %*
    exit /b 0
)
if exist "dist\TRECRulebook.exe" (
    start "" "dist\TRECRulebook.exe" %*
    exit /b 0
)
if exist "dist\TRECRulebook\TRECRulebook.exe" (
    start "" "dist\TRECRulebook\TRECRulebook.exe" %*
    exit /b 0
)

where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw -3 "app.py" %*
    exit /b 0
)

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "app.py" %*
    exit /b 0
)

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "app.py" %*
    exit /b %errorlevel%
)

where python >nul 2>nul
if %errorlevel%==0 (
    python "app.py" %*
    exit /b %errorlevel%
)

echo.
echo Python was not found on this computer.
echo Install Python 3.10 or newer from https://www.python.org/downloads/
echo and make sure "Add Python to PATH" is selected.
echo.
pause
exit /b 1
