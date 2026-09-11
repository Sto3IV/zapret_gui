@echo off
setlocal EnableExtensions
rem Zapret Control operator launcher.
rem Relocate to this script's directory so python -m zapret_gui finds bin\ and lists\.
cd /d "%~dp0"
if errorlevel 1 (
  echo ERROR: Cannot change directory to "%~dp0"
  if "%~1"=="" pause
  exit /b 1
)

set "PYTHON="
for /f "delims=" %%I in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON=%%I"
if not defined PYTHON (
  for /f "delims=" %%I in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON=%%I"
)
if not defined PYTHON (
  echo ERROR: Python 3.10+ was not found on PATH.
  echo Install Python from https://www.python.org/downloads/ and enable "Add python.exe to PATH".
  echo Then run: python -m pip install -r requirements.txt
  if "%~1"=="" pause
  exit /b 1
)

"%PYTHON%" -m zapret_gui.bootstrap --ensure
if errorlevel 1 (
  echo ERROR: Failed to verify or install GUI requirements from requirements.txt
  if "%~1"=="" pause
  exit /b 1
)

rem After a successful check: python -m zapret_gui %*
"%PYTHON%" -m zapret_gui %*
exit /b %ERRORLEVEL%
