@echo off
setlocal
cd /d %~dp0

if not exist backend\.venv (
  echo Creating Python virtual environment...
  python -m venv backend\.venv
)

start "IKSphere FastAPI" cmd /k "cd /d %~dp0backend && call .venv\Scripts\activate.bat && python -m pip install -r requirements.txt && python -m uvicorn app.main:app --reload --port 8000"
start "IKSphere React" cmd /k "cd /d %~dp0frontend && if not exist node_modules (npm install) && npm run dev"

echo.
echo IKSphere Mobile is starting.
echo Frontend: http://localhost:5173
echo Backend:  http://localhost:8000/docs
endlocal
