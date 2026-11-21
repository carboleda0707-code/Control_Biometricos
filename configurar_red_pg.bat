@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "PG_DATA=C:\Program Files\PostgreSQL\17\data"
set "PG_SERVICE=postgresql-x64-17"
set "VLAN_CIDR=172.16.6.0/24"
set "DB_NAME=bananeras_db"
set "AUTH_METHOD=scram-sha-256"

net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Ejecuta este archivo como Administrador.
    pause
    exit /b 1
)

if not exist "%PG_DATA%\postgresql.conf" (
    echo [ERROR] No se encontro postgresql.conf en %PG_DATA%.
    pause
    exit /b 1
)

if not exist "%PG_DATA%\pg_hba.conf" (
    echo [ERROR] No se encontro pg_hba.conf en %PG_DATA%.
    pause
    exit /b 1
)

echo [1/4] Respaldando postgresql.conf y pg_hba.conf...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ts = Get-Date -Format 'yyyyMMdd_HHmmss'; " ^
  "Copy-Item '%PG_DATA%\postgresql.conf' '%PG_DATA%\postgresql.conf.bak_'$ts; " ^
  "Copy-Item '%PG_DATA%\pg_hba.conf' '%PG_DATA%\pg_hba.conf.bak_'$ts"

echo [2/4] Configurando listen_addresses y puerto 5432 en postgresql.conf...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$f = '%PG_DATA%\postgresql.conf'; " ^
  "$c = Get-Content $f -Raw -Encoding UTF8; " ^
  "if ($c -match '(?m)^#?\s*listen_addresses\s*=.*$') { $c = [regex]::Replace($c, '(?m)^#?\s*listen_addresses\s*=.*$', \"listen_addresses = '*'\") } else { $c += \"`r`nlisten_addresses = '*'\" }; " ^
  "if ($c -match '(?m)^#?\s*port\s*=.*$') { $c = [regex]::Replace($c, '(?m)^#?\s*port\s*=.*$', 'port = 5432') } else { $c += \"`r`nport = 5432\" }; " ^
  "Set-Content -Path $f -Value $c -Encoding UTF8"

echo [3/4] Agregando regla para %VLAN_CIDR% en pg_hba.conf...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$h = '%PG_DATA%\pg_hba.conf'; " ^
  "$hc = Get-Content $h -Raw -Encoding UTF8; " ^
  "$regla = \"host    %DB_NAME%    all             %VLAN_CIDR%           %AUTH_METHOD%\"; " ^
  "if ($hc -notmatch [regex]::Escape('%VLAN_CIDR%')) { $hc += \"`r`n# Acceso VLAN RRHH`r`n$regla`r`n\"; Set-Content -Path $h -Value $hc -Encoding UTF8 }"

echo [4/4] Reiniciando servicio %PG_SERVICE% y verificando puerto 5432...
netsh advfirewall firewall show rule name="BioControl PostgreSQL 5432" >nul 2>&1
if errorlevel 1 (
    netsh advfirewall firewall add rule name="BioControl PostgreSQL 5432" dir=in action=allow protocol=TCP localport=5432 profile=private,domain,public >nul 2>&1
)

Restart-Service -Name "%PG_SERVICE%" -Force
timeout /t 3 /nobreak >nul
netstat -ano | findstr :5432

echo [OK] Configuracion de red aplicada.
pause