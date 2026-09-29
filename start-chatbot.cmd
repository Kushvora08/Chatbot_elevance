@echo off
setlocal
cd /d "%~dp0"
set "BUNDLED_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" "%~dp0start_chatbot.py" %*
) else if exist "%BUNDLED_PYTHON%" (
  "%BUNDLED_PYTHON%" "%~dp0start_chatbot.py" %*
) else (
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3 "%~dp0start_chatbot.py" %*
  ) else (
    python "%~dp0start_chatbot.py" %*
  )
)
if errorlevel 1 (
  echo.
  echo Startup failed. Python 3.10 or newer is required. Keep the error above when asking for help.
  pause
)
