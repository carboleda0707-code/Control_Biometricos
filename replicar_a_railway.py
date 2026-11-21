# -*- coding: utf-8 -*-
import os
import ssl
from pathlib import Path
from dotenv import load_dotenv
import pg8000.dbapi

# Carga el .env desde la carpeta del script antes de importar db/config
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

from db import get_conn as get_local_conn  # noqa: E402


def _get_railway_conn():
    host = os.getenv("RAILWAY_DB_HOST", "").strip()
    port_raw = os.getenv("RAILWAY_DB_PORT", "").strip()
    database = os.getenv("RAILWAY_DB_NAME", "").strip()
    user = os.getenv("RAILWAY_DB_USER", "").strip()
    password = os.getenv("RAILWAY_DB_PASSWORD", "").strip()
    ssl_mode = os.getenv("RAILWAY_DB_SSL_MODE", "require").strip().lower()

    if not all([host, port_raw, database, user, password]):
        raise RuntimeError(
            "Faltan variables RAILWAY_DB_HOST, RAILWAY_DB_PORT, RAILWAY_DB_NAME, RAILWAY_DB_USER o RAILWAY_DB_PASSWORD en el entorno / .env."
        )

    port = int(port_raw)

    kwargs = {
        "host": host,
        "port": port,
        "database": database,
        "user": user,
        "password": password,
    }
    if ssl_mode not in ("disable", "off", "false", "0"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx

    return pg8000.dbapi.connect(**kwargs)


def _asegurar_tablas_destino(dst_cur) -> None:
    dst_cur.execute("""
        CREATE TABLE IF NOT EXISTS companias (
            id INTEGER PRIMARY KEY,
            ruc VARCHAR(30),
            nombre VARCHAR(150),
            direccion TEXT,
            telefono VARCHAR(50),
            correo_principal VARCHAR(150),
            activa BOOLEAN DEFAULT TRUE
        );
    """)
    dst_cur.execute("""
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
    dst_cur.execute("""
        CREATE TABLE IF NOT EXISTS departamentos (
            id INTEGER PRIMARY KEY,
            empacadora_id INTEGER,
            nombre VARCHAR(150),
            descripcion TEXT,
            activo BOOLEAN DEFAULT TRUE
        );
    """)
    dst_cur.execute("""
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
    dst_cur.execute("""
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
    dst_cur.execute("""
        CREATE TABLE IF NOT EXISTS empleado_haciendas (
            pin_empleado VARCHAR(32) NOT NULL,
            empacadora_id INTEGER NOT NULL,
            area_autorizada VARCHAR(50) NOT NULL,
            CONSTRAINT uq_empleado_haciendas UNIQUE (pin_empleado, empacadora_id, area_autorizada)
        );
    """)
    dst_cur.execute("""
        CREATE TABLE IF NOT EXISTS marcaciones (
            id SERIAL PRIMARY KEY,
            pin_empleado VARCHAR(32) NOT NULL,
            fecha_hora TIMESTAMP NOT NULL,
            sn_dispositivo VARCHAR(64),
            estado_verificacion INTEGER DEFAULT 1,
            creado_en TIMESTAMP DEFAULT NOW(),
            CONSTRAINT uq_marcacion_unica UNIQUE (pin_empleado, fecha_hora, sn_dispositivo)
        );
    """)


def replicar_todo_a_railway(limite_marcaciones: int = 5000) -> dict:
    src_conn = get_local_conn()
    dst_conn = _get_railway_conn()
    src_cur = src_conn.cursor()
    dst_cur = dst_conn.cursor()

    resumen = {
        "companias": 0,
        "empacadoras": 0,
        "departamentos": 0,
        "dispositivos": 0,
        "empleados": 0,
        "empleado_haciendas": 0,
        "marcaciones_procesadas": 0,
    }

    try:
        _asegurar_tablas_destino(dst_cur)

        # 1. Compañías
        src_cur.execute(
            "SELECT id, ruc, nombre, direccion, telefono, correo_principal, activa FROM companias;"
        )
        for row in src_cur.fetchall():
            dst_cur.execute(
                """
                INSERT INTO companias (id, ruc, nombre, direccion, telefono, correo_principal, activa)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    ruc = EXCLUDED.ruc,
                    nombre = EXCLUDED.nombre,
                    direccion = EXCLUDED.direccion,
                    telefono = EXCLUDED.telefono,
                    correo_principal = EXCLUDED.correo_principal,
                    activa = EXCLUDED.activa;
                """,
                row,
            )
            resumen["companias"] += 1

        # 2. Empacadoras / Haciendas
        src_cur.execute(
            "SELECT id, compania_id, codigo, nombre, ubicacion, correo, encargado, activa FROM empacadoras;"
        )
        for row in src_cur.fetchall():
            dst_cur.execute(
                """
                INSERT INTO empacadoras (id, compania_id, codigo, nombre, ubicacion, correo, encargado, activa)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    compania_id = EXCLUDED.compania_id,
                    codigo = EXCLUDED.codigo,
                    nombre = EXCLUDED.nombre,
                    ubicacion = EXCLUDED.ubicacion,
                    correo = EXCLUDED.correo,
                    encargado = EXCLUDED.encargado,
                    activa = EXCLUDED.activa;
                """,
                row,
            )
            resumen["empacadoras"] += 1

        # 3. Departamentos
        src_cur.execute(
            "SELECT id, empacadora_id, nombre, descripcion, activo FROM departamentos;"
        )
        for row in src_cur.fetchall():
            dst_cur.execute(
                """
                INSERT INTO departamentos (id, empacadora_id, nombre, descripcion, activo)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    empacadora_id = EXCLUDED.empacadora_id,
                    nombre = EXCLUDED.nombre,
                    descripcion = EXCLUDED.descripcion,
                    activo = EXCLUDED.activo;
                """,
                row,
            )
            resumen["departamentos"] += 1

        # 4. Dispositivos (incluye ultima_conexion para 'Biométricos Activos')
        src_cur.execute(
            "SELECT sn, empacadora_id, nombre, rol, tipo_conexion, ip_address, port, ultima_conexion FROM dispositivos;"
        )
        for row in src_cur.fetchall():
            dst_cur.execute(
                """
                INSERT INTO dispositivos (sn, empacadora_id, nombre, rol, tipo_conexion, ip_address, port, ultima_conexion)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (sn) DO UPDATE SET
                    empacadora_id = EXCLUDED.empacadora_id,
                    nombre = EXCLUDED.nombre,
                    rol = EXCLUDED.rol,
                    tipo_conexion = EXCLUDED.tipo_conexion,
                    ip_address = EXCLUDED.ip_address,
                    port = EXCLUDED.port,
                    ultima_conexion = GREATEST(
                        COALESCE(dispositivos.ultima_conexion, EXCLUDED.ultima_conexion),
                        COALESCE(EXCLUDED.ultima_conexion, dispositivos.ultima_conexion)
                    );
                """,
                row,
            )
            resumen["dispositivos"] += 1

        # 5. Empleados
        src_cur.execute(
            "SELECT pin, cedula, nombres, empacadora_id, departamento_id, area, privilege, password, card_number, activo FROM empleados;"
        )
        for row in src_cur.fetchall():
            dst_cur.execute(
                """
                INSERT INTO empleados (pin, cedula, nombres, empacadora_id, departamento_id, area, privilege, password, card_number, activo)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                """,
                row,
            )
            resumen["empleados"] += 1

        # 6. Permisos por hacienda/área
        src_cur.execute(
            "SELECT pin_empleado, empacadora_id, area_autorizada FROM empleado_haciendas;"
        )
        for row in src_cur.fetchall():
            dst_cur.execute(
                """
                INSERT INTO empleado_haciendas (pin_empleado, empacadora_id, area_autorizada)
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING;
                """,
                row,
            )
            resumen["empleado_haciendas"] += 1

        # 7. Marcaciones recientes
        src_cur.execute(
            """
            SELECT pin_empleado, fecha_hora, sn_dispositivo, estado_verificacion, creado_en
            FROM marcaciones
            ORDER BY fecha_hora DESC
            LIMIT %s;
            """,
            (int(limite_marcaciones),),
        )
        for row in src_cur.fetchall():
            dst_cur.execute(
                """
                INSERT INTO marcaciones (pin_empleado, fecha_hora, sn_dispositivo, estado_verificacion, creado_en)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT ON CONSTRAINT uq_marcacion_unica DO NOTHING;
                """,
                row,
            )
            resumen["marcaciones_procesadas"] += 1

        dst_conn.commit()
        return resumen
    except Exception:
        dst_conn.rollback()
        raise
    finally:
        src_cur.close()
        dst_cur.close()
        src_conn.close()
        dst_conn.close()


if __name__ == "__main__":
    print("Replicando base local hacia Railway...")
    print(replicar_todo_a_railway())