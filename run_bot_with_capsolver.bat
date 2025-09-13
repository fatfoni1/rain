@echo off
echo ========================================
echo   BOT CDP dengan CAPSOLVER INTEGRATION
echo ========================================
echo.

echo [1] Checking Python installation...
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python tidak terinstall atau tidak ada di PATH
    echo Silakan install Python terlebih dahulu
    pause
    exit /b 1
)
echo Python OK

echo.
echo [2] Installing/Updating dependencies...
pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Gagal install dependencies
    pause
    exit /b 1
)

echo.
echo [3] Testing Capsolver integration...
python test_capsolver.py
if errorlevel 1 (
    echo ERROR: Test Capsolver gagal
    echo Silakan cek konfigurasi di bot_config.json
    pause
    exit /b 1
)

echo.
echo [4] Starting bot...
echo Bot akan mulai dalam 3 detik...
timeout /t 3 /nobreak >nul

echo.
echo ========================================
echo   BOT STARTED - Press Ctrl+C to stop
echo ========================================
python bot_cdp.py

echo.
echo Bot stopped.
pause