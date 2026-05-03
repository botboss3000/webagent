@echo off
:: Use only after nothing else is listening on port 8000 (no WinError 10048).
set WEBAGENT_PORT=8000
call "%~dp0start_webAgent.bat"
