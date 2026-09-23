@echo off
title Bahis Kuponu Asistani
cd /d "%~dp0"

if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
)

echo Bahis Kuponu Asistani baslatiliyor, birkac saniye icinde tarayicida acilacak...
echo Uygulamayi kapatmak icin bu pencereyi kapatin.
echo.

start "" /min cmd /c "timeout /t 4 /nobreak >nul & start "" http://localhost:8501"

streamlit run app.py --server.headless true

if errorlevel 1 (
    echo.
    echo Bir hata olustu. Yukaridaki mesaji inceleyin.
    pause
)
