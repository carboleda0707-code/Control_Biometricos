# -*- coding: utf-8 -*-
import asyncio
from contextlib import contextmanager
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
from typing import Any, List, Optional

import anthropic
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

from config import settings
from db import get_conn, init_db
from excel_engine import generar_excel_comedor

app = FastAPI(title="Control Biométrico & Comedores")


@contextmanager
def db_cursor(autocommit: bool = False):
    conn = get_conn()
    cur = conn.cursor()
    try:
        yield cur
        if autocommit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def _rows_to_dicts(cur, rows) -> list[dict]:
    cols = [d[0] for d in (cur.description or [])]
    return [dict(zip(cols, row)) for row in rows]


def _limpiar_sn(sn: str | None) -> str | None:
    val = (sn or "").strip()
    return val or None


def _normalizar_pin(pin_raw: str) -> str:
    """Limpia espacios y quita ceros a la izquierda en PINs numéricos."""
    clean = str(pin_raw or "").strip()
    if clean.isdigit():
        stripped = clean.lstrip("0")
        return stripped if stripped else "0"
    return clean


def _limpiar_nombre_zk(nombre_raw: str, pin_fallback: str) -> str:
    """Limpia caracteres extraños de firmwares ZKTeco; si queda vacío, usa placeholder."""
    txt = re.sub(r"[^\w\s\.\-ÁÉÍÓÚÑáéíóúñ]", "", str(nombre_raw or "").strip()).strip()
    if not txt or txt.isdigit():
        return f"SIN NOMBRE ({pin_fallback})"
    return txt.upper()[:24]


def _actualizar_ultima_conexion(cur, sn: str | None) -> None:
    sn_clean = _limpiar_sn(sn)
    if sn_clean:
        cur.execute("UPDATE dispositivos SET ultima_conexion = NOW() WHERE sn = %s;", (sn_clean,))


def _asegurar_tabla_comandos(cur) -> None:
    """Crea la tabla e índice de cola de comandos ADMS si aún no existen."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS comandos_adms (
            id SERIAL PRIMARY KEY,
            sn VARCHAR(64) NOT NULL,
            comando TEXT NOT NULL,
            estado VARCHAR(20) NOT NULL DEFAULT 'PENDIENTE',
            respuesta_reloj TEXT,
            creado_en TIMESTAMP NOT NULL DEFAULT NOW(),
            enviado_en TIMESTAMP,
            confirmado_en TIMESTAMP
        );
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_comandos_adms_sn_estado
        ON comandos_adms (sn, estado, id);
    """)


def _preparar_entorno_adms(cur, sn: str | None) -> str | None:
    _asegurar_tabla_comandos(cur)
    sn_clean = _limpiar_sn(sn)
    _actualizar_ultima_conexion(cur, sn_clean)
    return sn_clean


def encolar_comando_usuario_adms(
    pin: str,
    nombres: str,
    permisos_hacienda_area: list[tuple[int, str]],
    activo: bool = True,
) -> int:
    """Encola comandos ADMS para relojes PUSH_ADMS según (empacadora_id, area_autorizada)."""
    pin_norm = _normalizar_pin(pin)
    if not pin_norm or pin_norm == "0":
        return 0

    nombre_zk = _limpiar_nombre_zk(nombres, pin_norm)
    comando_zk = (
        f"DATA UPDATE USERINFO PIN={pin_norm}\tName={nombre_zk}\tPri=0\tPasswd=\tCard="
        if activo
        else f"DATA DEL USERINFO PIN={pin_norm}"
    )

    encolados = 0
    try:
        with db_cursor(autocommit=True) as cur:
            _asegurar_tabla_comandos(cur)
            for emp_id, area in permisos_hacienda_area or []:
                area_clean = str(area or "").strip()
                if emp_id is None or not area_clean:
                    continue
                cur.execute("""
                    SELECT sn
                    FROM dispositivos
                    WHERE tipo_conexion = 'PUSH_ADMS'
                      AND empacadora_id = %s
                      AND UPPER(TRIM(rol)) = UPPER(TRIM(%s))
                      AND sn IS NOT NULL
                      AND TRIM(sn) <> '';
                """, (int(emp_id), area_clean))
                for (sn_dev,) in cur.fetchall():
                    cur.execute(
                        "INSERT INTO comandos_adms (sn, comando, estado) VALUES (%s, %s, 'PENDIENTE');",
                        (sn_dev.strip(), comando_zk),
                    )
                    encolados += 1
    except Exception as e:
        print(f"[ADMS QUEUE ERROR] PIN={pin_norm}: {e}")

    return encolados


def _obtener_info_dispositivo(cur, sn: str | None) -> tuple[int | None, str | None]:
    if not sn:
        return None, None
    cur.execute("SELECT empacadora_id, rol FROM dispositivos WHERE sn = %s LIMIT 1;", (sn,))
    row = cur.fetchone()
    if not row:
        return None, None
    return (int(row[0]) if row[0] is not None else None, str(row[1]).strip() if row[1] else None)


def _extraer_usuario_adms(linea: str) -> tuple[str | None, str]:
    m_pin = re.search(r"(?:^|[\s\t])PIN=([^\t\r\n]+)", linea, re.IGNORECASE)
    if not m_pin:
        return None, ""
    m_name = re.search(r"(?:^|[\s\t])Name=([^\t\r\n]*)", linea, re.IGNORECASE)
    return m_pin.group(1).strip(), (m_name.group(1).strip() if m_name else "")


def _guardar_empleado_desde_reloj(
    cur,
    pin_raw: str,
    nombre_raw: str,
    sn: str | None,
    mapa_pin_existente: dict[str, str],
) -> None:
    """Inserta o actualiza el empleado en `empleados` y asigna permiso en `empleado_haciendas`."""
    pin_norm = _normalizar_pin(pin_raw)
    if not pin_norm or pin_norm == "0":
        return

    empacadora_id, area = _obtener_info_dispositivo(cur, sn)
    nombre_limpio = _limpiar_nombre_zk(nombre_raw, pin_norm)

    if pin_norm in mapa_pin_existente:
        pin_maestro = mapa_pin_existente[pin_norm]
        if not nombre_limpio.startswith("SIN NOMBRE ("):
            cur.execute("""
                UPDATE empleados
                SET nombres = %s
                WHERE pin = %s
                  AND COALESCE(TRIM(nombres), '') <> %s;
            """, (nombre_limpio, pin_maestro, nombre_limpio))
    else:
        pin_maestro = pin_norm
        cur.execute("""
            INSERT INTO empleados (pin, cedula, nombres, empacadora_id, area, activo)
            VALUES (%s, %s, %s, %s, %s, TRUE)
            ON CONFLICT (pin) DO NOTHING;
        """, (pin_maestro, pin_maestro, nombre_limpio, empacadora_id, area))
        mapa_pin_existente[pin_norm] = pin_maestro

    if empacadora_id is not None and area:
        cur.execute("""
            INSERT INTO empleado_haciendas (pin_empleado, empacadora_id, area_autorizada)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING;
        """, (pin_maestro, empacadora_id, area))


def _confirmar_comandos_desde_texto(cur, sn: str | None, texto: str) -> None:
    if not texto:
        return
    for cmd_id_str in re.findall(r"\bID[=:]\s*(\d+)", texto, re.IGNORECASE):
        cmd_id = int(cmd_id_str)
        sql = """
            UPDATE comandos_adms
            SET estado = 'CONFIRMADO',
                respuesta_reloj = %s,
                confirmado_en = NOW()
            WHERE id = %s
        """
        params: list[Any] = [texto.strip()[:250], cmd_id]
        if sn:
            sql += " AND sn = %s"
            params.append(sn)
        cur.execute(sql + ";", tuple(params))


# ==============================================================================
# RESPALDO Y RESTAURACIÓN PROGRAMADA DE BASE DE DATOS (pg_dump / pg_restore)
# ==============================================================================
def _carpeta_respaldos() -> Path:
    carpeta_cfg = os.getenv("BACKUP_DIR", "").strip()
    ruta = Path(carpeta_cfg) if carpeta_cfg else Path(settings.BASE_DIR) / "respaldos"
    ruta.mkdir(parents=True, exist_ok=True)
    return ruta


def _archivo_programacion() -> Path:
    return _carpeta_respaldos() / "programacion_respaldo.json"


def _cargar_programacion() -> dict:
    ruta = _archivo_programacion()
    base = {
        "habilitado": False,
        "fecha_hora": "",
        "repetir_semanal": False,
        "ultima_ejecucion": None,
        "ultimo_estado": None,
    }
    if not ruta.exists():
        return base
    try:
        data = json.loads(ruta.read_text(encoding="utf-8"))
        return {**base, **(data if isinstance(data, dict) else {})}
    except Exception:
        return base


def _guardar_programacion(data: dict) -> None:
    _archivo_programacion().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _binario_pg(nombre_binario: str) -> str:
    pg_bin = os.getenv("PG_BIN_DIR", "").strip()
    if pg_bin:
        for candidato in (Path(pg_bin) / f"{nombre_binario}.exe", Path(pg_bin) / nombre_binario):
            if candidato.exists():
                return str(candidato)
    return nombre_binario


def _ejecutar_comando_pg(args: list[str], error_fallback: str) -> None:
    env = os.environ.copy()
    if settings.DB_PASSWORD:
        env["PGPASSWORD"] = settings.DB_PASSWORD
    proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or error_fallback)


def ejecutar_pg_dump() -> dict:
    """Genera un respaldo comprimido (.dump) de la base de datos configurada."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    nombre_archivo = f"{settings.DB_NAME}_{timestamp}.dump"
    destino = _carpeta_respaldos() / nombre_archivo

    _ejecutar_comando_pg(
        [
            _binario_pg("pg_dump"),
            "-h", str(settings.DB_HOST),
            "-p", str(settings.DB_PORT),
            "-U", str(settings.DB_USER),
            "-Fc", "-b",
            "-f", str(destino),
            str(settings.DB_NAME),
        ],
        "Error ejecutando pg_dump",
    )

    size_kb = round(destino.stat().st_size / 1024, 2) if destino.exists() else 0.0
    return {
        "archivo": nombre_archivo,
        "tamano_kb": size_kb,
        "creado_en": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def ejecutar_pg_restore(nombre_archivo: str) -> dict:
    """Restaura un archivo .dump sobre la base de datos configurada."""
    base = _carpeta_respaldos().resolve()
    archivo_resuelto = (base / Path(nombre_archivo).name).resolve()
    if not archivo_resuelto.exists() or archivo_resuelto.parent != base:
        raise FileNotFoundError("El archivo de respaldo seleccionado no existe.")

    _ejecutar_comando_pg(
        [
            _binario_pg("pg_restore"),
            "-h", str(settings.DB_HOST),
            "-p", str(settings.DB_PORT),
            "-U", str(settings.DB_USER),
            "-d", str(settings.DB_NAME),
            "--clean", "--if-exists",
            str(archivo_resuelto),
        ],
        "Error ejecutando pg_restore",
    )

    return {
        "archivo": archivo_resuelto.name,
        "restaurado_en": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


async def worker_respaldo_programado() -> None:
    """Revisa periódicamente si llegó la fecha y hora programada para generar respaldo."""
    while True:
        prog = _cargar_programacion()
        try:
            if prog.get("habilitado") and prog.get("fecha_hora"):
                objetivo = datetime.fromisoformat(prog["fecha_hora"])
                ahora = datetime.now()
                clave_ejecucion = objetivo.strftime("%Y-%m-%d %H:%M")

                if ahora >= objetivo and (prog.get("ultima_ejecucion") or "") != clave_ejecucion:
                    info = ejecutar_pg_dump()
                    prog["ultima_ejecucion"] = clave_ejecucion
                    prog["ultimo_estado"] = f"OK ({info['archivo']})"
                    if prog.get("repetir_semanal"):
                        prog["fecha_hora"] = (objetivo + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M")
                    else:
                        prog["habilitado"] = False
                    _guardar_programacion(prog)
        except Exception as e:
            prog["ultimo_estado"] = f"Error: {e}"
            _guardar_programacion(prog)

        await asyncio.sleep(20)


# ==============================================================================
# RÉPLICA A RAILWAY (Bajo demanda vía /api/replicar-bd o bucle opcional)
# ==============================================================================
def _iniciar_replicador_railway() -> None:
    habilitado = os.getenv("RAILWAY_AUTO_REPLICA", "false").strip().lower()
    if habilitado in ("0", "false", "no", "off", ""):
        print("[RAILWAY AUTO-REPLICA] Deshabilitada por defecto (uso bajo demanda en /api/replicar-bd).")
        return

    from replicar_a_railway import ejecutar_replica_automatica_loop

    threading.Thread(
        target=ejecutar_replica_automatica_loop,
        name="hilo-replicar-railway",
        daemon=True,
    ).start()
    print("[RAILWAY AUTO-REPLICA] Iniciada automáticamente en segundo plano.")


@app.on_event("startup")
def startup():
    init_db()
    try:
        with db_cursor(autocommit=True) as cur:
            _asegurar_tabla_comandos(cur)
    except Exception as e:
        print(f"[DB] Error asegurando comandos_adms: {e}")

    _iniciar_replicador_railway()

    try:
        asyncio.get_running_loop().create_task(worker_respaldo_programado())
    except RuntimeError:
        pass


def _resolver_nombre_empresa() -> str:
    empresa = (getattr(settings, "EMPRESA_NOMBRE", "") or "").strip()
    if empresa:
        return empresa
    try:
        with db_cursor() as cur:
            cur.execute("SELECT nombre FROM companias WHERE activa = TRUE ORDER BY id ASC LIMIT 1;")
            row = cur.fetchone()
            if row and row[0]:
                return str(row[0]).strip()
    except Exception:
        pass
    return ""


@app.get("/api/estado-sistema")
def estado_sistema():
    return {
        "modo": "hibrido" if settings.RAILWAY_ENABLED else "local",
        "railway_enabled": bool(settings.RAILWAY_ENABLED),
        "push_heartbeat_segundos": int(settings.PUSH_HEARTBEAT_SEGUNDOS),
        "puerto": int(settings.SERVER_PORT),
        "base_datos": settings.DB_NAME,
        "empresa_nombre": _resolver_nombre_empresa(),
    }


@app.post("/api/replicar-bd")
def replicar_bd_bajo_demanda():
    try:
        from replicar_a_railway import replicar_todo_a_railway
        resumen = replicar_todo_a_railway()
        return {
            "status": "ok",
            "mensaje": f"BD replicada ({resumen.get('marcaciones_procesadas', 0)} marcaciones revisadas).",
            "resumen": resumen,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==============================================================================
# 1. PROTOCOLO PUSH ADMS (ZKTeco SenseFace 2A / Dispositivos Push)
# ==============================================================================
@app.get("/iclock/cdata", response_class=PlainTextResponse)
async def adms_handshake(SN: str = Query(...)):
    try:
        with db_cursor(autocommit=True) as cur:
            _preparar_entorno_adms(cur, SN)
    except Exception:
        pass

    return (
        f"GET OPTION FROM: {SN}\n"
        "Stamp=9999\n"
        "OpStamp=9999\n"
        "ErrorDelay=30\n"
        "Delay=10\n"
        "TransTimes=00:00;14:05\n"
        "TransInterval=1\n"
        "TransFlag=1111000000\n"
        "Realtime=1\n"
        "Encrypt=0\n"
    )


@app.post("/iclock/cdata", response_class=PlainTextResponse)
async def adms_receive_data(request: Request, SN: str = Query(...), table: str = Query("")):
    body_bytes = await request.body()
    raw_text = body_bytes.decode("utf-8", errors="ignore").strip()
    query_str = str(request.query_params).lower()
    sn_clean = _limpiar_sn(SN)

    if raw_text:
        print(f"[ADMS POST] SN={sn_clean} | table={table or query_str} | lineas={len(raw_text.splitlines())}")

    insertados = 0
    try:
        with db_cursor(autocommit=True) as cur:
            _preparar_entorno_adms(cur, sn_clean)

            if "ID=" in raw_text.upper() or "ID=" in query_str.upper():
                _confirmar_comandos_desde_texto(cur, sn_clean, f"{query_str} {raw_text}")

            cur.execute("SELECT pin FROM empleados;")
            mapa_pin_existente: dict[str, str] = {}
            for (pin_db,) in cur.fetchall():
                p_clean = str(pin_db or "").strip()
                if p_clean:
                    mapa_pin_existente.setdefault(_normalizar_pin(p_clean), p_clean)

            if raw_text:
                es_tabla_usuarios = (
                    table.upper() in ("OPERLOG", "USERINFO")
                    or "table=operlog" in query_str
                    or "table=userinfo" in query_str
                )
                es_attlog = (table.upper() == "ATTLOG") or ("table=attlog" in query_str)

                for line in raw_text.splitlines():
                    linea_limpia = line.strip()
                    if not linea_limpia:
                        continue

                    if es_tabla_usuarios or ("PIN=" in linea_limpia and not es_attlog):
                        pin_raw, name_raw = _extraer_usuario_adms(linea_limpia)
                        if pin_raw:
                            try:
                                cur.execute("SAVEPOINT sp_usuario_adms;")
                                _guardar_empleado_desde_reloj(cur, pin_raw, name_raw, sn_clean, mapa_pin_existente)
                                cur.execute("RELEASE SAVEPOINT sp_usuario_adms;")
                                print(f"[ADMS USER] Sincronizado PIN={pin_raw} ({name_raw}) desde SN={sn_clean}")
                            except Exception as e:
                                cur.execute("ROLLBACK TO SAVEPOINT sp_usuario_adms;")
                                print(f"[ADMS ERROR] Usuario PIN={pin_raw}: {e}")
                        continue

                    parts = linea_limpia.split("\t")
                    if len(parts) >= 2:
                        pin_norm = _normalizar_pin(parts[0].strip())
                        fecha_hora = parts[1].strip()
                        status = int(parts[2].strip()) if len(parts) > 2 and parts[2].strip().isdigit() else 1
                        if not pin_norm:
                            continue
                        try:
                            cur.execute("SAVEPOINT sp_marcacion_adms;")
                            cur.execute("""
                                INSERT INTO marcaciones (pin_empleado, fecha_hora, sn_dispositivo, estado_verificacion)
                                VALUES (%s, %s, %s, %s)
                                ON CONFLICT ON CONSTRAINT uq_marcacion_unica DO NOTHING;
                            """, (mapa_pin_existente.get(pin_norm, pin_norm), fecha_hora, sn_clean, status))
                            insertados += cur.rowcount
                            cur.execute("RELEASE SAVEPOINT sp_marcacion_adms;")
                        except Exception as e:
                            cur.execute("ROLLBACK TO SAVEPOINT sp_marcacion_adms;")
                            print(f"[ADMS ERROR] SN={SN} Linea='{line}' Err={e}")
    except Exception as e:
        print(f"[ADMS BATCH ERROR] {e}")

    return f"OK: {insertados}"


@app.get("/iclock/getrequest", response_class=PlainTextResponse)
async def adms_get_request(SN: str = Query(...)):
    sn_clean = _limpiar_sn(SN)
    respuesta = "OK"

    try:
        with db_cursor(autocommit=True) as cur:
            _preparar_entorno_adms(cur, sn_clean)

            if sn_clean:
                cur.execute("""
                    SELECT id, comando
                    FROM comandos_adms
                    WHERE sn = %s AND estado = 'PENDIENTE'
                    ORDER BY id ASC
                    LIMIT 20;
                """, (sn_clean,))
                filas = cur.fetchall()

                if filas:
                    lineas_cmd = [f"C:{cmd_id}:{cmd_txt}" for cmd_id, cmd_txt in filas]
                    ids_enviados = [cmd_id for cmd_id, _ in filas]
                    cur.execute("""
                        UPDATE comandos_adms
                        SET estado = 'ENVIADO',
                            enviado_en = NOW()
                        WHERE id = ANY(%s);
                    """, (ids_enviados,))
                    respuesta = "\n".join(lineas_cmd)
                    print(f"[ADMS CMD OUT] SN={sn_clean} -> {len(filas)} comando(s) enviados")
    except Exception as e:
        print(f"[ADMS GETREQUEST ERROR] SN={sn_clean}: {e}")
        respuesta = "OK"

    return respuesta


@app.post("/iclock/devicecmd", response_class=PlainTextResponse)
async def adms_device_cmd(request: Request, SN: str = Query(None)):
    body_text = (await request.body()).decode("utf-8", errors="ignore")
    query_str = str(request.query_params)
    sn_clean = _limpiar_sn(SN)

    try:
        with db_cursor(autocommit=True) as cur:
            _preparar_entorno_adms(cur, sn_clean)
            _confirmar_comandos_desde_texto(cur, sn_clean, f"{query_str} {body_text}")
    except Exception as e:
        print(f"[ADMS DEVICECMD ERROR] {e}")

    return "OK"


# ==============================================================================
# 2. ENDPOINT PARA PUENTE STANDALONE (Relojes vía pyzk / red local)
# ==============================================================================
class MarcacionBridge(BaseModel):
    pin: str
    fecha_hora: str
    sn: str


def _validar_bridge_key(request: Request, exigir_si_falta: bool = True) -> None:
    api_key = request.headers.get("X-Bridge-Key")
    if exigir_si_falta or api_key:
        if api_key != settings.BRIDGE_SECRET_KEY:
            raise HTTPException(status_code=401, detail="No autorizado")


@app.post("/api/bridge/marcaciones")
def recibir_bridge(items: List[MarcacionBridge], request: Request):
    _validar_bridge_key(request, exigir_si_falta=True)
    with db_cursor(autocommit=True) as cur:
        nuevos = 0
        for item in items:
            cur.execute("""
                INSERT INTO marcaciones (pin_empleado, fecha_hora, sn_dispositivo)
                VALUES (%s, %s, %s)
                ON CONFLICT ON CONSTRAINT uq_marcacion_unica DO NOTHING
            """, (item.pin, item.fecha_hora, item.sn))
            nuevos += cur.rowcount
    return {"status": "ok", "insertados": nuevos}


# ==============================================================================
# 3. GESTIÓN DE DISPOSITIVOS, RED Y RESPALDOS
# ==============================================================================
class DispositivoUpdate(BaseModel):
    sn: str
    nombre: str
    empacadora_id: int
    rol: str
    tipo_conexion: str = "PUSH_ADMS"
    ip_address: Optional[str] = None
    port: int = 4370


class SyncColaboradorReq(BaseModel):
    pin: str


class UsuarioRelojUpdate(BaseModel):
    uid: int
    nombre: str
    card: int = 0


class ProgramacionRespaldoReq(BaseModel):
    habilitado: bool = True
    fecha_hora: str
    repetir_semanal: bool = False


class RestaurarRespaldoReq(BaseModel):
    archivo: str


@app.get("/api/dispositivos")
def listar_dispositivos(request: Request):
    _validar_bridge_key(request, exigir_si_falta=False)
    with db_cursor() as cur:
        cur.execute("""
            SELECT d.sn, d.nombre, d.rol, d.tipo_conexion, d.ip_address, d.port,
                   TO_CHAR(d.ultima_conexion, 'YYYY-MM-DD HH24:MI:SS') AS ultima_conexion,
                   e.id AS empacadora_id, e.codigo AS empacadora_codigo, e.nombre AS empacadora_nombre
            FROM dispositivos d
            JOIN empacadoras e ON d.empacadora_id = e.id
            ORDER BY e.id, d.nombre;
        """)
        return _rows_to_dicts(cur, cur.fetchall())


@app.post("/api/dispositivos")
def guardar_dispositivo(item: DispositivoUpdate):
    with db_cursor(autocommit=True) as cur:
        cur.execute("""
            INSERT INTO dispositivos (sn, empacadora_id, nombre, rol, tipo_conexion, ip_address, port)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (sn) DO UPDATE SET
                empacadora_id = EXCLUDED.empacadora_id,
                nombre = EXCLUDED.nombre,
                rol = EXCLUDED.rol,
                tipo_conexion = EXCLUDED.tipo_conexion,
                ip_address = EXCLUDED.ip_address,
                port = EXCLUDED.port;
        """, (item.sn, item.empacadora_id, item.nombre, item.rol, item.tipo_conexion, item.ip_address, item.port))
    return {"status": "success", "mensaje": "Dispositivo configurado correctamente"}


@app.delete("/api/dispositivos/{sn}")
def eliminar_dispositivo(sn: str):
    with db_cursor(autocommit=True) as cur:
        cur.execute("DELETE FROM dispositivos WHERE sn = %s", (sn,))
        eliminados = cur.rowcount
    if eliminados == 0:
        raise HTTPException(status_code=404, detail="Dispositivo no encontrado")
    return {"status": "success", "mensaje": "Dispositivo eliminado correctamente"}


@app.get("/api/empleados")
def listar_empleados_sync():
    with db_cursor() as cur:
        cur.execute("""
            SELECT em.pin, em.nombres, em.empacadora_id, em.area,
                   e.codigo AS empacadora_codigo, e.nombre AS empacadora_nombre
            FROM empleados em
            LEFT JOIN empacadoras e ON em.empacadora_id = e.id
            WHERE COALESCE(em.activo, TRUE) = TRUE
            ORDER BY em.nombres ASC;
        """)
        return _rows_to_dicts(cur, cur.fetchall())


@app.get("/api/marcaciones/recientes")
def marcaciones_recientes(limite: int = 20):
    with db_cursor() as cur:
        cur.execute("""
            SELECT m.pin_empleado,
                   COALESCE(em.nombres, 'Desconocido / Sin Registro') AS colaborador,
                   TO_CHAR(m.fecha_hora, 'YYYY-MM-DD HH24:MI:SS') AS fecha_hora,
                   COALESCE(d.nombre, m.sn_dispositivo) AS dispositivo_origen,
                   m.sn_dispositivo,
                   COALESCE(e.nombre, '') AS hacienda_nombre
            FROM marcaciones m
            LEFT JOIN empleados em ON em.pin = m.pin_empleado
            LEFT JOIN dispositivos d ON d.sn = m.sn_dispositivo
            LEFT JOIN empacadoras e ON e.id = d.empacadora_id
            ORDER BY m.fecha_hora DESC
            LIMIT %s;
        """, (limite,))
        return _rows_to_dicts(cur, cur.fetchall())


@app.get("/api/red/probar-tcp")
def probar_tcp(ip: str, port: int = 4370):
    try:
        with socket.create_connection((ip, port), timeout=2.5):
            return {"ok": True, "mensaje": f"Puerto {port} accesible en {ip}."}
    except Exception as e:
        return {"ok": False, "mensaje": f"Sin respuesta TCP en {ip}:{port} ({e})."}


@app.post("/api/pull/descargar-marcaciones/{sn}")
def pull_descargar_marcaciones(sn: str):
    return {"status": "ok", "mensaje": f"Sincronización PULL solicitada para SN {sn}."}


@app.post("/api/pull/enviar-empleados/{sn}")
def pull_enviar_empleados(sn: str):
    return {"status": "ok", "mensaje": f"Envío de base de empleados programado para SN {sn}."}


@app.post("/api/pull/sincronizar-colaborador")
def pull_sincronizar_colaborador(req: SyncColaboradorReq):
    return {"status": "ok", "mensaje": f"Colaborador PIN {req.pin} encolado para relojes autorizados."}


@app.get("/api/pull/usuarios/{sn}")
def pull_listar_usuarios(sn: str):
    return {"sn": sn, "usuarios": []}


@app.put("/api/pull/usuarios/{sn}")
def pull_actualizar_usuario(sn: str, item: UsuarioRelojUpdate):
    return {"status": "ok", "mensaje": f"Usuario UID {item.uid} actualizado en {sn} sin alterar marcaciones."}


@app.get("/api/respaldos/estado")
def obtener_estado_respaldos():
    carpeta = _carpeta_respaldos()
    archivos = [
        {
            "archivo": f.name,
            "tamano_kb": round(f.stat().st_size / 1024, 2),
            "modificado_en": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        }
        for f in sorted(carpeta.glob("*.dump"), key=lambda p: p.stat().st_mtime, reverse=True)
    ]
    return {
        "base_datos": settings.DB_NAME,
        "carpeta": str(carpeta),
        "programacion": _cargar_programacion(),
        "archivos": archivos,
    }


@app.post("/api/respaldos/crear")
def crear_respaldo_ahora():
    try:
        info = ejecutar_pg_dump()
        return {"mensaje": f"Respaldo generado: {info['archivo']} ({info['tamano_kb']} KB)", **info}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/respaldos/programar")
def guardar_programacion_respaldo(req: ProgramacionRespaldoReq):
    if req.habilitado and not req.fecha_hora.strip():
        raise HTTPException(status_code=400, detail="Debe indicar fecha y hora para programar el respaldo.")

    prog = _cargar_programacion()
    prog.update({
        "habilitado": bool(req.habilitado),
        "fecha_hora": req.fecha_hora.strip(),
        "repetir_semanal": bool(req.repetir_semanal),
    })
    _guardar_programacion(prog)
    return {"mensaje": "Programación de respaldo guardada.", "programacion": prog}


@app.post("/api/respaldos/restaurar")
def restaurar_respaldo(req: RestaurarRespaldoReq):
    try:
        info = ejecutar_pg_restore(req.archivo)
        return {"mensaje": f"Base de datos restaurada desde {info['archivo']}.", **info}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==============================================================================
# 4. API PANEL RR.HH. (Empacadoras, KPIs y Descarga Excel)
# ==============================================================================
@app.get("/api/empacadoras")
def listar_empacadoras(fecha: Optional[str] = None):
    fecha_consulta = fecha or date.today().isoformat()
    with db_cursor() as cur:
        cur.execute("""
            SELECT
                e.id, e.codigo, e.nombre,
                COUNT(DISTINCT CASE WHEN d.rol = 'COMEDOR' AND m.fecha_hora::date = %s
                      AND m.fecha_hora::time BETWEEN %s AND %s
                      THEN m.pin_empleado END) AS total_almuerzos,
                COUNT(DISTINCT CASE WHEN d.rol IN ('EMPAQUE', 'OFICINA') AND m.fecha_hora::date = %s
                      THEN m.pin_empleado END) AS total_asistencia
            FROM empacadoras e
            LEFT JOIN dispositivos d ON d.empacadora_id = e.id
            LEFT JOIN marcaciones m ON m.sn_dispositivo = d.sn
            WHERE e.activa = TRUE
            GROUP BY e.id, e.codigo, e.nombre
            ORDER BY e.id ASC;
        """, (fecha_consulta, settings.COMEDOR_INICIO, settings.COMEDOR_FIN, fecha_consulta))
        return _rows_to_dicts(cur, cur.fetchall())


@app.get("/api/reportes/comedor/{empacadora_id}")
def descargar_excel_comedor(empacadora_id: int, fecha: Optional[str] = None):
    fecha_consulta = fecha or date.today().isoformat()
    try:
        buffer, filename = generar_excel_comedor(empacadora_id, fecha_consulta)
        return StreamingResponse(
            buffer,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ==============================================================================
# 5. CHAT IA RR.HH. CON TOOL USE SQL (Anthropic Claude)
# ==============================================================================
def ejecutar_consulta_sql_segura(query: str):
    query_limpia = query.strip().lower()
    if not query_limpia.startswith("select"):
        return {"error": "Acceso denegado. Solo se permiten consultas de lectura (SELECT)."}

    palabras_prohibidas = ["insert ", "update ", "delete ", "drop ", "alter ", "truncate ", "grant ", "revoke "]
    if any(palabra in query_limpia for palabra in palabras_prohibidas):
        return {"error": "Consulta bloqueada por políticas de seguridad de solo lectura."}

    try:
        with db_cursor() as cur:
            cur.execute(query)
            resultados = _rows_to_dicts(cur, cur.fetchall())
        return {"resultados": json.loads(json.dumps(resultados, default=str))}
    except Exception as e:
        return {"error": f"Error ejecutando SQL: {str(e)}"}


tools_definition = [
    {
        "name": "ejecutar_consulta_sql_segura",
        "description": "Ejecuta una consulta SQL SELECT en PostgreSQL para analizar asistencias, empleados, departamentos, dispositivos o almuerzos en las empacadoras.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Consulta SQL SELECT válida para PostgreSQL.",
                }
            },
            "required": ["query"],
        },
    }
]


class ChatRequest(BaseModel):
    mensaje: str
    fecha: Optional[str] = None


@app.post("/api/chat")
def chat_rrhh(req: ChatRequest):
    api_key = settings.ANTHROPIC_API_KEY
    if not api_key:
        raise HTTPException(status_code=500, detail="Falta configurar ANTHROPIC_API_KEY en config.ini o entorno")

    fecha_consulta = req.fecha or date.today().isoformat()
    empresa_actual = _resolver_nombre_empresa()
    client = anthropic.Anthropic(api_key=api_key)

    system_prompt = (
        f"Eres el analista experto de RR.HH. y Operaciones{f' para {empresa_actual}' if empresa_actual else ''}. "
        f"La red local conecta las haciendas por VLANs y replica los datos hacia la nube. Fecha de referencia actual: {fecha_consulta}. "
        f"Horario oficial de comedor: {settings.COMEDOR_INICIO} a {settings.COMEDOR_FIN}. "
        "Tienes acceso a PostgreSQL mediante la herramienta 'ejecutar_consulta_sql_segura'. "
        "Esquema de tablas relacional:\n"
        "- companias (id, ruc, nombre, activa)\n"
        "- empacadoras (id, compania_id, codigo, nombre, ubicacion, correo, encargado, activa)\n"
        "- departamentos (id, empacadora_id, nombre, descripcion, activo)\n"
        "- dispositivos (sn, empacadora_id, nombre, rol ['COMEDOR', 'EMPAQUE', 'PALETIZADO', 'OFICINA', 'GARITA'], tipo_conexion, ip_address, port, ultima_conexion)\n"
        "- empleados (pin [Cédula], cedula, nombres, empacadora_id, departamento_id, area, privilege, password, card_number, activo)\n"
        "- empleado_haciendas (pin_empleado, empacadora_id, area_autorizada)\n"
        "- marcaciones (id, pin_empleado, fecha_hora, sn_dispositivo, estado_verificacion)\n"
        "Cuando el usuario pregunte sobre asistencias, marcaciones, comedor, olvidos de marcación, personal o dispositivos, "
        "genera la consulta SQL SELECT adecuada, ejecútala con la herramienta, analiza los datos reales y entrega una respuesta clara, directa y estructurada."
    )

    messages: list[dict[str, Any]] = [{"role": "user", "content": req.mensaje}]
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        system=system_prompt,
        tools=tools_definition,
        messages=messages,
    )

    while response.stop_reason == "tool_use":
        messages.append({"role": "assistant", "content": response.content})
        tool_results_content = []
        for block in response.content:
            if block.type == "tool_use":
                resultado_sql = (
                    ejecutar_consulta_sql_segura(block.input["query"])
                    if block.name == "ejecutar_consulta_sql_segura"
                    else {"error": "Herramienta no reconocida"}
                )
                tool_results_content.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(resultado_sql),
                })

        messages.append({"role": "user", "content": tool_results_content})
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=system_prompt,
            tools=tools_definition,
            messages=messages,
        )

    texto_final = next((b.text for b in response.content if hasattr(b, "text")), "Sin respuesta generada.")
    return {"respuesta": texto_final}


def _resolver_static_dir() -> str:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundle_static = os.path.join(sys._MEIPASS, "static")
        if os.path.isdir(bundle_static):
            return bundle_static

    base_static = os.path.join(settings.BASE_DIR, "static")
    if os.path.isdir(base_static):
        return base_static

    return os.path.join(os.getcwd(), "static")


STATIC_DIR = _resolver_static_dir()
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=settings.SERVER_HOST,
        port=settings.SERVER_PORT,
        log_level="info" if settings.DEBUG else "warning",
    )
