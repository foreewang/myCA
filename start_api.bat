@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Missing .venv\Scripts\python.exe
    echo Create the Python 3.10 environment according to 服务端口与启动说明.md first.
    exit /b 1
)

set "COLONY_API_WORKERS=1"
set "UVICORN_WORKERS=1"
set "WEB_CONCURRENCY=1"
set "COLONY_LOG_REDACT_SENSITIVE=1"

echo Validating deployment configuration...
".venv\Scripts\python.exe" -m workflow.deployment_preflight
if errorlevel 1 (
    echo [ERROR] Python or locked runtime preflight failed. API was not started.
    exit /b 1
)

".venv\Scripts\python.exe" -m workflow.config_validator
if errorlevel 1 (
    echo [ERROR] Configuration validation failed. API was not started.
    exit /b 1
)

echo Starting Colony System API on 0.0.0.0:8000 with one worker...
".venv\Scripts\python.exe" -m uvicorn workflow.api_server:app --host 0.0.0.0 --port 8000 --workers 1 --lifespan on --timeout-graceful-shutdown 45
set "API_EXIT_CODE=%ERRORLEVEL%"

echo Colony System API exited with code %API_EXIT_CODE%.
exit /b %API_EXIT_CODE%
