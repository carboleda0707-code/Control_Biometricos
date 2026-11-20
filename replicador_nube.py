# -*- coding: utf-8 -*-
import configparser
import os
import sys
import time
import psycopg2
from psycopg2.extras import execute_values
from db import get_conn


def _directorio_base_app() -> str:
    """Resuelve la carpeta del ejecutable (.exe) o del script, compatible con PyInstaller."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _leer_config_ini() -> dict:
    """Lee parámetros opcionales de réplica desde config.ini al lado del .exe / proyecto."""
    candidatos = [
        os.path.join(_directorio_base_app(), "config.ini"),
        os.path.join(os.getcwd(), "config.ini"),
    ]
    cfg = configparser.ConfigParser()
    for ruta in candidatos:
        if os.path.isfile(ruta):
            cfg.read(ruta, encoding="utf-8")
            break

    seccion = cfg["CLOUD_HYBRID"] if "CLOUD_HYBRID" in cfg else {}
    return {
        "habilitado": seccion.get("HYBRID_CLOUD_ENABLED", "").strip(),
        "url": (
            seccion.get("RAILWAY_DATABASE_URL", "").strip()
            or seccion.get("CLOUD_DATABASE_URL", "").strip()
        ),
        "sslmode": seccion.get("CLOUD_SSLMODE", "").strip(),
        "intervalo": seccion.get("CLOUD_SYNC_INTERVAL_SEC", "").strip(),
    }


def _obtener_config_nube() -> tuple[bool, str, str, int]:
    """Obtiene (habilitado, url_railway, sslmode, intervalo_seg) desde entorno o config.ini."""
    ini_cfg = _leer_config_ini()

    raw_hab = (
        os.getenv("HYBRID_CLOUD_ENABLED", "").strip()
        or ini_cfg["habilitado"]
        or "false"
    ).lower()
    habilitado = raw_hab in ("1", "true", "yes", "si", "on")

    url = (
        os.getenv("RAILWAY_DATABASE_URL", "").strip()
        or os.getenv("CLOUD_DATABASE_URL", "").strip()
        or ini_cfg["url"]
    )

    sslmode = (
        os.getenv("CLOUD_SSLMODE", "").strip()
        or ini_cfg["sslmode"]
        or "require"
    )

    raw_int = (
        os.getenv("CLOUD_SYNC_INTERVAL_SEC", "").strip()
        or os.getenv("RAILWAY_SYNC_INTERVAL_SEC", "").strip()
        or ini_cfg["intervalo"]
        or "15"
    )
    try:
        intervalo = max(3, int(raw_int))
    except ValueError:
        intervalo = 15

    return habilitado, url, sslmode, intervalo


def _get_cloud_conn(url: str, sslmode: str):
    if not url:
        raise RuntimeError("No se ha configurado RAILWAY_DATABASE_URL / CLOUD_DATABASE_URL.")
    return psycopg2.connect(url, sslmode=sslmode, connect_timeout=8)


def asegurar_esquema_railway(cur_cloud) -> None:
    """Crea las tablas espejo en Railway si aún no existen."""
    cur_cloud.execute("""
        CREATE TABLE IF NOT EXISTS companias (
            id INTEGER PRIMARY KEY,
            ruc VARCHAR(30),
            nombre VARCHAR(150),
            activa BOOLEAN DEFAULT TRUE
        );
    """)
    cur_cloud.execute("""
        CREATE TABLE IF NOT EXISTS empacadoras (
            id INTEGER PRIMARY KEY,
            compania_id INTEGER,
            codigo VARCHAR(30),
            nombre VARCHAR(150),
            ubicacion TEXT,
            correo VARCHAR(150),
            encargado VARCHAR(150),
            activa BOOLEAN DEFAULT TRUE
        );
    """)
    cur_cloud.execute("""
        CREATE TABLE IF NOT EXISTS departamentos (
            id INTEGER PRIMARY KEY,
            empacadora_id INTEGER,
            nombre VARCHAR(150),
            descripcion TEXT,
            activo BOOLEAN DEFAULT TRUE
        );
    """)
    cur_cloud.execute("""
        CREATE TABLE IF NOT EXISTS dispositivos (
            sn VARCHAR(64) PRIMARY KEY,
            empacadora_id INTEGER,
            nombre VARCHAR(150),
            rol VARCHAR(50),
            tipo_conexion VARCHAR(30),
            ip_address VARCHAR(64),
            port INTEGER,
            ultima_conexion TIMESTAMP
        );
    """)
    cur_cloud.execute("""
        CREATE TABLE IF NOT EXISTS empleados (
            pin VARCHAR(32) PRIMARY KEY,
            cedula VARCHAR(32),
            nombres VARCHAR(150),
            empacadora_id INTEGER,
            departamento_id INTEGER,
            area VARCHAR(50),
            privilege INTEGER DEFAULT 0,
            password VARCHAR(64),
            card_number BIGINT,
            activo BOOLEAN DEFAULT TRUE
        );
    """)
    cur_cloud.execute("""
        CREATE TABLE IF NOT EXISTS empleado_haciendas (
            pin_empleado VARCHAR(32) NOT NULL,
            empacadora_id INTEGER NOT NULL,
            area_autorizada VARCHAR(50) NOT NULL,
            PRIMARY KEY (pin_empleado, empacadora_id, area_autorizada)
        );
    """)
    cur_cloud.execute("""
        CREATE TABLE IF NOT EXISTS marcaciones (
            id BIGINT PRIMARY KEY,
            pin_empleado VARCHAR(32) NOT NULL,
            fecha_hora TIMESTAMP NOT NULL,
            sn_dispositivo VARCHAR(64),
            estado_verificacion INTEGER DEFAULT 1
        );
    """)
    cur_cloud.execute("""
        CREATE TABLE IF NOT EXISTS comandos_adms (
            id INTEGER PRIMARY KEY,
            sn VARCHAR(64) NOT NULL,
            comando TEXT NOT NULL,
            estado VARCHAR(20) NOT NULL DEFAULT 'PENDIENTE',
            respuesta_reloj TEXT,
            creado_en TIMESTAMP,
            enviado_en TIMESTAMP,
            confirmado_en TIMESTAMP
        );
    """)


def sincronizar_hacia_railway(url: str, sslmode: str, lote_marcaciones: int = 2000) -> dict:
    """Replica los datos de la BD local hacia Railway."""
    resumen = {
        "empleados": 0,
        "dispositivos": 0,
        "marcaciones_nuevas": 0,
        "comandos_adms": 0,
    }

    conn_local = get_conn()
    cur_local = conn_local.cursor()
    conn_cloud = _get_cloud_conn(url, sslmode)
    cur_cloud = conn_cloud.cursor()

    try:
        asegurar_esquema_railway(cur_cloud)

        cur_local.execute("SELECT id, ruc, nombre, activa FROM companias;")
        rows = cur_local.fetchall()
        if rows:
            execute_values(cur_cloud, """
                INSERT INTO companias (id, ruc, nombre, activa)
                VALUES %s
                ON CONFLICT (id) DO UPDATE SET
                    ruc = EXCLUDED.ruc,
                    nombre = EXCLUDED.nombre,
                    activa = EXCLUDED.activa;
            """, rows)

        cur_local.execute("""
            SELECT id, compania_id, codigo, nombre, ubicacion, correo, encargado, activa
            FROM empacadoras;
        """)
        rows = cur_local.fetchall()
        if rows:
            execute_values(cur_cloud, """
                INSERT INTO empacadoras (id, compania_id, codigo, nombre, ubicacion, correo, encargado, activa)
                VALUES %s
                ON CONFLICT (id) DO UPDATE SET
                    compania_id = EXCLUDED.compania_id,
                    codigo = EXCLUDED.codigo,
                    nombre = EXCLUDED.nombre,
                    ubicacion = EXCLUDED.ubicacion,
                    correo = EXCLUDED.correo,
                    encargado = EXCLUDED.encargado,
                    activa = EXCLUDED.activa;
            """, rows)

        cur_local.execute("""
            SELECT id, empacadora_id, nombre, descripcion, activo
            FROM departamentos;
        """)
        rows = cur_local.fetchall()
        if rows:
            execute_values(cur_cloud, """
                INSERT INTO departamentos (id, empacadora_id, nombre, descripcion, activo)
                VALUES %s
                ON CONFLICT (id) DO UPDATE SET
                    empacadora_id = EXCLUDED.empacadora_id,
                    nombre = EXCLUDED.nombre,
                    descripcion = EXCLUDED.descripcion,
                    activo = EXCLUDED.activo;
            """, rows)

        cur_local.execute("""
            SELECT sn, empacadora_id, nombre, rol, tipo_conexion, ip_address, port, ultima_conexion
            FROM dispositivos;
        """)
        rows = cur_local.fetchall()
        if rows:
            execute_values(cur_cloud, """
                INSERT INTO dispositivos (sn, empacadora_id, nombre, rol, tipo_conexion, ip_address, port, ultima_conexion)
                VALUES %s
                ON CONFLICT (sn) DO UPDATE SET
                    empacadora_id = EXCLUDED.empacadora_id,
                    nombre = EXCLUDED.nombre,
                    rol = EXCLUDED.rol,
                    tipo_conexion = EXCLUDED.tipo_conexion,
                    ip_address = EXCLUDED.ip_address,
                    port = EXCLUDED.port,
                    ultima_conexion = EXCLUDED.ultima_conexion;
            """, rows)
            resumen["dispositivos"] = len(rows)

        cur_local.execute("""
            SELECT pin, cedula, nombres, empacadora_id, departamento_id, area, privilege, password, card_number, activo
            FROM empleados;
        """)
        rows = cur_local.fetchall()
        if rows:
            execute_values(cur_cloud, """
                INSERT INTO empleados (pin, cedula, nombres, empacadora_id, departamento_id, area, privilege, password, card_number, activo)
                VALUES %s
                ON CONFLICT (pin) DO UPDATE SET
                    cedula = EXCLUDED.cedula,
                    nombres = EXCLUDED.nombres,
                    empacadora_id = EXCLUDED.empacadora_id,
                    departamento_id = EXCLUDED.departamento_id,
                    area = EXCLUDED.area,
                    privilege = EXCLUDED.privilege,
                    password = EXCLUDED.password,
                    card_number = EXCLUDED.card_number,
                    activo = EXCLUDED.activo;
            """, rows)
            resumen["empleados"] = len(rows)

        cur_local.execute("SELECT pin_empleado, empacadora_id, area_autorizada FROM empleado_haciendas;")
        rows = cur_local.fetchall()
        if rows:
            execute_values(cur_cloud, """
                INSERT INTO empleado_haciendas (pin_empleado, empacadora_id, area_autorizada)
                VALUES %s
                ON CONFLICT (pin_empleado, empacadora_id, area_autorizada) DO NOTHING;
            """, rows)

        cur_cloud.execute("SELECT COALESCE(MAX(id), 0) FROM marcaciones;")
        ultimo_id_cloud = int(cur_cloud.fetchone()[0] or 0)

        cur_local.execute("""
            SELECT id, pin_empleado, fecha_hora, sn_dispositivo, estado_verificacion
            FROM marcaciones
            WHERE id > %s
            ORDER BY id ASC
            LIMIT %s;
        """, (ultimo_id_cloud, int(lote_marcaciones)))
        marc_rows = cur_local.fetchall()
        if marc_rows:
            execute_values(cur_cloud, """
                INSERT INTO marcaciones (id, pin_empleado, fecha_hora, sn_dispositivo, estado_verificacion)
                VALUES %s
                ON CONFLICT (id) DO NOTHING;
            """, marc_rows)
            resumen["marcaciones_nuevas"] = len(marc_rows)

        cur_local.execute("""
            SELECT id, sn, comando, estado, respuesta_reloj, creado_en, enviado_en, confirmado_en
            FROM comandos_adms;
        """)
        rows = cur_local.fetchall()
        if rows:
            execute_values(cur_cloud, """
                INSERT INTO comandos_adms (id, sn, comando, estado, respuesta_reloj, creado_en, enviado_en, confirmado_en)
                VALUES %s
                ON CONFLICT (id) DO UPDATE SET
                    sn = EXCLUDED.sn,
                    comando = EXCLUDED.comando,
                    estado = EXCLUDED.estado,
                    respuesta_reloj = EXCLUDED.respuesta_reloj,
                    creado_en = EXCLUDED.creado_en,
                    enviado_en = EXCLUDED.enviado_en,
                    confirmado_en = EXCLUDED.confirmado_en;
            """, rows)
            resumen["comandos_adms"] = len(rows)

        conn_cloud.commit()
        return resumen
    except Exception:
        conn_cloud.rollback()
        raise
    finally:
        cur_local.close()
        conn_local.close()
        cur_cloud.close()
        conn_cloud.close()


def ejecutar_loop() -> None:
    """Bucle continuo seguro para correr en segundo plano junto al ejecutable del cliente."""
    while True:
        habilitado, url, sslmode, intervalo = _obtener_config_nube()
        if not habilitado or not url:
            time.sleep(max(10, intervalo))
            continue

        try:
            rep = sincronizar_hacia_railway(url=url, sslmode=sslmode)
            if rep["marcaciones_nuevas"] > 0:
                print(f"[RAILWAY SYNC] +{rep['marcaciones_nuevas']} marcaciones enviadas a Railway.")
        except Exception as e:
            print(f"[RAILWAY SYNC WARN] Reintento en {intervalo}s: {e}")

        time.sleep(intervalo)


if __name__ == "__main__":
    ejecutar_loop()