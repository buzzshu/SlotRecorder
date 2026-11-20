@echo off
rem SlotRecorder - one-click update: pull the latest CODE from the public repo (a few KB).
rem Your videos / logs / config.json / emulator apps are NOT in the repo and stay untouched.
rem First time on a PC: run setup_git.bat once. After that, just double-click this.
rem ASCII-ONLY on purpose: cmd mis-parses UTF-8 Chinese in .bat files on zh-TW (CP950) Windows.
cd /d "%~dp0"
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
  echo [!] This copy is not connected to the update repo yet -- run setup_git.bat first.
  echo.
  pause & exit /b 1
)
echo Updating SlotRecorder ...
git pull --ff-only
if errorlevel 1 (
  echo.
  echo [!] Update did not succeed. If the message mentions "local changes", a tool file
  echo     was edited by hand ^(do not edit tool files^). Screenshot it and send to buzzliu.
  echo.
  pause & exit /b 1
)
echo.
echo Done. Please restart SlotRecorder.
pause
