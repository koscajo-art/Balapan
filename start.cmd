@echo off
cd /d "%~dp0"
python -c "import fastapi, uvicorn, jinja2, multipart" 2>nul
if errorlevel 1 (
    python -m pip install -r requirements.txt
    if errorlevel 1 exit /b 1
)
echo Balapan: http://127.0.0.1:8013
python -m uvicorn main:app --host 127.0.0.1 --port 8013
pause
