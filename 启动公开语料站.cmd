@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Opening the public corpus website: http://127.0.0.1:8002/
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" -Stage public-site
set "CORPUS_EXIT=%ERRORLEVEL%"
if not "%CORPUS_EXIT%"=="0" echo Public site failed. Review the message above.
if not "%CORPUS_EXIT%"=="0" pause
exit /b %CORPUS_EXIT%
