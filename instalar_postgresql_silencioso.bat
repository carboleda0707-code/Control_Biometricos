@echo off
setlocal EnableDelayedExpansion
:: ============================================================================
:: INSTALADOR SILENCIOSO DE POSTGRESQL 17.12 (WINDOWS X64)
:: Ejecutar como Administrador
:: ============================================================================
cd /d "%~dp0"

set "PG_INSTALLER=postgresql-17.12-2-windows-x64.exe"
set "PG_PASS=Litos0707"
set "PG_PORT=5432"

net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Ejecuta este archivo como Administrador.
    pause
    exit /b 1
)

if not exist "%PG_INSTALLER%" (
    echo [ERROR] No se encontro %PG_INSTALLER% en %cd%.
    echo Copia postgresql-17.12-2-windows-x64.exe a esta carpeta antes de continuar.
    pause
    exit /b 1
)

echo [1/2] Instalando PostgreSQL 17.12 en puerto %PG_PORT%...
"%~dp0%PG_INSTALLER%" ^
  --mode unattended ^
  --unattendedmodeui none ^
  --superpassword "%PG_PASS%" ^
  --serverport %PG_PORT% ^
  --enable-components server,commandlinetools ^
  --disable-components pgadmin,stackbuilder

if errorlevel 1 (
    echo [ERROR] Fallo la instalacion silenciosa de PostgreSQL.
    pause
    exit /b 1
)

echo [2/2] Verificando servicio de PostgreSQL...
timeout /t 5 /nobreak >nul
sc query state= running | findstr /i "postgresql" >nul 2>&1
if errorlevel 1 (
    echo [AVISO] El servicio aun no figura en ejecucion. Revisa services.msc.
) else (
    echo [OK] PostgreSQL 17.12 instalado y corriendo en el puerto %PG_PORT%.
)

pause