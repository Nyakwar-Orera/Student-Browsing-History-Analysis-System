@echo off
REM MamalBrowse Sentinel — Workstation Launcher
cd /d "C:\ProgramData\Sentinel"
set PYPATH=C:\Program Files\Python314\python.exe
if not exist "%PYPATH%" set PYPATH=C:\Program Files\Python313\python.exe
if not exist "%PYPATH%" set PYPATH=C:\Python311\python.exe
if not exist "%PYPATH%" set PYPATH=python
"%PYPATH%" "C:\ProgramData\Sentinel\browseguard_agent.py" >> "C:\ProgramData\Sentinel\agent.log" 2>&1

