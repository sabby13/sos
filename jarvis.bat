@echo off
REM ===================================================================
REM  OpenJarvis launcher.
REM
REM  Activates the virtual environment and runs OpenJarvis. Every
REM  argument is passed straight through, so this works exactly like
REM  the `jarvis` command:
REM
REM     jarvis.bat                       voice mode  (double-click this)
REM     jarvis.bat voice -k              push to talk, no wake word
REM     jarvis.bat chat                  text mode, no microphone
REM     jarvis.bat ask "open chrome"     one-shot
REM     jarvis.bat doctor                check the install
REM     jarvis.bat mic                   test the microphone
REM ===================================================================

setlocal
cd /d "%~dp0"

set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo.
    echo   OpenJarvis is not set up yet.
    echo   Run setup.bat first - it only takes a few minutes.
    echo.
    pause
    exit /b 1
)

"%VENV_PY%" -m openjarvis %*
set "CODE=%ERRORLEVEL%"

REM Keep the window open on a real failure so a double-clicked launch does
REM not vanish before you can read the error. Code 130 is Ctrl+C, which is
REM how you are meant to quit voice mode - that is not a failure.
if not "%CODE%"=="0" if not "%CODE%"=="130" (
    echo.
    echo   OpenJarvis exited with code %CODE%.
    pause
)

exit /b %CODE%
