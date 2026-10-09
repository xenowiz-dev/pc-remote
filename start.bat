@echo off
title PC Remote
cd /d "%~dp0"
python server.py %*
pause
