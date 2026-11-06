@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "APP_NAME=TRECRulebook"
set "MODE=onefile"
if /I "%~1"=="onedir" set "MODE=onedir"
if /I "%~1"=="onefile" set "MODE=onefile"

echo ============================================================
echo  Building %APP_NAME%.exe  (%MODE%)
echo ============================================================
echo.

REM ---- locate Python 3.10+ ------------------------------------------------
set "PY="
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    where python >nul 2>nul
    if not errorlevel 1 (
        python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
        if not errorlevel 1 set "PY=python"
    )
)
if not defined PY (
    echo ERROR: Python 3.10 or newer was not found.
    echo Install it from https://www.python.org/downloads/
    goto :build_failed
)

%PY% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python 3.10 or newer is required.
    goto :build_failed
)

REM ---- make sure PyInstaller is available --------------------------------
%PY% -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo PyInstaller is not installed for this Python. Installing it...
    %PY% -m pip install --disable-pip-version-check --upgrade pyinstaller
    if errorlevel 1 (
        echo Retrying with a per-user install...
        %PY% -m pip install --disable-pip-version-check --user --upgrade pyinstaller
        if errorlevel 1 (
            echo ERROR: Could not install PyInstaller.
            echo Check your internet connection and pip permissions.
            goto :build_failed
        )
    )
)

if not exist "build" mkdir "build"
if not exist "dist" mkdir "dist"

set "MODE_FLAG=--onefile"
if /I "%MODE%"=="onedir" set "MODE_FLAG=--onedir"
set "DATA_DIR=%CD%\data"

echo.
echo Running PyInstaller...
%PY% -m PyInstaller ^
    %MODE_FLAG% ^
    --noconfirm ^
    --clean ^
    --noupx ^
    --windowed ^
    --name "%APP_NAME%" ^
    --add-data "%DATA_DIR%;data" ^
    --distpath "dist" ^
    --workpath "build" ^
    --specpath "build" ^
    "app.py"

if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller failed. Review the output above.
    goto :build_failed
)

echo.
if /I "%MODE%"=="onedir" (
    set "EXE_PATH=dist\%APP_NAME%\%APP_NAME%.exe"
) else (
    set "EXE_PATH=dist\%APP_NAME%.exe"
)

if not exist "%EXE_PATH%" (
    echo ERROR: Build finished but %EXE_PATH% was not found.
    goto :build_failed
)

echo ============================================================
echo  Built: %EXE_PATH%
echo  Start it with: start.bat
echo ============================================================
echo.
echo Opening the dist folder...
start "" explorer "dist"
pause
exit /b 0

:build_failed
echo.
pause
exit /b 1

