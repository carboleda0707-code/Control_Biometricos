@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "APP_HOST=0.0.0.0"
set "APP_PORT=8080"
set "EXE_NAME=servidor_biometrico.exe"

if exist "config.ini" (
    for /f "usebackq tokens=1,* delims==" %%A in (`findstr /r /i "^[ ]*HOST[ ]*=" config.ini`) do (
        set "VAL=%%B"
        set "VAL=!VAL: =!"
        if not "!VAL!"=="" set "APP_HOST=!VAL!"
    )
    for /f "usebackq tokens=1,* delims==" %%A in (`findstr /r /i "^[ ]*PORT[ ]*=" config.ini`) do (
        set "VAL=%%B"
        set "VAL=!VAL: =!"
        if not "!VAL!"=="" set "APP_PORT=!VAL!"
    )
)

if not exist "%EXE_NAME%" (
    echo [ERROR] No se encontro %EXE_NAME% en %cd%.
    exit /b 1
)

echo [INICIANDO] Servicio Biometrico en %APP_HOST%:%APP_PORT%...
"%~dp0%EXE_NAME%"
if errorlevel 1 (
    echo [ERROR] El servicio finalizo con error. Revisa los logs y config.ini.
    exit /b 1
)