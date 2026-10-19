@echo off
rem First run creates .venv and installs dependencies, then starts the service.
rem   run.bat [--host 0.0.0.0] [--port 8780] [--token SECRET]
rem TORCH_INDEX picks the torch build (default CUDA 12.8; CPU only: https://download.pytorch.org/whl/cpu)
setlocal
cd /d "%~dp0"
if "%TORCH_INDEX%"=="" set TORCH_INDEX=https://download.pytorch.org/whl/cu128
if not exist .venv\Scripts\python.exe (
  python -m venv .venv || exit /b 1
  .venv\Scripts\python.exe -m pip install -q --upgrade pip
  .venv\Scripts\python.exe -m pip install -q torch torchaudio --index-url %TORCH_INDEX% || exit /b 1
  .venv\Scripts\python.exe -m pip install -q -r requirements.txt || exit /b 1
)
.venv\Scripts\python.exe run.py %*
