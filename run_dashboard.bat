@echo off
title SathyaScan Web Platform
echo ========================================================
echo        Starting SathyaScan Web Platform
echo ========================================================
echo.

cd /d "%~dp0backend"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Python virtual environment not found in backend\.venv
    echo Please ensure the .venv folder is set up.
    echo.
    pause
    exit /b 1
)

echo Opening browser at http://localhost:8000/ ...
start "" http://localhost:8000/

echo.
echo Server is running on http://127.0.0.1:8000
echo Press Ctrl+C in this terminal window to stop the server.
echo.

.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000 --reload
