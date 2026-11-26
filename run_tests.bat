@echo off
rem Unit tests (seconds, no emulator needed), then the statistics regression check against your own recordings.
rem Keep this file ASCII-only (cmd mis-parses UTF-8 Chinese in .bat files).
setlocal
cd /d "%~dp0"
set "PY="
set "PYDIR="
if exist "%~dp0python\python.exe" set "PY=%~dp0python\python.exe"
if exist python_dir.txt set /p PYDIR=<python_dir.txt
if not defined PY if defined PYDIR if exist "%PYDIR%\python.exe" set "PY=%PYDIR%\python.exe"
if not defined PY set "PY=python"
set PYTHONIOENCODING=utf-8
"%PY%" -W ignore -m unittest discover -s tests
if errorlevel 1 goto :end
echo.
"%PY%" -m core.regress check
:end
pause
