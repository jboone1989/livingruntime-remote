@echo off
setlocal
python "%~dp0tunnel.py" %*
exit /b %ERRORLEVEL%
