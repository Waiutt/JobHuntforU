@echo off
rem JobHuntBot dashboard launcher (idempotent).
rem If the server is already listening on 8420, it just opens the browser;
rem otherwise it starts the server, waits until ready, then opens the page.
rem Real logic lives in _launch_dashboard.ps1 (the desktop shortcut calls the same script).
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0_launch_dashboard.ps1"
