# -*- coding: utf-8 -*-
import re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from db import get_conn


def normalizar_pin(pin_raw: str) -> str:
    """Limpia espacios y quita ceros a la izquierda en PINs numéricos."""
    clean = str(pin_raw or "").strip()
    if clean.isdigit():
        stripped = clean.lstrip("0")
        return stripped if stripped else "0"
    return clean


def es_cedula_valida_o_pin_real(pin_norm: str) -> bool:
    """
    Filtra IDs correlativos cortos de prueba (ej. 1..999) frente a cédulas reales
    (9 o 10 dígitos numéricos tras quitar o mantener el cero inicial).
    """
    return pin_norm.isdigit() and 9 <= len(pin_norm) <= 10


def limpiar_nombre_zk(nombre_raw: str, pin_fallback: str) -> str:
    """Limpia caracteres corruptos de firmwares ZKTeco; si queda vacío, usa un placeholder."""
    txt = str(nombre_raw or "").strip()
    txt = re.sub(r"[^\w\s\.\-ÁÉÍÓÚÑáéíóúñ]", "", txt).strip()
    if not txt or txt.isdigit():
        return f"SIN NOMBRE ({pin_fallback})"
    return txt.upper()[:60]


def _leer_dispositivo_zk(dev: dict, timeout: int = 6) -> dict:
    """Conecta a un reloj ZKTeco y extrae sus usuarios y marcaciones."""
    from zk import ZK

    sn = dev["sn"]
    ip = str(dev["ip_address"]).strip()
    port = int(dev.get("port") or 4370)

    resultado = {
        "sn": sn,
        "nombre": dev["nombre"],
        "ip": ip,
        "port": port,
        "empacadora_id": int(dev["empacadora_id"]),
        "area_fisica": str(dev["rol"]).strip(),
        "ok": False,
        "error": None,
        "usuarios": [],
        "marcaciones": [],
    }

    zk = ZK(ip, port=port, timeout=timeout)
    conn_zk = None
    try:
        conn_zk = zk.connect()
        raw_users = conn_zk.get_users() or []
        raw_att = conn_zk.get_attendance() or []

        for u in raw_users:
            resultado["usuarios"].append({
                "uid": int(getattr(u, "uid", 0) or 0),
                "user_id": str(getattr(u, "user_id", "") or "").strip(),
                "name": str(getattr(u, "name", "") or "").strip(),
            })

        for a in raw_att:
            ts = getattr(a, "timestamp", None)
            if ts:
                resultado["marcaciones"].append({
                    "user_id": str(getattr(a, "user_id", "") or "").strip(),
                    "fecha_hora": ts.strftime("%Y-%m-%d %H:%M:%S"),
                    "status": int(getattr(a, "status", 1) or 1),
                })

        resultado["ok"] = True
    except Exception as ex:
        resultado["error"] = str(ex)
    finally:
        if conn_zk:
            try:
                conn_zk.disconnect()
            except Exception:
                pass

    return resultado


def _procesar_lote_dispositivos(
    dispositivos: list[dict],
    solo_cedulas_reales: bool = True,
    max_workers: int = 6,
    timeout_por_reloj: int = 6,
) -> dict:
    """Núcleo compartido para sincronizar un conjunto de dispositivos hacia PostgreSQL."""
    conn = get_conn()
    cur = conn.cursor()

    resumen = {
        "total_dispositivos": len(dispositivos),
        "dispositivos_ok": 0,
        "dispositivos_falla": 0,
        "empleados_nuevos": 0,
        "permisos_agregados": 0,
        "marcaciones_nuevas": 0,
        "ids_cortos_omitidos": 0,
        "detalle_dispositivos": [],
    }

    if not dispositivos:
        cur.close()
        conn.close()
        return resumen

    try:
        cur.execute("SELECT pin, nombres FROM empleados;")
        mapa_pin_existente = {}
        for pin_db, _nom_db in cur.fetchall():
            p_norm = normalizar_pin(pin_db)
            mapa_pin_existente[p_norm] = str(pin_db).strip()

        lecturas = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_leer_dispositivo_zk, dev, timeout_por_reloj): dev
                for dev in dispositivos
            }
            for future in as_completed(futures):
                lecturas.append(future.result())

        for lec in lecturas:
            sn = lec["sn"]
            hac_id = lec["empacadora_id"]
            area = lec["area_fisica"]

            if not lec["ok"]:
                resumen["dispositivos_falla"] += 1
                resumen["detalle_dispositivos"].append({
                    "sn": sn,
                    "dispositivo": lec["nombre"],
                    "ip": lec["ip"],
                    "area": area,
                    "estado": f"ERROR: {lec['error']}",
                    "usuarios_leidos": 0,
                    "marcaciones_nuevas": 0,
                })
                continue

            resumen["dispositivos_ok"] += 1
            cur.execute("UPDATE dispositivos SET ultima_conexion = %s WHERE sn = %s;", (datetime.now(), sn))

            for u in lec["usuarios"]:
                pin_norm = normalizar_pin(u["user_id"])
                if not pin_norm or pin_norm == "0":
                    continue

                if solo_cedulas_reales and not es_cedula_valida_o_pin_real(pin_norm):
                    resumen["ids_cortos_omitidos"] += 1
                    continue

                if pin_norm in mapa_pin_existente:
                    pin_maestro = mapa_pin_existente[pin_norm]
                else:
                    pin_maestro = pin_norm
                    nombre_limpio = limpiar_nombre_zk(u["name"], pin_maestro)
                    cur.execute("""
                        INSERT INTO empleados (pin, cedula, nombres, empacadora_id, area, activo)
                        VALUES (%s, %s, %s, %s, %s, TRUE)
                        ON CONFLICT (pin) DO NOTHING;
                    """, (pin_maestro, pin_maestro, nombre_limpio, hac_id, area))
                    if cur.rowcount > 0:
                        resumen["empleados_nuevos"] += 1
                    mapa_pin_existente[pin_norm] = pin_maestro

                cur.execute("""
                    INSERT INTO empleado_haciendas (pin_empleado, empacadora_id, area_autorizada)
                    VALUES (%s, %s, %s)
                    ON CONFLICT DO NOTHING;
                """, (pin_maestro, hac_id, area))
                if cur.rowcount > 0:
                    resumen["permisos_agregados"] += 1

            marc_dev_nuevas = 0
            for m in lec["marcaciones"]:
                pin_m_norm = normalizar_pin(m["user_id"])
                if not pin_m_norm:
                    continue

                pin_destino = mapa_pin_existente.get(pin_m_norm, pin_m_norm)
                cur.execute("""
                    INSERT INTO marcaciones (pin_empleado, fecha_hora, sn_dispositivo, estado_verificacion)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT ON CONSTRAINT uq_marcacion_unica DO NOTHING;
                """, (pin_destino, m["fecha_hora"], sn, m["status"]))
                if cur.rowcount > 0:
                    marc_dev_nuevas += 1
                    resumen["marcaciones_nuevas"] += 1

            resumen["detalle_dispositivos"].append({
                "sn": sn,
                "dispositivo": lec["nombre"],
                "ip": lec["ip"],
                "area": area,
                "estado": "OK",
                "usuarios_leidos": len(lec["usuarios"]),
                "marcaciones_nuevas": marc_dev_nuevas,
            })

        conn.commit()
        return resumen
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cur.close()
        conn.close()


def sincronizar_dispositivos_seleccionados(
    sns_seleccionados: list[str],
    solo_cedulas_reales: bool = True,
    max_workers: int = 6,
    timeout_por_reloj: int = 6,
) -> dict:
    """
    Sincronización selectiva Biométricos marcados -> PostgreSQL:
    Solo lee y sincroniza los `sn` indicados por el usuario.
    """
    sns_limpios = [str(s).strip() for s in (sns_seleccionados or []) if str(s).strip()]
    if not sns_limpios:
        return {
            "total_dispositivos": 0,
            "dispositivos_ok": 0,
            "dispositivos_falla": 0,
            "empleados_nuevos": 0,
            "permisos_agregados": 0,
            "marcaciones_nuevas": 0,
            "ids_cortos_omitidos": 0,
            "detalle_dispositivos": [],
        }

    conn = get_conn()
    cur = conn.cursor()
    try:
        placeholders = ",".join(["%s"] * len(sns_limpios))
        cur.execute(f"""
            SELECT d.sn, d.nombre, d.ip_address, d.port, d.empacadora_id, d.rol,
                   e.codigo AS cod_emp, e.nombre AS emp_nombre
            FROM dispositivos d
            JOIN empacadoras e ON d.empacadora_id = e.id
            WHERE d.sn IN ({placeholders})
              AND d.ip_address IS NOT NULL
              AND TRIM(d.ip_address) <> ''
              AND d.rol IS NOT NULL
              AND TRIM(d.rol) <> ''
            ORDER BY e.codigo ASC, d.nombre ASC;
        """, tuple(sns_limpios))
        cols = [desc[0] for desc in cur.description]
        dispositivos = [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        cur.close()
        conn.close()

    return _procesar_lote_dispositivos(
        dispositivos=dispositivos,
        solo_cedulas_reales=solo_cedulas_reales,
        max_workers=max_workers,
        timeout_por_reloj=timeout_por_reloj,
    )


def sincronizar_todos_los_biometricos(
    solo_cedulas_reales: bool = True,
    max_workers: int = 8,
    timeout_por_reloj: int = 6,
) -> dict:
    """
    Sincronización por lotes de todos los biométricos activos -> PostgreSQL.
    """
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT d.sn, d.nombre, d.ip_address, d.port, d.empacadora_id, d.rol,
                   e.codigo AS cod_emp, e.nombre AS emp_nombre
            FROM dispositivos d
            JOIN empacadoras e ON d.empacadora_id = e.id
            WHERE d.ip_address IS NOT NULL
              AND TRIM(d.ip_address) <> ''
              AND d.rol IS NOT NULL
              AND TRIM(d.rol) <> ''
            ORDER BY e.codigo ASC, d.nombre ASC;
        """)
        cols = [desc[0] for desc in cur.description]
        dispositivos = [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        cur.close()
        conn.close()

    return _procesar_lote_dispositivos(
        dispositivos=dispositivos,
        solo_cedulas_reales=solo_cedulas_reales,
        max_workers=max_workers,
        timeout_por_reloj=timeout_por_reloj,
    )


if __name__ == "__main__":
    print("Iniciando sincronización de biométricos...")
    rep = sincronizar_todos_los_biometricos()
    print(rep)