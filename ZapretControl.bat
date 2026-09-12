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

rem Collect the GUI arguments, pulling out our own --no-elevate switch.
set "GUIARGS="
set "NOELEV="
:collect
if "%~1"=="" goto collected
if /i "%~1"=="--no-elevate" (
  set "NOELEV=1"
  shift
  goto collect
)
set GUIARGS=%GUIARGS% %1
shift
goto collect
:collected

rem sc create/start/stop needs an elevated token, the same one service.bat asks
rem for at startup. Taking it once here lets every sc.exe call run in-process
rem with its output captured, instead of flashing past in a per-action UAC
rem console the GUI cannot read. --no-elevate skips this (CI, --smoke, or a
rem --root path containing spaces, which does not survive the relaunch).
if defined NOELEV goto elevated
net session >nul 2>&1
if not errorlevel 1 goto elevated
where powershell >nul 2>&1
if errorlevel 1 (
  echo ERROR: powershell.exe was not found, cannot request administrator rights.
  echo Start this script from an elevated prompt, or pass --no-elevate.
  pause
  exit /b 1
)
echo Requesting admin rights...
if defined GUIARGS (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%GUIARGS%' -Verb RunAs -WorkingDirectory '%~dp0'"
) else (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs -WorkingDirectory '%~dp0'"
)
exit /b 0

:elevated
set "PYTHON="
for /f "delims=" %%I in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON=%%I"
if not defined PYTHON (
  for /f "delims=" %%I in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PYTHON=%%I"
)
if not defined PYTHON (
  echo ERROR: Python 3.10+ was not found on PATH.
  echo Install Python from https://www.python.org/downloads/ and enable "Add python.exe to PATH".
  echo Then run: python -m pip install -r requirements.txt
  pause
  exit /b 1
)

"%PYTHON%" -m zapret_gui.bootstrap --ensure
if errorlevel 1 (
  echo ERROR: Failed to verify or install GUI requirements from requirements.txt
  pause
  exit /b 1
)

rem After a successful check: python -m zapret_gui %*
"%PYTHON%" -m zapret_gui%GUIARGS%
exit /b %ERRORLEVEL%
