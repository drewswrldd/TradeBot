@echo off
REM ATS Trading Bot Startup Script
REM Starts ngrok tunnel for TradingView webhook reception

setlocal

REM Configuration
set NGROK_DOMAIN=kamron-unwarming-allegra.ngrok-free.dev
set LOCAL_PORT=8080
set LOG_DIR=C:\Users\duckm\TradeBot\logs
set NGROK_LOG=%LOG_DIR%\ngrok.log

REM Ensure log directory exists
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM Log startup time
echo [%date% %time%] Starting ATS Bot services... >> "%NGROK_LOG%"

REM Kill any existing ngrok processes to avoid conflicts
taskkill /f /im ngrok.exe >nul 2>&1

REM Start ngrok with logging
echo [%date% %time%] Starting ngrok tunnel to %NGROK_DOMAIN%... >> "%NGROK_LOG%"
start "" /min ngrok http %LOCAL_PORT% --host-header=localhost --domain=%NGROK_DOMAIN% --log=stdout >> "%NGROK_LOG%" 2>&1

REM Give ngrok time to establish tunnel
timeout /t 5 /nobreak >nul

echo [%date% %time%] ngrok tunnel started. >> "%NGROK_LOG%"
echo ATS Bot startup complete. Check %NGROK_LOG% for details.

endlocal
