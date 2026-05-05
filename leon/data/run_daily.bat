@echo off
REM Schedule this in Windows Task Scheduler (daily after US close).
REM Edit PYTHON to your Python 3.10+ that has openbb + sqlalchemy installed.

set DEFAULT_PYTHON=E:\python\Python312\python.exe
set VENV_PYTHON=%~dp0..\..\.venv2\Scripts\python.exe
if exist "%VENV_PYTHON%" (
    set PYTHON=%VENV_PYTHON%
) else (
    set PYTHON=%DEFAULT_PYTHON%
)
set SCRIPT=%~dp0ingest_daily_snapshot.py
set LOG_DIR=%~dp0logs

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%i
set LOG_FILE=%LOG_DIR%\daily_snapshot_%TODAY%.log
set KEEP_DAYS=180

cd /d "%~dp0"
echo [%DATE% %TIME%] START ingest_daily_snapshot.py python="%PYTHON%" >> "%LOG_FILE%"
"%PYTHON%" "%SCRIPT%" >> "%LOG_FILE%" 2>&1
set EXIT_CODE=%ERRORLEVEL%
echo [%DATE% %TIME%] END ingest_daily_snapshot.py exit_code=%EXIT_CODE% >> "%LOG_FILE%"
forfiles /P "%LOG_DIR%" /M *.log /D -%KEEP_DAYS% /C "cmd /c del /q @path" >nul 2>&1
exit /b %EXIT_CODE%
