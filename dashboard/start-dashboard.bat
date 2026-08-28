@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Open the browser ~1s later so the server is already listening (avoids a
rem connection-refused first load), then run the server in the foreground.
start "dashboard-opener" cmd /c "timeout /t 1 /nobreak >nul & start http://localhost:8420/dashboard.html"
node server.js
pause
