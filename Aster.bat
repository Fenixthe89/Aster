@echo off
chcp 65001 >nul
title Aster

echo.
echo ==========================================
echo             ASTER
echo ==========================================
echo.
echo Avvio in corso...
echo.

cd /d "%~dp0"

.venv\Scripts\python.exe aster.py

pause