@echo off
chcp 65001 >nul
REM WiFi OTA RvR 실행 런처 (Windows)
set "PY=C:\Users\ASUS\AppData\Local\Programs\Python\Python312\python.exe"
"%PY%" "%~dp0wifi_ota_rvr.py" %*
pause
