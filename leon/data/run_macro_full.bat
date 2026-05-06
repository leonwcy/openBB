@echo off
REM Full macro backfill (e.g. first run 5y history).
REM Python resolution order:
REM 1) LEON_PYTHON env var (recommended system/user env var)
REM 2) python from PATH
REM 3) py -3 launcher from PATH

set "SCRIPT=%~dp0ingest_macro_full.py"
set "LOG_DIR=%~dp0logs"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%i
set "LOG_FILE=%LOG_DIR%\macro_full_%TODAY%.log"
set "KEEP_DAYS=180"

cd /d "%~dp0"
set "PY_CMD="
if defined LEON_PYTHON set "PY_CMD="%LEON_PYTHON%""
if not defined PY_CMD (
    where python >nul 2>&1
    if %ERRORLEVEL%==0 set "PY_CMD=python"
)
if not defined PY_CMD (
    where py >nul 2>&1
    if %ERRORLEVEL%==0 set "PY_CMD=py -3"
)
if not defined PY_CMD (
    echo [ERROR] Python not found. Set LEON_PYTHON or add python/py to PATH. >> "%LOG_FILE%"
    exit /b 1
)
echo [%DATE% %TIME%] START ingest_macro_full.py python="%PY_CMD%" >> "%LOG_FILE%"
call %PY_CMD% "%SCRIPT%" >> "%LOG_FILE%" 2>&1
set EXIT_CODE=%ERRORLEVEL%
echo [%DATE% %TIME%] END ingest_macro_full.py exit_code=%EXIT_CODE% >> "%LOG_FILE%"
forfiles /P "%LOG_DIR%" /M *.log /D -%KEEP_DAYS% /C "cmd /c del /q @path" >nul 2>&1
exit /b %EXIT_CODE%
