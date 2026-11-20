# -*- coding: utf-8 -*-
import re
from fastapi import APIRouter, Request, Response
from db import get_conn

router = APIRouter(prefix="/iclock", tags=["ZKTeco ADMS Push"])


def _normalizar_pin(pin_raw: str) -> str:
    """Limpia espacios y quita ceros a la izquierda en PINs numéricos."""
    clean = str(pin_raw or "").strip()
    if clean.isdigit():
        stripped = clean.lstrip("0")
        return stripped if stripped else "0"
    return clean


def _limpiar_nombre_zk(nombre_raw: str, pin_fallback: str) -> str:
    """Limpia caracteres extraños de firmwares ZKTeco; si queda vacío, usa placeholder."""
    txt = str(nombre_raw or "").strip()
    txt = re.sub(r"[^\w\s\.\-ÁÉÍÓÚÑáéíóúñ]", "", txt).strip()
    if not txt or txt.isdigit():
        return f"SIN NOMBRE ({pin_fallback})"
    return txt.upper()[:24]  # Muchos firmwares ZK truncan nombres largos en pantalla


def _actualizar_ultima_conexion(cur, sn: str | None) -> None:
    """Actualiza la fecha de última conexión del biométrico si viene informado el SN."""
    if sn and sn.strip():
        cur.execute("""
            UPDATE dispositivos
            SET ultima_conexion = NOW()
            WHERE sn = %s;
        """, (sn.strip(),))


def _asegurar_tabla_comandos(cur) -> None:
    """Crea la tabla de cola de comandos ADMS si aún no existe."""
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


def encolar_comando_usuario_adms(
    pin: str,
    nombres: str,
    permisos_hacienda_area: list[tuple[int, str]],
    activo: bool = True,
) -> int:
    """
    Encola comandos ADMS para todos los relojes PUSH_ADMS activos que coincidan
    con (empacadora_id, area_autorizada) del empleado.
    Devuelve la cantidad de comandos encolados.
    """
    pin_norm = _normalizar_pin(pin)
    if not pin_norm or pin_norm == "0":
        return 0

    nombre_zk = _limpiar_nombre_zk(nombres, pin_norm)
    # Comando estándar ZKTeco ADMS para alta/actualización de usuario
    comando_zk = f"DATA UPDATE USERINFO PIN={pin_norm}\tName={nombre_zk}\tPri=0\tPasswd=\tCard="
    if not activo:
        # Si está inactivo, se envía sin permiso o se elimina según necesidad operativa
        comando_zk = f"DATA DEL USERINFO PIN={pin_norm}"

    encolados = 0
    conn = get_conn()
    cur = conn.cursor()
    try:
        _asegurar_tabla_comandos(cur)
        for emp_id, area in (permisos_hacienda_area or []):
            if emp_id is None or not str(area or "").strip():
                continue
            cur.execute("""
                SELECT sn
                FROM dispositivos
                WHERE tipo_conexion = 'PUSH_ADMS'
                  AND empacadora_id = %s
                  AND UPPER(TRIM(rol)) = UPPER(TRIM(%s))
                  AND sn IS NOT NULL
                  AND TRIM(sn) <> '';
            """, (int(emp_id), str(area).strip()))

            for (sn_dev,) in cur.fetchall():
                cur.execute("""
                    INSERT INTO comandos_adms (sn, comando, estado)
                    VALUES (%s, %s, 'PENDIENTE');
                """, (sn_dev.strip(), comando_zk))
                encolados += 1

        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[ADMS QUEUE ERROR] No se pudo encolar comando para PIN={pin_norm}: {e}")
    finally:
        cur.close()
        conn.close()

    return encolados


def _obtener_info_dispositivo(cur, sn: str | None) -> tuple[int | None, str | None]:
    """Obtiene (empacadora_id, rol/area) asociados al SN del dispositivo."""
    if not sn:
        return None, None
    cur.execute("""
        SELECT empacadora_id, rol
        FROM dispositivos
        WHERE sn = %s
        LIMIT 1;
    """, (sn,))
    row = cur.fetchone()
    if not row:
        return None, None
    emp_id = int(row[0]) if row[0] is not None else None
    area = str(row[1]).strip() if row[1] else None
    return emp_id, area


def _extraer_usuario_adms(linea: str) -> tuple[str | None, str]:
    """Extrae (pin, nombre) desde líneas ADMS de usuario (USER PIN=... Name=...)."""
    m_pin = re.search(r"(?:^|[\s\t])PIN=([^\t\r\n]+)", linea, re.IGNORECASE)
    if not m_pin:
        return None, ""
    pin_raw = m_pin.group(1).strip()

    m_name = re.search(r"(?:^|[\s\t])Name=([^\t\r\n]*)", linea, re.IGNORECASE)
    name_raw = m_name.group(1).strip() if m_name else ""
    return pin_raw, name_raw


def _guardar_empleado_desde_reloj(
    cur,
    pin_raw: str,
    nombre_raw: str,
    sn: str | None,
    mapa_pin_existente: dict[str, str],
) -> None:
    """Inserta o actualiza el empleado en `empleados` y le asigna permiso en `empleado_haciendas`."""
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
                  AND (nombres IS NULL OR TRIM(nombres) = '' OR nombres LIKE 'SIN NOMBRE (%%');
            """, (nombre_limpio, pin_maestro))
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
    """Marca comandos como CONFIRMADO cuando el reloj reporta retorno de ejecución."""
    if not texto:
        return
    # Patrones habituales ADMS: ID=12&Return=0 o ID:12 Return:0
    matches = re.findall(r"\bID[=:]\s*(\d+)", texto, re.IGNORECASE)
    for cmd_id_str in matches:
        cmd_id = int(cmd_id_str)
        if sn:
            cur.execute("""
                UPDATE comandos_adms
                SET estado = 'CONFIRMADO',
                    respuesta_reloj = %s,
                    confirmado_en = NOW()
                WHERE id = %s AND sn = %s;
            """, (texto.strip()[:250], cmd_id, sn))
        else:
            cur.execute("""
                UPDATE comandos_adms
                SET estado = 'CONFIRMADO',
                    respuesta_reloj = %s,
                    confirmado_en = NOW()
                WHERE id = %s;
            """, (texto.strip()[:250], cmd_id))


@router.get("/cdata")
async def adms_handshake(SN: str = None):
    """El reloj ZKTeco hace un GET inicial para verificar comunicación y sincronizar hora."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        _asegurar_tabla_comandos(cur)
        _actualizar_ultima_conexion(cur, SN)
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        cur.close()
        conn.close()

    return Response(content="OK", media_type="text/plain")


@router.get("/getrequest")
async def adms_get_request(SN: str = None):
    """
    El reloj consulta comandos pendientes por ADMS.
    Devuelve comandos PENDIENTE y los marca como ENVIADO, o responde OK si no hay cola.
    """
    sn_clean = SN.strip() if SN and SN.strip() else None
    respuesta = "OK"

    conn = get_conn()
    cur = conn.cursor()
    try:
        _asegurar_tabla_comandos(cur)
        _actualizar_ultima_conexion(cur, sn_clean)

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
                lineas_cmd = []
                ids_enviados = []
                for cmd_id, cmd_txt in filas:
                    lineas_cmd.append(f"C:{cmd_id}:{cmd_txt}")
                    ids_enviados.append(cmd_id)

                cur.execute("""
                    UPDATE comandos_adms
                    SET estado = 'ENVIADO',
                        enviado_en = NOW()
                    WHERE id = ANY(%s);
                """, (ids_enviados,))

                respuesta = "\n".join(lineas_cmd)
                print(f"[ADMS CMD OUT] SN={sn_clean} -> {len(filas)} comando(s) enviados")

        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[ADMS GETREQUEST ERROR] SN={sn_clean}: {e}")
        respuesta = "OK"
    finally:
        cur.close()
        conn.close()

    return Response(content=respuesta, media_type="text/plain")


@router.post("/devicecmd")
async def adms_device_cmd(request: Request, SN: str = None):
    """Recibe el resultado de ejecución de comandos enviados al reloj."""
    body_bytes = await request.body()
    body_text = body_bytes.decode("utf-8", errors="ignore")
    query_str = str(request.query_params)
    sn_clean = SN.strip() if SN and SN.strip() else None

    conn = get_conn()
    cur = conn.cursor()
    try:
        _asegurar_tabla_comandos(cur)
        _actualizar_ultima_conexion(cur, sn_clean)
        _confirmar_comandos_desde_texto(cur, sn_clean, f"{query_str} {body_text}")
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[ADMS DEVICECMD ERROR] {e}")
    finally:
        cur.close()
        conn.close()

    return Response(content="OK", media_type="text/plain")


@router.post("/cdata")
async def adms_receive_data(request: Request, SN: str = None):
    """
    El reloj envía marcaciones (ATTLOG), altas/cambios de usuarios (OPERLOG / USERINFO)
    o confirmaciones de comandos mediante peticiones POST.
    """
    body_bytes = await request.body()
    body_text = body_bytes.decode("utf-8", errors="ignore")
    query_str = str(request.query_params).lower()
    sn_clean = SN.strip() if SN and SN.strip() else None

    if body_text.strip():
        print(f"[ADMS POST] SN={sn_clean} | query={request.query_params} | lineas={len(body_text.strip().splitlines())}")

    conn = get_conn()
    cur = conn.cursor()
    try:
        _asegurar_tabla_comandos(cur)
        _actualizar_ultima_conexion(cur, sn_clean)

        if "ID=" in body_text.upper() or "ID=" in query_str.upper():
            _confirmar_comandos_desde_texto(cur, sn_clean, f"{query_str} {body_text}")

        # Mapa de PIN normalizado -> PIN maestro registrado en `empleados`
        cur.execute("SELECT pin FROM empleados;")
        mapa_pin_existente: dict[str, str] = {}
        for (pin_db,) in cur.fetchall():
            p_clean = str(pin_db or "").strip()
            if p_clean:
                p_norm = _normalizar_pin(p_clean)
                mapa_pin_existente.setdefault(p_norm, p_clean)

        if body_text.strip():
            es_tabla_usuarios = ("table=operlog" in query_str) or ("table=userinfo" in query_str)
            lineas = body_text.strip().splitlines()

            for linea in lineas:
                linea_limpia = linea.strip()
                if not linea_limpia:
                    continue

                # 1. Alta / actualización de usuario desde el reloj (USER PIN=... Name=...)
                if es_tabla_usuarios or "PIN=" in linea_limpia:
                    if "PIN=" in linea_limpia:
                        pin_raw, name_raw = _extraer_usuario_adms(linea_limpia)
                        if pin_raw:
                            try:
                                cur.execute("SAVEPOINT sp_usuario_adms;")
                                _guardar_empleado_desde_reloj(
                                    cur, pin_raw, name_raw, sn_clean, mapa_pin_existente
                                )
                                cur.execute("RELEASE SAVEPOINT sp_usuario_adms;")
                                print(f"[ADMS USER] Sincronizado PIN={pin_raw} ({name_raw}) desde SN={sn_clean}")
                            except Exception as e:
                                cur.execute("ROLLBACK TO SAVEPOINT sp_usuario_adms;")
                                print(f"[ADMS ERROR] No se pudo guardar usuario PIN={pin_raw}: {e}")
                    continue

                # 2. Marcaciones de asistencia (ATTLOG): PIN \t FechaHora \t Estado \t Verificacion
                partes = linea_limpia.split("\t")
                if len(partes) >= 2:
                    pin_raw = partes[0].strip()
                    fecha_str = partes[1].strip()
                    estado_verif = int(partes[2].strip()) if len(partes) > 2 and partes[2].strip().isdigit() else 1

                    pin_norm = _normalizar_pin(pin_raw)
                    if not pin_norm:
                        continue

                    pin_destino = mapa_pin_existente.get(pin_norm, pin_norm)

                    try:
                        cur.execute("SAVEPOINT sp_marcacion_adms;")
                        cur.execute("""
                            INSERT INTO marcaciones (pin_empleado, fecha_hora, sn_dispositivo, estado_verificacion)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT ON CONSTRAINT uq_marcacion_unica DO NOTHING;
                        """, (pin_destino, fecha_str, sn_clean, estado_verif))
                        cur.execute("RELEASE SAVEPOINT sp_marcacion_adms;")
                    except Exception as e:
                        cur.execute("ROLLBACK TO SAVEPOINT sp_marcacion_adms;")
                        print(f"[ADMS ERROR] No se pudo guardar marcación del PIN {pin_raw}: {e}")

        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[ADMS BATCH ERROR] {e}")
    finally:
        cur.close()
        conn.close()

    return Response(content="OK", media_type="text/plain")