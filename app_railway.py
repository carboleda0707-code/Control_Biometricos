# -*- coding: utf-8 -*-
import json
import os
import re
import ssl
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, unquote

import pg8000.dbapi
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "index.html"

app = FastAPI(title="Panel Biométrico - Réplica Railway")


class ChatRequest(BaseModel):
    mensaje: str


def _build_ssl_context(ssl_mode: str):
    mode = (ssl_mode or "").strip().lower()
    if mode in ("disable", "off", "false", "0"):
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def get_conn():
    db_url = (os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL") or "").strip()
    if db_url:
        parsed = urlparse(db_url)
        host = parsed.hostname or ""
        port = int(parsed.port or 5432)
        database = (parsed.path or "").lstrip("/") or "railway"
        user = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        ssl_mode = os.getenv("DB_SSL_MODE") or os.getenv("RAILWAY_DB_SSL_MODE") or "require"
    else:
        host = (os.getenv("PGHOST") or os.getenv("RAILWAY_DB_HOST") or "localhost").strip()
        port = int(os.getenv("PGPORT") or os.getenv("RAILWAY_DB_PORT") or "5432")
        database = (os.getenv("PGDATABASE") or os.getenv("RAILWAY_DB_NAME") or "railway").strip()
        user = (os.getenv("PGUSER") or os.getenv("RAILWAY_DB_USER") or "postgres").strip()
        password = os.getenv("PGPASSWORD") or os.getenv("RAILWAY_DB_PASSWORD") or ""
        default_ssl = "disable" if host in ("localhost", "127.0.0.1") else "require"
        ssl_mode = os.getenv("DB_SSL_MODE") or os.getenv("RAILWAY_DB_SSL_MODE") or default_ssl

    kwargs = {
        "host": host,
        "port": port,
        "database": database,
        "user": user,
        "password": password,
    }
    ssl_ctx = _build_ssl_context(ssl_mode)
    if ssl_ctx is not None:
        kwargs["ssl_context"] = ssl_ctx

    return pg8000.dbapi.connect(**kwargs)


def fetch_all(query: str, params: tuple | list | None = None) -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    try:
        if params:
            cur.execute(query, tuple(params))
        else:
            cur.execute(query)
        cols = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
        result = []
        for row in rows:
            item = {}
            for idx, col in enumerate(cols):
                val = row[idx]
                if hasattr(val, "isoformat"):
                    item[col] = val.isoformat(sep=" ", timespec="seconds") if hasattr(val, "hour") else val.isoformat()
                else:
                    item[col] = val
            result.append(item)
        return result
    finally:
        cur.close()
        conn.close()


def construir_sql_inconsistencias(hac_id=None, area_filtro=None, tipo_inc=None):
    filtros_inc_marcas = ["(sin_registro_maestro OR MOD(total_marcas, 2) <> 0)"]
    params_inc_marcas = []

    if hac_id is not None:
        filtros_inc_marcas.append("hacienda_id = %s::int")
        params_inc_marcas.append(int(hac_id))

    if area_filtro and area_filtro != "Todas las áreas":
        filtros_inc_marcas.append("area = %s::text")
        params_inc_marcas.append(str(area_filtro))

    if tipo_inc == "Marcación sin registro (PIN desconocido)":
        filtros_inc_marcas.append("sin_registro_maestro = TRUE")
    elif tipo_inc == "Marcación impar (falta entrada o salida)":
        filtros_inc_marcas.append("(sin_registro_maestro = FALSE AND MOD(total_marcas, 2) <> 0)")

    filtros_ausencias = ["e.activo = TRUE"]
    params_ausencias = []

    if hac_id is not None:
        filtros_ausencias.append("eh.empacadora_id = %s::int")
        params_ausencias.append(int(hac_id))

    if area_filtro and area_filtro != "Todas las áreas":
        filtros_ausencias.append("eh.area_autorizada = %s::text")
        params_ausencias.append(str(area_filtro))

    incluir_marcas = tipo_inc in (
        None,
        "Todas las incidencias",
        "Marcación sin registro (PIN desconocido)",
        "Marcación impar (falta entrada o salida)",
    )
    incluir_ausencias = tipo_inc in (
        None,
        "Todas las incidencias",
        "Activo sin marcación",
    )

    uniones = []
    params_finales = []

    if incluir_marcas:
        uniones.append(f"""
            SELECT
                fecha,
                pin,
                colaborador,
                hacienda,
                area,
                dispositivo_sn,
                total_marcas,
                primera_marca::text AS primera_marca,
                ultima_marca::text AS ultima_marca,
                CASE
                    WHEN sin_registro_maestro THEN 'Marcación sin registro (PIN desconocido)'
                    WHEN MOD(total_marcas, 2) <> 0 THEN 'Marcación impar (falta entrada o salida)'
                    ELSE NULL
                END AS incidencia
            FROM marcas_por_dia
            WHERE {" AND ".join(filtros_inc_marcas)}
        """)
        params_finales.extend(params_inc_marcas)

    if incluir_ausencias:
        uniones.append(f"""
            SELECT
                f.fecha,
                e.pin,
                e.nombres AS colaborador,
                emp.nombre AS hacienda,
                eh.area_autorizada AS area,
                'Sin marcación en el día'::text AS dispositivo_sn,
                0::bigint AS total_marcas,
                NULL::text AS primera_marca,
                NULL::text AS ultima_marca,
                'Activo sin marcación'::text AS incidencia
            FROM fechas f
            CROSS JOIN empleados e
            JOIN empleado_haciendas eh
                ON e.pin = eh.pin_empleado
            JOIN empacadoras emp
                ON eh.empacadora_id = emp.id
            WHERE {" AND ".join(filtros_ausencias)}
              AND NOT EXISTS (
                  SELECT 1
                  FROM marcaciones m
                  WHERE m.fecha_hora::date = f.fecha
                    AND LTRIM(TRIM(m.pin_empleado), '0') = LTRIM(TRIM(e.pin), '0')
              )
        """)
        params_finales.extend(params_ausencias)

    if not uniones:
        return None, []

    sql = f"""
        WITH fechas AS (
            SELECT generate_series(%s::date, %s::date, interval '1 day')::date AS fecha
        ),
        marcas_por_dia AS (
            SELECT
                m.fecha_hora::date AS fecha,
                COALESCE(e.pin, m.pin_empleado) AS pin,
                COALESCE(e.nombres, 'Desconocido / Sin Registro') AS colaborador,
                COALESCE(emp.nombre, 'Sin Hacienda') AS hacienda,
                COALESCE(emp.id, e.empacadora_id) AS hacienda_id,
                COALESCE(d.rol, e.area, 'Sin Área') AS area,
                COALESCE(CONCAT(d.nombre, ' (', m.sn_dispositivo, ')'), m.sn_dispositivo) AS dispositivo_sn,
                COUNT(*) AS total_marcas,
                MIN(m.fecha_hora) AS primera_marca,
                MAX(m.fecha_hora) AS ultima_marca,
                (e.pin IS NULL) AS sin_registro_maestro
            FROM marcaciones m
            LEFT JOIN empleados e
                ON LTRIM(TRIM(m.pin_empleado), '0') = LTRIM(TRIM(e.pin), '0')
            LEFT JOIN dispositivos d
                ON m.sn_dispositivo = d.sn
            LEFT JOIN empacadoras emp
                ON COALESCE(d.empacadora_id, e.empacadora_id) = emp.id
            WHERE m.fecha_hora::date BETWEEN %s::date AND %s::date
            GROUP BY
                m.fecha_hora::date,
                COALESCE(e.pin, m.pin_empleado),
                COALESCE(e.nombres, 'Desconocido / Sin Registro'),
                COALESCE(emp.nombre, 'Sin Hacienda'),
                COALESCE(emp.id, e.empacadora_id),
                COALESCE(d.rol, e.area, 'Sin Área'),
                COALESCE(CONCAT(d.nombre, ' (', m.sn_dispositivo, ')'), m.sn_dispositivo),
                (e.pin IS NULL)
        )
        SELECT *
        FROM (
            {" UNION ALL ".join(uniones)}
        ) reporte_incidencias
        ORDER BY fecha DESC, incidencia ASC, colaborador ASC;
    """

    todos_params = [None, None, None, None] + params_finales
    return sql, todos_params


@app.get("/")
def serve_index():
    if not INDEX_FILE.exists():
        raise HTTPException(status_code=404, detail="index.html no encontrado")
    return FileResponse(str(INDEX_FILE), media_type="text/html; charset=utf-8")


@app.get("/api/estado-sistema")
def estado_sistema():
    heartbeat = int(os.getenv("PUSH_HEARTBEAT_SEGUNDOS", "300"))
    empresa_nombre = (os.getenv("EMPRESA_NOMBRE") or "").strip()
    if not empresa_nombre:
        try:
            rows = fetch_all("SELECT nombre FROM companias WHERE activa = TRUE ORDER BY id ASC LIMIT 1;")
            if rows:
                empresa_nombre = str(rows[0].get("nombre") or "").strip()
        except Exception:
            pass

    return {
        "empresa_nombre": empresa_nombre,
        "push_heartbeat_segundos": heartbeat,
        "railway_enabled": True,
    }


@app.get("/api/companias")
def listar_companias():
    try:
        return fetch_all("SELECT id, nombre FROM companias WHERE activa = TRUE ORDER BY nombre ASC;")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/empacadoras")
def listar_empacadoras():
    try:
        return fetch_all("""
            SELECT e.id, e.compania_id, e.codigo, e.nombre, c.nombre AS compania
            FROM empacadoras e
            JOIN companias c ON e.compania_id = c.id
            WHERE e.activa = TRUE
            ORDER BY c.nombre ASC, e.codigo ASC;
        """)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/dispositivos")
def listar_dispositivos():
    try:
        return fetch_all("""
            SELECT
                d.sn,
                d.empacadora_id,
                d.nombre,
                d.rol,
                d.tipo_conexion,
                d.ip_address,
                d.port,
                d.ultima_conexion,
                COALESCE(emp.codigo, '') AS empacadora_codigo,
                COALESCE(emp.nombre, 'Sin Hacienda') AS empacadora_nombre
            FROM dispositivos d
            LEFT JOIN empacadoras emp ON d.empacadora_id = emp.id
            ORDER BY empacadora_nombre ASC, d.rol ASC, d.nombre ASC;
        """)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/areas")
def listar_areas():
    try:
        rows = fetch_all("""
            SELECT DISTINCT UPPER(TRIM(area)) AS area
            FROM empleados
            WHERE area IS NOT NULL AND TRIM(area) <> ''
            UNION
            SELECT DISTINCT UPPER(TRIM(nombre)) AS area
            FROM departamentos
            WHERE activo = TRUE AND nombre IS NOT NULL AND TRIM(nombre) <> ''
            UNION
            SELECT DISTINCT UPPER(TRIM(rol)) AS area
            FROM dispositivos
            WHERE rol IS NOT NULL AND TRIM(rol) <> ''
            UNION
            SELECT DISTINCT UPPER(TRIM(area_autorizada)) AS area
            FROM empleado_haciendas
            WHERE area_autorizada IS NOT NULL AND TRIM(area_autorizada) <> '' AND UPPER(TRIM(area_autorizada)) <> 'GENERAL'
            ORDER BY area ASC;
        """)
        return [r["area"] for r in rows if r.get("area")]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/reportes/empleados")
def reporte_empleados(
    compania_id: Optional[int] = Query(default=None),
    empacadora_id: Optional[int] = Query(default=None),
    ubicacion: Optional[str] = Query(default=None),
    area: Optional[str] = Query(default=None),
    cedula: Optional[str] = Query(default=None),
):
    sql = """
        SELECT DISTINCT
            emp.pin AS cedula,
            emp.nombres,
            COALESCE(emp.area, dept.nombre, 'Sin área') AS area,
            emp.activo,
            STRING_AGG(DISTINCT CONCAT('[', e_hac.codigo, '] ', e_hac.nombre), ', ') AS haciendas_autorizadas
        FROM empleados emp
        LEFT JOIN departamentos dept ON emp.departamento_id = dept.id
        LEFT JOIN empleado_haciendas eh ON emp.pin = eh.pin_empleado
        LEFT JOIN empacadoras e_hac ON eh.empacadora_id = e_hac.id
        LEFT JOIN companias c_hac ON e_hac.compania_id = c_hac.id
    """
    conds = []
    params = []

    if compania_id is not None:
        conds.append("c_hac.id = %s::int")
        params.append(int(compania_id))

    if empacadora_id is not None:
        conds.append("emp.pin IN (SELECT pin_empleado FROM empleado_haciendas WHERE empacadora_id = %s::int)")
        params.append(int(empacadora_id))

    if ubicacion:
        try:
            ubic_obj = json.loads(ubicacion)
            modo_ubic = ubic_obj.get("modo")
            ubic_hac_id = ubic_obj.get("empacadora_id")
            ubic_rol = ubic_obj.get("rol")
            ubic_sn = ubic_obj.get("sn")

            if modo_ubic == "DISPOSITIVO" and ubic_sn:
                sub_conds = ["m_u.sn_dispositivo = %s::text"]
                sub_params = [str(ubic_sn)]
                if ubic_hac_id not in (None, "", "null"):
                    sub_conds.append("d_u.empacadora_id = %s::int")
                    sub_params.append(int(ubic_hac_id))
                conds.append(f"""
                    (
                        EXISTS (
                            SELECT 1
                            FROM marcaciones m_u
                            JOIN dispositivos d_u ON m_u.sn_dispositivo = d_u.sn
                            WHERE LTRIM(TRIM(m_u.pin_empleado), '0') = LTRIM(TRIM(emp.pin), '0')
                              AND {" AND ".join(sub_conds)}
                        )
                        OR (
                            UPPER(COALESCE(emp.area, '')) = UPPER(%s::text)
                            OR UPPER(COALESCE(dept.nombre, '')) = UPPER(%s::text)
                        )
                    )
                """)
                params.extend(sub_params)
                params.extend([str(ubic_rol or ""), str(ubic_rol or "")])
            elif ubic_rol:
                rol_str = str(ubic_rol).upper()
                sub_conds = ["UPPER(COALESCE(d_r.rol, '')) = %s::text"]
                sub_params = [rol_str]
                if ubic_hac_id not in (None, "", "null"):
                    sub_conds.append("d_r.empacadora_id = %s::int")
                    sub_params.append(int(ubic_hac_id))
                conds.append(f"""
                    (
                        UPPER(COALESCE(emp.area, '')) = %s::text
                        OR UPPER(COALESCE(dept.nombre, '')) = %s::text
                        OR EXISTS (
                            SELECT 1
                            FROM empleado_haciendas eh_r
                            WHERE eh_r.pin_empleado = emp.pin
                              AND UPPER(COALESCE(eh_r.area_autorizada, '')) = %s::text
                        )
                        OR EXISTS (
                            SELECT 1
                            FROM marcaciones m_r
                            JOIN dispositivos d_r ON m_r.sn_dispositivo = d_r.sn
                            WHERE LTRIM(TRIM(m_r.pin_empleado), '0') = LTRIM(TRIM(emp.pin), '0')
                              AND {" AND ".join(sub_conds)}
                        )
                    )
                """)
                params.extend([rol_str, rol_str, rol_str])
                params.extend(sub_params)
        except json.JSONDecodeError:
            pass

    if area and area != "Todas las áreas":
        area_str = str(area).upper()
        conds.append("""
            (
                UPPER(COALESCE(emp.area, '')) = %s::text
                OR UPPER(COALESCE(dept.nombre, '')) = %s::text
                OR EXISTS (
                    SELECT 1
                    FROM empleado_haciendas eh_a
                    WHERE eh_a.pin_empleado = emp.pin
                      AND UPPER(COALESCE(eh_a.area_autorizada, '')) = %s::text
                )
            )
        """)
        params.extend([area_str, area_str, area_str])

    if cedula and cedula.strip():
        conds.append("(emp.pin ILIKE %s::text OR COALESCE(emp.cedula, '') ILIKE %s::text)")
        termino = f"%{cedula.strip()}%"
        params.extend([termino, termino])

    if conds:
        sql += " WHERE " + " AND ".join(conds)

    sql += " GROUP BY emp.pin, emp.nombres, emp.area, dept.nombre, emp.activo ORDER BY emp.nombres ASC;"
    try:
        return fetch_all(sql, params)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/reportes/marcaciones")
def reporte_marcaciones(
    fecha_desde: date = Query(default_factory=date.today),
    fecha_hasta: date = Query(default_factory=date.today),
    empacadora_id: Optional[int] = Query(default=None),
    rol: Optional[str] = Query(default=None),
    cedula: Optional[str] = Query(default=None),
    area: Optional[str] = Query(default=None),
):
    sql = """
        SELECT m.fecha_hora, m.pin_empleado AS cedula,
               COALESCE(e.nombres, 'Desconocido / Sin Registro') AS empleado,
               COALESCE(e.area, 'N/D') AS area_empleado,
               d.nombre AS dispositivo, d.rol AS rol_dispositivo,
               emp.nombre AS hacienda_ubicacion, m.estado_verificacion
        FROM marcaciones m
        LEFT JOIN empleados e ON LTRIM(TRIM(m.pin_empleado), '0') = LTRIM(TRIM(e.pin), '0')
        LEFT JOIN dispositivos d ON m.sn_dispositivo = d.sn
        LEFT JOIN empacadoras emp ON d.empacadora_id = emp.id
    """
    conds = [
        "m.fecha_hora::date >= %s::date",
        "m.fecha_hora::date <= %s::date",
    ]
    params = [fecha_desde, fecha_hasta]

    if empacadora_id is not None:
        conds.append("d.empacadora_id = %s::int")
        params.append(int(empacadora_id))

    if rol and rol != "Todos los roles":
        conds.append("d.rol = %s::text")
        params.append(str(rol))

    if cedula and cedula.strip():
        conds.append("(m.pin_empleado ILIKE %s::text OR COALESCE(e.cedula, '') ILIKE %s::text)")
        termino = f"%{cedula.strip()}%"
        params.extend([termino, termino])

    if area and area != "Todas las áreas":
        conds.append("UPPER(COALESCE(e.area, '')) = UPPER(%s::text)")
        params.append(str(area))

    sql += " WHERE " + " AND ".join(conds) + " ORDER BY m.fecha_hora DESC LIMIT 5000;"
    try:
        return fetch_all(sql, params)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/reportes/inconsistencias")
def reporte_inconsistencias(
    fecha_desde: date = Query(default_factory=date.today),
    fecha_hasta: date = Query(default_factory=date.today),
    tipo_incidencia: Optional[str] = Query(default="Todas las incidencias"),
    empacadora_id: Optional[int] = Query(default=None),
    area: Optional[str] = Query(default=None),
):
    if fecha_desde > fecha_hasta:
        raise HTTPException(status_code=400, detail="La fecha 'Desde' no puede ser mayor que 'Hasta'.")

    sql_inc, params_inc = construir_sql_inconsistencias(
        hac_id=empacadora_id,
        area_filtro=area,
        tipo_inc=tipo_incidencia,
    )
    if not sql_inc:
        return []

    params_inc[0] = fecha_desde
    params_inc[1] = fecha_hasta
    params_inc[2] = fecha_desde
    params_inc[3] = fecha_hasta

    try:
        return fetch_all(sql_inc, params_inc)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/chat")
def chat_consulta(req: ChatRequest):
    mensaje = (req.mensaje or "").strip()
    if not mensaje:
        raise HTTPException(status_code=400, detail="Escribe una consulta antes de enviar.")

    api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        try:
            resumen_hac = fetch_all("""
                SELECT COALESCE(emp.nombre, 'Sin Hacienda') AS hacienda, COUNT(*) AS total_marcaciones
                FROM marcaciones m
                LEFT JOIN dispositivos d ON m.sn_dispositivo = d.sn
                LEFT JOIN empacadoras emp ON d.empacadora_id = emp.id
                WHERE m.fecha_hora::date = CURRENT_DATE
                GROUP BY COALESCE(emp.nombre, 'Sin Hacienda')
                ORDER BY total_marcaciones DESC;
            """)
            if resumen_hac:
                lineas = [f"- {r['hacienda']}: {r['total_marcaciones']} marcaciones hoy" for r in resumen_hac]
                return {"respuesta": "Resumen de hoy (sin ANTHROPIC_API_KEY configurada en Railway):\n" + "\n".join(lineas)}
            return {"respuesta": "No hay marcaciones registradas hoy en la base replicada. Configura ANTHROPIC_API_KEY en Railway para respuestas con IA."}
        except Exception as e:
            return JSONResponse(status_code=500, content={"detail": str(e)})

    try:
        import urllib.request

        contexto_resumen = fetch_all("""
            SELECT
                (SELECT COUNT(*) FROM empleados WHERE activo = TRUE) AS empleados_activos,
                (SELECT COUNT(*) FROM dispositivos) AS total_dispositivos,
                (SELECT COUNT(*) FROM marcaciones WHERE fecha_hora::date = CURRENT_DATE) AS marcaciones_hoy;
        """)
        contexto_txt = json.dumps(contexto_resumen[0] if contexto_resumen else {}, ensure_ascii=False)

        payload = {
            "model": os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
            "max_tokens": 800,
            "system": (
                "Eres un asistente de RR. HH. y control biométrico para haciendas bananeras. "
                "Responde de forma clara y concisa en español. Contexto actual de la BD replicada: "
                + contexto_txt
            ),
            "messages": [{"role": "user", "content": mensaje}],
        }
        http_req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(http_req, timeout=25) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            partes = [b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"]
            return {"respuesta": "\n".join(partes).strip() or "(Sin respuesta)"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error consultando Claude: {re.sub(r'key=[^&\\s]+', 'key=***', str(e))}")