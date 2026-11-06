@echo off
rem Same as run.bat but keeps a console window open so errors are visible.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m fupan --debug
pause
