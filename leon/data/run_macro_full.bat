@echo off
REM Full macro backfill (e.g. first run 5y history).
REM Edit PYTHON to your Python 3.10+ environment.

set DEFAULT_PYTHON=E:\python\Python312\python.exe
set VENV_PYTHON=%~dp0..\..\.venv2\Scripts\python.exe
if exist "%VENV_PYTHON%" (
    set PYTHON=%VENV_PYTHON%
) else (
    set PYTHON=%DEFAULT_PYTHON%
)
set SCRIPT=%~dp0ingest_macro_full.py

cd /d "%~dp0"
"%PYTHON%" "%SCRIPT%"
exit /b %ERRORLEVEL%
