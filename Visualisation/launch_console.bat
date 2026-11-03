@echo off
setlocal
cd /d "%~dp0"
if not exist node_modules (
  echo Installing dependencies...
  call npm install
  if errorlevel 1 exit /b %errorlevel%
)
set "PORT=%PORT%"
if "%PORT%"=="" set "PORT=5173"
set "BOARD_IP=%BOARD_IP%"
if "%BOARD_IP%"=="" set "BOARD_IP=192.168.31.153"
set "TRANSPORT=%TRANSPORT%"
if "%TRANSPORT%"=="" set "TRANSPORT=Weston/Wayland stream"
set "STREAM_TARGET=%STREAM_TARGET%"
if "%STREAM_TARGET%"=="" set "STREAM_TARGET=%BOARD_IP%:8765"
set "VNC_TARGET=%VNC_TARGET%"
if "%VNC_TARGET%"=="" set "VNC_TARGET=%BOARD_IP%:5900"
echo RK3576 remote console: http://localhost:%PORT%
echo Board: %BOARD_IP%
echo Transport: %TRANSPORT%
echo Stream target: %STREAM_TARGET%
echo Legacy VNC target: %VNC_TARGET%
node server.mjs
