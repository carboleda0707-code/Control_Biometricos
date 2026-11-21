# -*- coding: utf-8 -*-
import re
import ssl
import pg8000.dbapi
from config import settings

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def _quote_ident(name: str) -> str:
    """Valida y entrecomilla de forma segura un identificador SQL de PostgreSQL."""
    if not name or not _IDENT_RE.match(name):
        raise ValueError(f"Nombre de base de datos no válido o inseguro: '{name}'")
    return '"' + name.replace('"', '""') + '"'


def _build_ssl_context():
    """Construye el contexto SSL para pg8000 según DB_SSL_MODE / RAILWAY_SSL_MODE."""
    mode = (settings.DB_SSL_MODE or "").strip().lower()
    if mode in ("disable", "off", "false", "0", "allow"):
        return None
    if mode in ("verify-ca", "verify-full"):
        return ssl.create_default_context()
    if mode in ("require", "prefer") or settings.RAILWAY_ENABLED:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None


def _connect_pg8000(database_name: str):
    """Abre conexión con pg8000 aplicando ssl_context solo cuando corresponda."""
    ssl_ctx = _build_ssl_context()
    kwargs = {
        "user": settings.DB_USER,
        "password": settings.DB_PASSWORD,
        "host": settings.DB_HOST,
        "port": settings.DB_PORT,
        "database": database_name,
    }
    if ssl_ctx is not None:
        kwargs["ssl_context"] = ssl_ctx
    return pg8000.dbapi.connect(**kwargs)


def get_conn():
    """Conexión 100% en Python puro con pg8000 utilizando los parámetros dinámicos de config."""
    return _connect_pg8000(settings.DB_NAME)


def crear_base_datos_si_no_existe():
    """Verifica y crea la base de datos usando la base por defecto 'postgres' y credenciales de configuración."""
    conn = None
    cur = None
    try:
        db_ident = _quote_ident(settings.DB_NAME)
        conn = _connect_pg8000("postgres")
        conn.autocommit = True
        cur = conn.cursor()

        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s;", (settings.DB_NAME,))
        existe = cur.fetchone()

        if not existe:
            print(f"[DB] La base de datos '{settings.DB_NAME}' no existe. Creandola...")
            cur.execute(f"CREATE DATABASE {db_ident};")
            print("[DB] Base de datos creada con exito.")
    except Exception as e:
        print(f"[DB AVISO] Nota al verificar base de datos: {e}")
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()


def init_db():
    # 1. Asegurar que la base de datos exista
    crear_base_datos_si_no_existe()

    # 2. Conectar y crear/migrar tablas, índices y vistas usando pg8000
    conn = get_conn()
    conn.autocommit = False
    cur = conn.cursor()

    try:
        # Tabla de Compañías (Razón Social matriz)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS companias (
                id SERIAL PRIMARY KEY,
                ruc VARCHAR(20) UNIQUE NOT NULL,
                nombre VARCHAR(150) NOT NULL,
                direccion VARCHAR(255),
                telefono VARCHAR(50),
                correo_principal VARCHAR(120),
                activa BOOLEAN DEFAULT TRUE
            );
        """)

        # Tabla de Empacadoras / Haciendas (Relación 1 a N con Compañías)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS empacadoras (
                id SERIAL PRIMARY KEY,
                compania_id INT REFERENCES companias(id) ON DELETE RESTRICT,
                codigo VARCHAR(20) UNIQUE NOT NULL,
                nombre VARCHAR(100) NOT NULL,
                ubicacion VARCHAR(200),
                correo VARCHAR(120),
                encargado VARCHAR(120),
                activa BOOLEAN DEFAULT TRUE
            );
        """)

        # Migración segura de columnas nuevas si la tabla empacadoras ya existía previamente
        cur.execute("ALTER TABLE empacadoras ADD COLUMN IF NOT EXISTS compania_id INT REFERENCES companias(id) ON DELETE RESTRICT;")
        cur.execute("ALTER TABLE empacadoras ADD COLUMN IF NOT EXISTS ubicacion VARCHAR(200);")
        cur.execute("ALTER TABLE empacadoras ADD COLUMN IF NOT EXISTS correo VARCHAR(120);")
        cur.execute("ALTER TABLE empacadoras ADD COLUMN IF NOT EXISTS encargado VARCHAR(120);")

        # Tabla de Departamentos / Áreas (Relación 1 a N con Empacadoras)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS departamentos (
                id SERIAL PRIMARY KEY,
                empacadora_id INT REFERENCES empacadoras(id) ON DELETE CASCADE,
                nombre VARCHAR(100) NOT NULL,
                descripcion VARCHAR(255),
                activo BOOLEAN DEFAULT TRUE,
                CONSTRAINT uq_departamento_empacadora UNIQUE (empacadora_id, nombre)
            );
        """)

        # Tabla de Dispositivos (Soporta Push ADMS y Pull Standalone con múltiples áreas físicas)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS dispositivos (
                sn VARCHAR(50) PRIMARY KEY,
                empacadora_id INT REFERENCES empacadoras(id),
                nombre VARCHAR(100) NOT NULL,
                rol VARCHAR(30) NOT NULL CHECK (rol IN ('COMEDOR', 'EMPAQUE', 'PALETIZADO', 'OFICINA', 'GARITA')),
                tipo_conexion VARCHAR(20) DEFAULT 'PUSH_ADMS',
                ip_address VARCHAR(50),
                port INT DEFAULT 4370,
                ultima_conexion TIMESTAMP
            );
        """)

        # Migración segura por si la tabla dispositivos ya existía con el CHECK anterior
        cur.execute("ALTER TABLE dispositivos DROP CONSTRAINT IF EXISTS dispositivos_rol_check;")
        cur.execute("ALTER TABLE dispositivos ADD CONSTRAINT dispositivos_rol_check CHECK (rol IN ('COMEDOR', 'EMPAQUE', 'PALETIZADO', 'OFICINA', 'GARITA'));")

        # Tabla de Empleados (100% compatible con estructura nativa ZKTeco)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS empleados (
                pin VARCHAR(30) PRIMARY KEY,
                cedula VARCHAR(20),
                nombres VARCHAR(150) NOT NULL,
                empacadora_id INT REFERENCES empacadoras(id) ON DELETE RESTRICT,
                departamento_id INT REFERENCES departamentos(id) ON DELETE SET NULL,
                area VARCHAR(100),
                privilege INT DEFAULT 0,
                password VARCHAR(30) DEFAULT '',
                card_number VARCHAR(30) DEFAULT '',
                activo BOOLEAN DEFAULT TRUE
            );
        """)

        # Migración segura en empleados para compatibilidad nativa ZKTeco
        cur.execute("ALTER TABLE empleados ADD COLUMN IF NOT EXISTS departamento_id INT REFERENCES departamentos(id) ON DELETE SET NULL;")
        cur.execute("ALTER TABLE empleados ADD COLUMN IF NOT EXISTS privilege INT DEFAULT 0;")
        cur.execute("ALTER TABLE empleados ADD COLUMN IF NOT EXISTS password VARCHAR(30) DEFAULT '';")
        cur.execute("ALTER TABLE empleados ADD COLUMN IF NOT EXISTS card_number VARCHAR(30) DEFAULT '';")
        cur.execute("ALTER TABLE empleados ALTER COLUMN area DROP DEFAULT;")

        # Tabla intermedia avanzada para permitir múltiples haciendas Y áreas específicas por empleado
        cur.execute("""
            CREATE TABLE IF NOT EXISTS empleado_haciendas (
                pin_empleado VARCHAR(30) NOT NULL REFERENCES empleados(pin) ON DELETE CASCADE,
                empacadora_id INT NOT NULL REFERENCES empacadoras(id) ON DELETE CASCADE,
                area_autorizada VARCHAR(100) DEFAULT 'GENERAL',
                PRIMARY KEY (pin_empleado, empacadora_id, area_autorizada)
            );
        """)

        # Migración segura por si la tabla empleado_haciendas ya existía con llave primaria antigua
        cur.execute("ALTER TABLE empleado_haciendas ADD COLUMN IF NOT EXISTS area_autorizada VARCHAR(100) DEFAULT 'GENERAL';")

        # Tabla de Marcaciones (Con restricción única anti-duplicados)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS marcaciones (
                id BIGSERIAL PRIMARY KEY,
                pin_empleado VARCHAR(30) NOT NULL,
                fecha_hora TIMESTAMP NOT NULL,
                sn_dispositivo VARCHAR(50) REFERENCES dispositivos(sn),
                estado_verificacion INT DEFAULT 1,
                creado_en TIMESTAMP DEFAULT NOW(),
                CONSTRAINT uq_marcacion_unica UNIQUE (pin_empleado, fecha_hora, sn_dispositivo)
            );
        """)

        # Índices para acelerar búsquedas por fecha, empleado, biométrico, rol y hacienda
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_marcaciones_fecha ON marcaciones (fecha_hora);
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_marcaciones_pin_fecha ON marcaciones (pin_empleado, fecha_hora);
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_marcaciones_sn ON marcaciones (sn_dispositivo);
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_marcaciones_sn_fecha ON marcaciones (sn_dispositivo, fecha_hora);
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_dispositivos_rol ON dispositivos (rol);
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_dispositivos_empacadora ON dispositivos (empacadora_id);
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_empleados_empacadora ON empleados (empacadora_id);
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_empleados_departamento ON empleados (departamento_id);
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_empleado_haciendas_empacadora ON empleado_haciendas (empacadora_id);
        """)

        # Vista de marcaciones enriquecidas (asistencia, comedor, dispositivo, hacienda y compañía)
        cur.execute("""
            CREATE OR REPLACE VIEW v_marcaciones_detalle AS
            SELECT
                m.id AS marcacion_id,
                m.fecha_hora,
                m.fecha_hora::date AS fecha,
                m.pin_empleado,
                e.pin AS pin_maestro,
                e.cedula,
                COALESCE(e.nombres, 'Desconocido / Sin Registro') AS colaborador,
                COALESCE(e.activo, FALSE) AS empleado_activo,
                COALESCE(d.nombre, e.area, 'Sin Área') AS area_operativa,
                dept.nombre AS departamento,
                m.sn_dispositivo,
                COALESCE(d.nombre, m.sn_dispositivo) AS dispositivo_nombre,
                d.rol AS rol_dispositivo,
                COALESCE(d.empacadora_id, e.empacadora_id) AS empacadora_id,
                emp.codigo AS codigo_hacienda,
                COALESCE(emp.nombre, 'Sin Hacienda') AS hacienda_nombre,
                comp.nombre AS compania_nombre,
                m.estado_verificacion,
                (e.pin IS NULL) AS sin_registro_maestro
            FROM marcaciones m
            LEFT JOIN empleados e
                ON LTRIM(TRIM(m.pin_empleado), '0') = LTRIM(TRIM(e.pin), '0')
            LEFT JOIN departamentos dept
                ON e.departamento_id = dept.id
            LEFT JOIN dispositivos d
                ON m.sn_dispositivo = d.sn
            LEFT JOIN empacadoras emp
                ON COALESCE(d.empacadora_id, e.empacadora_id) = emp.id
            LEFT JOIN companias comp
                ON emp.compania_id = comp.id;
        """)

        # Vista de personal con haciendas y áreas autorizadas
        cur.execute("""
            CREATE OR REPLACE VIEW v_empleados_autorizaciones AS
            SELECT
                e.pin,
                e.cedula,
                e.nombres,
                COALESCE(e.area, 'Sin área') AS area,
                dept.nombre AS departamento,
                e.activo,
                e.empacadora_id AS empacadora_principal_id,
                emp_base.codigo AS codigo_hacienda_principal,
                emp_base.nombre AS hacienda_principal,
                comp.nombre AS compania_nombre,
                STRING_AGG(
                    DISTINCT CONCAT('[', e_hac.codigo, '] ', e_hac.nombre),
                    ', ' ORDER BY CONCAT('[', e_hac.codigo, '] ', e_hac.nombre)
                ) AS haciendas_autorizadas,
                STRING_AGG(
                    DISTINCT CONCAT(e_hac.codigo, ':', eh.area_autorizada),
                    ', ' ORDER BY CONCAT(e_hac.codigo, ':', eh.area_autorizada)
                ) AS areas_autorizadas
            FROM empleados e
            LEFT JOIN departamentos dept
                ON e.departamento_id = dept.id
            LEFT JOIN empacadoras emp_base
                ON e.empacadora_id = emp_base.id
            LEFT JOIN companias comp
                ON emp_base.compania_id = comp.id
            LEFT JOIN empleado_haciendas eh
                ON e.pin = eh.pin_empleado
            LEFT JOIN empacadoras e_hac
                ON eh.empacadora_id = e_hac.id
            GROUP BY
                e.pin,
                e.cedula,
                e.nombres,
                e.area,
                dept.nombre,
                e.activo,
                e.empacadora_id,
                emp_base.codigo,
                emp_base.nombre,
                comp.nombre;
        """)

        # Otorgar acceso de lectura a las vistas si el rol rrhhconsulta_ia existe
        cur.execute("""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rrhhconsulta_ia') THEN
                    GRANT SELECT ON v_marcaciones_detalle, v_empleados_autorizaciones TO rrhhconsulta_ia;
                END IF;
            END
            $$;
        """)

        conn.commit()
        print("[DB] Estructura, índices y vistas inicializados y migrados correctamente (Multihacienda con áreas específicas).")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()