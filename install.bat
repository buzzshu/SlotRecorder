@echo off
rem SlotRecorder setup. Keep this file ASCII-only: cmd mis-parses UTF-8 Chinese text in .bat files.
rem Python 3.10 - 3.12 is installed automatically when missing: (a) winget, else (b) official installer from python.org
rem into SlotRecorder\python (silent, current user, no admin).
setlocal
cd /d "%~dp0"
set "PYVER=3.12.10"
set "LOCALPY=%~dp0python\python.exe"
echo ===== SlotRecorder setup =====
echo.

rem ---- 1. Python ----
set "PY="
if defined SLOTREC_TEST_NOPY goto py_missing
call :find_python
if defined PY goto py_ok

:py_missing
echo Python 3.10 - 3.12 not found - installing Python %PYVER% automatically...
if defined SLOTREC_TEST_NOWINGET goto py_download
where winget >nul 2>nul
if errorlevel 1 goto py_download
echo [a] winget: Python 3.12 for the current user - no admin needed, takes 1-2 minutes...
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
call :find_python
if defined PY goto py_ok
echo     winget did not work - using the official installer instead.

:py_download
echo [b] Downloading the official Python %PYVER% installer from python.org - about 27 MB...
set "PYEXE=%TEMP%\slotrec-python-%PYVER%-amd64.exe"
set "PYURL=https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-amd64.exe"
curl.exe -L --fail -s -S -o "%PYEXE%" "%PYURL%"
if not exist "%PYEXE%" powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -UseBasicParsing -Uri '%PYURL%' -OutFile '%PYEXE%'"
if not exist "%PYEXE%" goto py_fail
echo     Installing into the SlotRecorder\python folder - silent, about 1 minute...
"%PYEXE%" /quiet InstallAllUsers=0 PrependPath=0 Include_launcher=0 Include_test=0 Include_doc=0 Shortcuts=0 AssociateFiles=0 TargetDir="%~dp0python"
del "%PYEXE%" >nul 2>nul
if exist "%LOCALPY%" set PY="%LOCALPY%"
if defined PY goto py_ok

:py_fail
echo [!] Could not install Python automatically - no internet, or blocked by the company network?
echo     Install by hand: https://www.python.org/downloads/release/python-31210/  "Windows installer 64-bit"
echo     Tick "Add python.exe to PATH" during setup, then run install.bat again.
pause
exit /b 1

:py_ok
echo Python: %PY%
%PY% -c "import tkinter" >nul 2>nul
if errorlevel 1 echo [!] This Python has no tkinter - the window will not open. Reinstall Python with "tcl/tk" ticked.
%PY% -c "import sys, os; open('python_dir.txt', 'w', encoding='oem', errors='replace').write(os.path.dirname(sys.executable))"

echo [1/3] Python packages...
%PY% -m pip install --disable-pip-version-check --no-warn-script-location -r requirements.txt
if errorlevel 1 (
  echo [!] Package install failed. If the message mentions onnxruntime / DLL, install the VC++ runtime:
  echo     https://aka.ms/vs/17/release/vc_redist.x64.exe
  pause
  exit /b 1
)

where winget >nul 2>nul
if errorlevel 1 (
  echo [!] winget not available - install scrcpy / ffmpeg manually, see below.
  goto check
)
echo [2/3] scrcpy - recording...
where scrcpy >nul 2>nul || winget install --id Genymobile.scrcpy -e --accept-source-agreements --accept-package-agreements
echo [3/3] ffmpeg - merging / analysis...
where ffmpeg >nul 2>nul || winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements

:check
echo.
%PY% -c "import sys; sys.path.insert(0, '.'); from core.config import find_tools; t = find_tools(); bad = [k for k in ('scrcpy', 'ffmpeg', 'ffprobe', 'adb') if not t[k]]; print('Tool check: ' + ('all found' if not bad else 'NOT found: ' + ', '.join(bad))); sys.exit(1 if bad else 0)"
if errorlevel 1 (
  echo.
  echo Manual install: unzip into SlotRecorder\tools\ - the program finds them there.
  echo   scrcpy: https://github.com/Genymobile/scrcpy/releases   file scrcpy-win64-*.zip
  echo   ffmpeg: https://www.gyan.dev/ffmpeg/builds/            file ffmpeg-release-essentials.zip
  echo If winget just installed them, close this window and run install.bat once more.
  pause
  exit /b 1
)
echo.
echo Setup done. Double-click run.bat to open SlotRecorder, then press the environment check button first.
pause
goto :eof

rem ---- finds an installed Python 3.10 - 3.12 -> PY ----
:find_python
set "PY="
if exist "%LOCALPY%" set PY="%LOCALPY%"
if defined PY goto :eof
for %%v in (3.12 3.11 3.10) do if not defined PY py -%%v -c "import sys" >nul 2>nul && set "PY=py -%%v"
if defined PY goto :eof
python -c "import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,12) else 1)" >nul 2>nul && set "PY=python"
if defined PY goto :eof
for %%v in (312 311 310) do if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe"
goto :eof
