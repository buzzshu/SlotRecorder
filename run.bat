@echo off
rem Start the SlotRecorder GUI. Keep this file ASCII-only (cmd mis-parses UTF-8 Chinese in .bat files).
setlocal
cd /d "%~dp0"
set "PYW="
set "PYDIR="
rem 1. Python installed into this folder by install.bat  2. the Python install.bat found  3. any pythonw on PATH
if exist "%~dp0python\pythonw.exe" set "PYW=%~dp0python\pythonw.exe"
if exist python_dir.txt set /p PYDIR=<python_dir.txt
if not defined PYW if defined PYDIR if exist "%PYDIR%\pythonw.exe" set "PYW=%PYDIR%\pythonw.exe"
if not defined PYW ( where pythonw >nul 2>nul && set "PYW=pythonw" )
if not defined PYW ( where pyw >nul 2>nul && set "PYW=pyw" )
if not defined PYW (
  echo Python not found - run install.bat first.
  pause
  exit /b 1
)
start "" "%PYW%" gui.py
