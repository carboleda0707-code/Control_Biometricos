@echo off
setlocal EnableDelayedExpansion
:: ============================================================================
:: INSTALADOR SERVIDOR COMPILADO - CONTROL BIOMETRICO BANANERAS (PC LIMPIO)
:: Ejecutar como Administrador para habilitar reglas en el Firewall de Windows
:: ============================================================================
cd /d "%~dp0"

set "EXE_NAME=servidor_biometrico.exe"
set "FW_PORT=8081"

echo [0/4] Verificando permisos de Administrador...
net session >nul 2>&1
if errorlevel 1 (
    echo [AVISO] No se detectaron privilegios de Administrador.
    echo Haz clic derecho sobre este archivo y selecciona "Ejecutar como administrador"
    echo para garantizar la creacion de las reglas en el Firewall de Windows.
    pause
)

echo [1/4] Verificando ejecutable compilado del servidor...
if not exist "%EXE_NAME%" (
    echo [ERROR] No se encontro %EXE_NAME% en %cd%.
    echo Copia el archivo compilado %EXE_NAME% en esta carpeta antes de continuar.
    pause
    exit /b 1
)
echo [OK] Ejecutable encontrado: %EXE_NAME%

echo [2/4] Configurando config.ini...
if exist "config.ini" (
    echo [INFO] config.ini ya existe. Se conserva la configuracion actual.
) else (
    (
    echo [DEFAULT]
    echo DEBUG = false
    echo TIMEZONE = America/Guayaquil
    echo.
    echo [SERVER]
    echo HOST = 0.0.0.0
    echo PORT = 8081
    echo.
    echo [DATABASE]
    echo HOST = localhost
    echo PORT = 5432
    echo USER = postgres
    echo PASSWORD = Litos0707
    echo NAME = bananeras_db
    echo URL = postgresql://postgres:Litos0707@localhost:5432/bananeras_db
    echo.
    echo [SECURITY]
    echo BRIDGE_SECRET_KEY = clave_segura_puente_2026
    echo.
    echo [EMPACADORAS]
    echo CODIGOS = EMP01,EMP02,EMP03,EMP04,EMP05
    echo.
    echo [COMEDOR]
    echo HORA_INICIO = 11:00:00
    echo HORA_FIN = 14:00:00
    ) > config.ini
    echo [OK] config.ini generado con valores por defecto.
    echo [AVISO] Revisa USER, PASSWORD y NAME en config.ini antes de arrancar en produccion.
)

if exist "config.ini" (
    for /f "usebackq tokens=1,* delims==" %%A in (`findstr /r /i "^[ ]*PORT[ ]*=" config.ini`) do (
        set "VAL=%%B"
        set "VAL=!VAL: =!"
        if not "!VAL!"=="" set "FW_PORT=!VAL!"
    )
)

echo [3/4] Configurando reglas de Firewall de Windows (TCP 5432 y TCP %FW_PORT%)...
netsh advfirewall firewall delete rule name="BioControl PostgreSQL 5432" >nul 2>&1
netsh advfirewall firewall add rule name="BioControl PostgreSQL 5432" dir=in action=allow protocol=TCP localport=5432 profile=private,domain,public >nul 2>&1
if errorlevel 1 (
    echo [ERROR] No se pudo crear la regla TCP 5432. Ejecuta este .bat como Administrador.
) else (
    echo [OK] Regla Firewall activa: TCP 5432 ^(private, domain, public^).
)

netsh advfirewall firewall delete rule name="BioControl Servicio %FW_PORT%" >nul 2>&1
netsh advfirewall firewall add rule name="BioControl Servicio %FW_PORT%" dir=in action=allow protocol=TCP localport=%FW_PORT% profile=private,domain,public >nul 2>&1
if errorlevel 1 (
    echo [ERROR] No se pudo crear la regla TCP %FW_PORT%. Ejecuta este .bat como Administrador.
) else (
    echo [OK] Regla Firewall activa: TCP %FW_PORT% ^(private, domain, public^).
)

echo [4/4] Verificando servicio de PostgreSQL...
sc query state= running | findstr /i "postgresql" >nul 2>&1
if errorlevel 1 (
    echo [AVISO] No se detecto un servicio de PostgreSQL en ejecucion.
    echo Instala PostgreSQL en el puerto 5432 y crea la base 'bananeras_db' antes de iniciar.
) else (
    echo [OK] Servicio de PostgreSQL detectado en ejecucion.
)

echo ============================================================================
echo Preparacion completada en %cd%.
echo 1. Verifica que exista la BD 'bananeras_db' y ejecuta tu .sql de roles readonly.
echo 2. Ajusta config.ini si tu clave de PostgreSQL o puerto difieren.
echo 3. Inicia el servicio con 'iniciar_sistema.bat'.
echo ============================================================================
pause