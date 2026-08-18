@echo off
REM ===================================================================
REM  OpenJarvis - one-time setup.
REM  Creates the virtual environment, installs everything including the
REM  voice extras, and creates your .env file. Safe to run again later.
REM ===================================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo   OpenJarvis setup
echo   ----------------
echo.

REM --- 1. Python -----------------------------------------------------
where python >nul 2>&1
if errorlevel 1 (
    echo   [X] Python was not found on your PATH.
    echo.
    echo       Install Python 3.10 or newer from https://python.org
    echo       and tick "Add Python to PATH" during installation.
    goto :fail
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
echo   [1/4] Python !PYVER! found.

REM --- 2. Virtual environment ----------------------------------------
if exist ".venv\Scripts\python.exe" (
    echo   [2/4] Virtual environment already exists.
) else (
    echo   [2/4] Creating virtual environment in .venv ...
    python -m venv .venv
    if errorlevel 1 goto :fail
)

set "VENV_PY=.venv\Scripts\python.exe"

REM --- 3. Dependencies -----------------------------------------------
echo   [3/4] Installing OpenJarvis + voice extras. This takes a few minutes.
echo         Downloading onnxruntime and numpy is the slow part.
echo.
"%VENV_PY%" -m pip install --upgrade pip --quiet
"%VENV_PY%" -m pip install -e ".[voice]"
if errorlevel 1 goto :fail
echo.

REM --- 4. API key ----------------------------------------------------
set "NEEDKEY="
if exist ".env" (
    echo   [4/4] .env already exists, leaving it alone.
) else (
    copy ".env.example" ".env" >nul
    set "NEEDKEY=1"
    echo   [4/4] Created .env from the template.
)

echo.
echo   ------------------------------------------------------------
"%VENV_PY%" -m openjarvis doctor
echo   ------------------------------------------------------------
echo.

if defined NEEDKEY (
    echo   NEXT STEP: open .env and paste in your API key.
    echo.
    echo     GROQ_API_KEY=gsk_...
    echo.
    echo   Groq has a free tier and the fastest transcription.
    echo   Get a key at https://console.groq.com/keys
    echo.
    choice /c YN /n /m "   Open .env now? [Y/N] "
    if !errorlevel!==1 notepad ".env"
    echo.
)

echo   Setup complete. Start OpenJarvis with:  jarvis.bat
echo.
pause
exit /b 0

:fail
echo.
echo   Setup failed. The error above says why.
echo.
pause
exit /b 1
