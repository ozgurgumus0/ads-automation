@echo off
cd /d "%~dp0"
echo Installing dependencies...
pip install -r requirements.txt
echo.
echo Starting Ads Sync...
echo.
echo ==========================================
echo   Open http://localhost:5000 in browser
echo ==========================================
echo.
start "" "http://localhost:5000"
python app.py
pause
