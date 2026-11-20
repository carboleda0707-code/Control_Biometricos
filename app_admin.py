# -*- coding: utf-8 -*-
import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import threading

import streamlit as st

from config import settings
from db import get_conn, init_db
from views import areas, companias, dispositivos, empleados, haciendas, reportes

st.set_page_config(
    page_title="Control Biométrico Bananeras - Panel Admin",
    page_icon="🍌",
    layout="wide",
)


@st.cache_resource
def bootstrap_db():
    init_db()
    return True


def _carpeta_respaldos() -> Path:
    carpeta_cfg = os.getenv("BACKUP_DIR", "").strip()
    ruta = Path(carpeta_cfg) if carpeta_cfg else Path(settings.BASE_DIR) / "respaldos"
    ruta.mkdir(parents=True, exist_ok=True)
    return ruta


def _archivo_programacion() -> Path:
    return _carpeta_respaldos() / "programacion_respaldo.json"


def _cargar_programacion() -> dict:
    base = {
        "habilitado": False,
        "fecha_hora": "",
        "repetir_semanal": False,
        "ultima_ejecucion": None,
        "ultimo_estado": None,
    }
    ruta = _archivo_programacion()
    if not ruta.exists():
        return base
    try:
        data = json.loads(ruta.read_text(encoding="utf-8"))
        return {**base, **(data if isinstance(data, dict) else {})}
    except Exception:
        return base


def _guardar_programacion(data: dict) -> None:
    _archivo_programacion().write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _binario_pg(nombre_binario: str) -> str:
    pg_bin = os.getenv("PG_BIN_DIR", "").strip()
    if pg_bin:
        for candidato in (Path(pg_bin) / f"{nombre_binario}.exe", Path(pg_bin) / nombre_binario):
            if candidato.exists():
                return str(candidato)
    return nombre_binario


def ejecutar_pg_dump() -> dict:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    nombre_archivo = f"{settings.DB_NAME}_{timestamp}.dump"
    destino = _carpeta_respaldos() / nombre_archivo

    env = os.environ.copy()
    if settings.DB_PASSWORD:
        env["PGPASSWORD"] = settings.DB_PASSWORD

    cmd = [
        _binario_pg("pg_dump"),
        "-h", str(settings.DB_HOST),
        "-p", str(settings.DB_PORT),
        "-U", str(settings.DB_USER),
        "-Fc",
        "-b",
        "-f", str(destino),
        str(settings.DB_NAME),
    ]

    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Error ejecutando pg_dump")

    size_kb = round(destino.stat().st_size / 1024, 2) if destino.exists() else 0.0
    return {
        "archivo": nombre_archivo,
        "tamano_kb": size_kb,
        "creado_en": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def ejecutar_pg_restore(nombre_archivo: str) -> dict:
    base = _carpeta_respaldos().resolve()
    archivo_resuelto = (base / Path(nombre_archivo).name).resolve()

    if not archivo_resuelto.exists() or archivo_resuelto.parent != base:
        raise FileNotFoundError("El archivo de respaldo seleccionado no existe.")

    env = os.environ.copy()
    if settings.DB_PASSWORD:
        env["PGPASSWORD"] = settings.DB_PASSWORD

    cmd = [
        _binario_pg("pg_restore"),
        "-h", str(settings.DB_HOST),
        "-p", str(settings.DB_PORT),
        "-U", str(settings.DB_USER),
        "-d", str(settings.DB_NAME),
        "--clean",
        "--if-exists",
        str(archivo_resuelto),
    ]

    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Error ejecutando pg_restore")

    return {
        "archivo": archivo_resuelto.name,
        "restaurado_en": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _paso_worker_respaldo() -> None:
    prog = _cargar_programacion()
    if not (prog.get("habilitado") and prog.get("fecha_hora")):
        return
    objetivo = datetime.fromisoformat(prog["fecha_hora"])
    ahora = datetime.now()
    clave_ejecucion = objetivo.strftime("%Y-%m-%d %H:%M")

    if ahora >= objetivo and (prog.get("ultima_ejecucion") or "") != clave_ejecucion:
        info = ejecutar_pg_dump()
        prog["ultima_ejecucion"] = clave_ejecucion
        prog["ultimo_estado"] = f"OK ({info['archivo']})"
        if prog.get("repetir_semanal"):
            siguiente = objetivo + timedelta(days=7)
            prog["fecha_hora"] = siguiente.strftime("%Y-%m-%dT%H:%M")
        else:
            prog["habilitado"] = False
        _guardar_programacion(prog)


async def worker_respaldo_programado() -> None:
    while True:
        try:
            _paso_worker_respaldo()
        except Exception as e:
            prog = _cargar_programacion()
            prog["ultimo_estado"] = f"Error: {e}"
            _guardar_programacion(prog)
        await asyncio.sleep(20)


@st.cache_resource
def iniciar_worker_respaldo() -> bool:
    def _runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(worker_respaldo_programado())

    hilo = threading.Thread(target=_runner, daemon=True, name="worker-respaldo-bd")
    hilo.start()
    return True


def verificar_conexion_db() -> tuple[bool, str]:
    """Valida conexión activa a PostgreSQL sin exponer credenciales sensibles."""
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT 1;")
        cur.fetchone()
        cur.close()
        return True, "Conectada"
    except Exception as exc:
        return False, str(exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _obtener_resumen_dashboard() -> dict:
    """Obtiene conteos rápidos del sistema si la BD está conectada."""
    resumen = {
        "companias": 0,
        "haciendas": 0,
        "empleados": 0,
        "dispositivos": 0,
        "marcaciones_hoy": 0,
    }
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM companias;")
        resumen["companias"] = int(cur.fetchone()[0] or 0)

        cur.execute("SELECT COUNT(*) FROM empacadoras WHERE activa = TRUE;")
        resumen["haciendas"] = int(cur.fetchone()[0] or 0)

        cur.execute("SELECT COUNT(*) FROM empleados WHERE activo = TRUE;")
        resumen["empleados"] = int(cur.fetchone()[0] or 0)

        cur.execute("SELECT COUNT(*) FROM dispositivos;")
        resumen["dispositivos"] = int(cur.fetchone()[0] or 0)

        cur.execute("SELECT COUNT(*) FROM marcaciones WHERE fecha_hora::date = CURRENT_DATE;")
        resumen["marcaciones_hoy"] = int(cur.fetchone()[0] or 0)

        cur.close()
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return resumen


def _render_respaldos_y_restauracion() -> None:
    st.title("Copia de Seguridad y Restauración de Base de Datos")
    st.caption(f"Base de datos: {settings.DB_NAME} | Carpeta: {_carpeta_respaldos()}")

    prog = _cargar_programacion()
    col_izq, col_der = st.columns(2)

    with col_izq:
        st.subheader("Programar por Fecha y Hora")
        fecha_defecto = datetime.now().date()
        hora_defecto = datetime.now().replace(second=0, microsecond=0).time()
        if prog.get("fecha_hora"):
            try:
                dt_prog = datetime.fromisoformat(prog["fecha_hora"])
                fecha_defecto = dt_prog.date()
                hora_defecto = dt_prog.time().replace(second=0, microsecond=0)
            except Exception:
                pass

        fecha_sel = st.date_input("Fecha de respaldo", value=fecha_defecto, key="respaldo_fecha")
        hora_sel = st.time_input("Hora de respaldo (Ej. 14:00)", value=hora_defecto, key="respaldo_hora")
        repetir_sem = st.checkbox(
            "Repetir cada semana en el mismo día y hora",
            value=bool(prog.get("repetir_semanal")),
            key="respaldo_semanal",
        )
        habilitado = st.checkbox(
            "Activar programación automática",
            value=bool(prog.get("habilitado")),
            key="respaldo_habilitado",
        )

        b1, b2 = st.columns(2)
        if b1.button("Guardar programación", type="primary", use_container_width=True):
            fecha_hora_iso = f"{fecha_sel.isoformat()}T{hora_sel.strftime('%H:%M')}"
            prog.update({
                "habilitado": bool(habilitado),
                "fecha_hora": fecha_hora_iso,
                "repetir_semanal": bool(repetir_sem),
            })
            _guardar_programacion(prog)
            st.success(f"Programación guardada para {fecha_hora_iso.replace('T', ' ')}.")

        if b2.button("Respaldar ahora", use_container_width=True):
            try:
                info = ejecutar_pg_dump()
                prog["ultima_ejecucion"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                prog["ultimo_estado"] = f"OK ({info['archivo']})"
                _guardar_programacion(prog)
                st.success(f"Respaldo generado: {info['archivo']} ({info['tamano_kb']} KB)")
            except Exception as exc:
                st.error(f"Error al generar respaldo: {exc}")

    with col_der:
        st.subheader("Restaurar Base de Datos")
        estado_txt = prog.get("ultimo_estado") or "Sin ejecuciones registradas"
        prog_txt = (
            f"{prog['fecha_hora'].replace('T', ' ')} ({prog.get('ultimo_estado') or 'Activa'})"
            if prog.get("habilitado") and prog.get("fecha_hora")
            else f"Inactiva | Último estado: {estado_txt}"
        )
        st.info(f"Programación: {prog_txt}")

        carpeta = _carpeta_respaldos()
        archivos = sorted(carpeta.glob("*.dump"), key=lambda p: p.stat().st_mtime, reverse=True)
        if archivos:
            opciones = [a.name for a in archivos]
            archivo_sel = st.selectbox("Seleccione el respaldo (.dump)", opciones, key="respaldo_archivo_sel")
            if st.button("Restaurar base seleccionada", type="primary", use_container_width=True):
                try:
                    info = ejecutar_pg_restore(archivo_sel)
                    st.success(f"Base restaurada desde {info['archivo']}.")
                except Exception as exc:
                    st.error(f"Error al restaurar base de datos: {exc}")
        else:
            st.warning("Aún no existen archivos .dump en la carpeta de respaldos.")

    st.divider()
    st.subheader("Historial de Copias de Seguridad")
    carpeta = _carpeta_respaldos()
    archivos = sorted(carpeta.glob("*.dump"), key=lambda p: p.stat().st_mtime, reverse=True)
    if archivos:
        filas = [
            {
                "Archivo": f.name,
                "Tamaño (KB)": round(f.stat().st_size / 1024, 2),
                "Fecha de Creación": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            }
            for f in archivos
        ]
        st.dataframe(filas, use_container_width=True, hide_index=True)
    else:
        st.caption("Sin respaldos generados todavía.")


bootstrap_db()
iniciar_worker_respaldo()

modo_hibrido = bool(getattr(settings, "RAILWAY_ENABLED", False))
heartbeat_seg = int(getattr(settings, "PUSH_HEARTBEAT_SEGUNDOS", 300))

# Menú lateral estructurado
st.sidebar.title("🍌 BioControl Bananeras")
st.sidebar.caption("Modo Híbrido (Local + Nube)" if modo_hibrido else "Sistema Autónomo Local (LAN/VLAN)")
st.sidebar.divider()

menu = st.sidebar.radio(
    "Navegación Principal:",
    [
        "📊 Dashboard General",
        "🏛️ Mantenimiento de Compañías",
        "🌴 Haciendas Empacadoras",
        "📂 Áreas y Departamentos",
        "👥 Gestión de Empleados",
        "🖨️ Dispositivos Biométricos",
        "📊 Reportes y Auditoría",
        "💾 Copia de Seguridad / Restaurar",
    ],
)

# Estado de Base de Datos y Servicio en el menú lateral (sin contraseña)
st.sidebar.divider()
st.sidebar.caption("Estado de Conexión")

if "db_status" not in st.session_state:
    st.session_state.db_status = verificar_conexion_db()

if st.sidebar.button("🔄 Reprobar conexión", use_container_width=True):
    st.session_state.db_status = verificar_conexion_db()

db_ok, db_detalle = st.session_state.db_status

if db_ok:
    st.sidebar.success(f"🟢 BD Conectada ({settings.DB_NAME})")
else:
    st.sidebar.error("🔴 BD Sin conexión")
    st.sidebar.caption(f"Detalle: {db_detalle}")

st.sidebar.markdown(
    f"""
    **Host/Puerto:** `{settings.DB_HOST}:{settings.DB_PORT}`  
    **Base de datos:** `{settings.DB_NAME}`  
    **Usuario:** `{settings.DB_USER}`  
    **Puerto servicio:** `{settings.SERVER_PORT}`  
    **Réplica nube:** `{"Activa" if modo_hibrido else "Desactivada"}`  
    **Heartbeat PUSH:** `{heartbeat_seg}s`
    """
)

if menu == "📊 Dashboard General":
    st.title("Panel de Control - Biométricos Bananeras")
    st.caption("Resumen operativo de compañías, haciendas, personal, dispositivos ZKTeco y marcaciones del día.")

    if db_ok:
        datos_dash = _obtener_resumen_dashboard()
        k1, k2, k3, k4, k5 = st.columns(5)
        k1.metric("Compañías", datos_dash["companias"])
        k2.metric("Haciendas Activas", datos_dash["haciendas"])
        k3.metric("Empleados Activos", datos_dash["empleados"])
        k4.metric("Biométricos", datos_dash["dispositivos"])
        k5.metric("Marcaciones Hoy", datos_dash["marcaciones_hoy"])
        st.divider()
        st.info("Seleccione una opción del menú lateral para gestionar catálogos, sincronizar relojes o auditar reportes.")
    else:
        st.warning("La base de datos no está conectada en este momento. Revise el estado en la barra lateral.")

elif menu == "🏛️ Mantenimiento de Compañías":
    companias.render()

elif menu == "🌴 Haciendas Empacadoras":
    haciendas.render()

elif menu == "📂 Áreas y Departamentos":
    areas.render()

elif menu == "👥 Gestión de Empleados":
    empleados.render()

elif menu == "🖨️ Dispositivos Biométricos":
    dispositivos.render()

elif menu == "📊 Reportes y Auditoría":
    reportes.render()

elif menu == "💾 Copia de Seguridad / Restaurar":
    _render_respaldos_y_restauracion()