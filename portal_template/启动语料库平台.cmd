@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting the unified corpus platform: sign in, build and share.
echo First use: create your account in this window. Then open the browser page.
echo Default address: http://127.0.0.1:8000/
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" -Stage api
set "CORPUS_EXIT=%ERRORLEVEL%"
if not "%CORPUS_EXIT%"=="0" echo Corpus platform failed to start. Review the error above.
if "%CORPUS_EXIT%"=="0" echo The platform is running or its page has been opened.
echo Press any key to close this launcher window.
pause >nul
exit /b %CORPUS_EXIT%
