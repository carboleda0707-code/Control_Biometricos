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
    page_title="Control Biométrico Bananeras",
    page_icon="🍌",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Estilos minimalistas, bajando el contenido aprox. 1 línea y manteniendo letra normal/compacta
st.markdown(
    """
    <style>
        .block-container {
            padding-top: 1.15rem !important;
            padding-bottom: 1rem !important;
        }
        div[data-testid="stVerticalBlock"] > div {
            gap: 0.45rem;
        }
        hr {
            margin-top: 0.35rem !important;
            margin-bottom: 0.45rem !important;
        }
        h1 {
            font-size: 1.4rem !important;
            font-weight: 600 !important;
            margin-top: 0 !important;
            margin-bottom: 0.15rem !important;
            line-height: 1.2 !important;
        }
        h2, .stSubheader {
            font-size: 1.05rem !important;
            font-weight: 600 !important;
            margin-top: 0.25rem !important;
            margin-bottom: 0.25rem !important;
        }
        h3 {
            font-size: 0.95rem !important;
            font-weight: 600 !important;
        }
        p, label, .stMarkdown, .stCaption, div[data-testid="stText"] {
            font-size: 0.85rem !important;
        }
        div[data-testid="stSidebar"] .block-container {
            padding-top: 0.95rem !important;
        }
        div[data-testid="stSidebar"] h1 {
            font-size: 1.15rem !important;
        }
    </style>
    """,
    unsafe_allow_html=True,
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

    # Prioriza instalaciones recientes de PostgreSQL en Windows (18, 17, 16...) frente a versiones antiguas en PATH
    for ver in ("18", "17", "16", "15", "14"):
        for base_dir in (Path(r"C:\Program Files\PostgreSQL"), Path(r"C:\Program Files (x86)\PostgreSQL")):
            candidato_win = base_dir / ver / "bin" / f"{nombre_binario}.exe"
            if candidato_win.exists():
                return str(candidato_win)

    return nombre_binario


def _limpiar_dumps_vacios() -> int:
    """Elimina archivos .dump corruptos o de 0 bytes en la carpeta de respaldos."""
    eliminados = 0
    for f in _carpeta_respaldos().glob("*.dump"):
        try:
            if f.is_file() and f.stat().st_size == 0:
                f.unlink()
                eliminados += 1
        except Exception:
            pass
    return eliminados


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

    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "Error ejecutando pg_dump")

        if not destino.exists() or destino.stat().st_size == 0:
            raise RuntimeError("pg_dump finalizó sin escribir datos (archivo de 0 bytes).")

        size_kb = round(destino.stat().st_size / 1024, 2)
        return {
            "archivo": nombre_archivo,
            "tamano_kb": size_kb,
            "creado_en": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception:
        try:
            if destino.exists() and destino.stat().st_size == 0:
                destino.unlink()
        except Exception:
            pass
        raise


def ejecutar_pg_restore(nombre_archivo: str) -> dict:
    base = _carpeta_respaldos().resolve()
    archivo_resuelto = (base / Path(nombre_archivo).name).resolve()

    if not archivo_resuelto.exists() or archivo_resuelto.parent != base:
        raise FileNotFoundError("El archivo de respaldo seleccionado no existe.")

    if archivo_resuelto.stat().st_size == 0:
        raise ValueError("El archivo de respaldo seleccionado está vacío (0 KB) y no puede restaurarse.")

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
        prog["ultimo_estado"] = f"OK ({info['archivo']} - {info['tamano_kb']} KB)"
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
            prog["habilitado"] = False
            prog["ultimo_estado"] = f"Error (auto-pausado): {e}"
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
        "errores": [],
    }
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()

        consultas = [
            ("companias", "SELECT COUNT(*) FROM companias;"),
            ("haciendas", "SELECT COUNT(*) FROM empacadoras WHERE activa = TRUE;"),
            ("empleados", "SELECT COUNT(*) FROM empleados WHERE activo = TRUE;"),
            ("dispositivos", "SELECT COUNT(*) FROM dispositivos;"),
            ("marcaciones_hoy", "SELECT COUNT(*) FROM marcaciones WHERE fecha_hora::date = CURRENT_DATE;"),
        ]
        for clave, sql in consultas:
            try:
                cur.execute(sql)
                resumen[clave] = int(cur.fetchone()[0] or 0)
            except Exception as exc_q:
                try:
                    conn.rollback()
                except Exception:
                    pass
                resumen["errores"].append(f"{clave}: {exc_q}")

        cur.close()
    except Exception as exc_c:
        resumen["errores"].append(f"conexion: {exc_c}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return resumen


def _render_respaldos_y_restauracion() -> None:
    st.title("Copia de Seguridad y Restauración")
    st.caption(f"Base de datos: {settings.DB_NAME} | Binario: {_binario_pg('pg_dump')} | Carpeta: {_carpeta_respaldos()}")

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
                prog["ultimo_estado"] = f"OK ({info['archivo']} - {info['tamano_kb']} KB)"
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
        archivos_todos = sorted(carpeta.glob("*.dump"), key=lambda p: p.stat().st_mtime, reverse=True)
        archivos_validos = [a for a in archivos_todos if a.stat().st_size > 0]
        archivos_vacios = [a for a in archivos_todos if a.stat().st_size == 0]

        if archivos_validos:
            opciones = [a.name for a in archivos_validos]
            archivo_sel = st.selectbox("Seleccione el respaldo (.dump)", opciones, key="respaldo_archivo_sel")
            if st.button("Restaurar base seleccionada", type="primary", use_container_width=True):
                try:
                    info = ejecutar_pg_restore(archivo_sel)
                    st.success(f"Base restaurada desde {info['archivo']}.")
                except Exception as exc:
                    st.error(f"Error al restaurar base de datos: {exc}")
        else:
            st.warning("Aún no existen respaldos válidos (> 0 KB) en la carpeta de respaldos.")

        if archivos_vacios:
            if st.button(f"Limpiar {len(archivos_vacios)} archivo(s) vacíos (0 KB)", use_container_width=True):
                borrados = _limpiar_dumps_vacios()
                st.success(f"Se eliminaron {borrados} archivo(s) vacíos.")
                st.rerun()

    st.divider()
    st.subheader("Historial de Copias de Seguridad")
    carpeta = _carpeta_respaldos()
    archivos = [f for f in sorted(carpeta.glob("*.dump"), key=lambda p: p.stat().st_mtime, reverse=True) if f.stat().st_size > 0]
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
        st.caption("Sin respaldos válidos generados todavía.")


bootstrap_db()
iniciar_worker_respaldo()

modo_hibrido = bool(getattr(settings, "RAILWAY_ENABLED", False))
heartbeat_seg = int(getattr(settings, "PUSH_HEARTBEAT_SEGUNDOS", 300))

# Menú lateral estructurado (contraíble y expandible desde el control nativo de Streamlit)
st.sidebar.title("BioControl Bananeras")
st.sidebar.caption("Modo Híbrido (Local + Nube)" if modo_hibrido else "Sistema Autónomo Local (LAN/VLAN)")
st.sidebar.divider()

menu = st.sidebar.radio(
    "Navegación Principal:",
    [
        "Dashboard General",
        "Mantenimiento de Compañías",
        "Haciendas Empacadoras",
        "Áreas y Departamentos",
        "Gestión de Empleados",
        "Dispositivos Biométricos",
        "Reportes y Auditoría",
        "Copia de Seguridad / Restaurar",
    ],
)

st.sidebar.divider()
st.sidebar.caption("Estado de Conexión")

if "db_status" not in st.session_state:
    st.session_state.db_status = verificar_conexion_db()

if st.sidebar.button("Reprobar conexión", use_container_width=True):
    st.session_state.db_status = verificar_conexion_db()

db_ok, db_detalle = st.session_state.db_status

if db_ok:
    st.sidebar.success(f"BD Conectada ({settings.DB_NAME})")
else:
    st.sidebar.error("BD Sin conexión")
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

if menu == "Dashboard General":
    st.title("Panel de Control")
    st.caption("Resumen operativo de compañías, haciendas, personal, dispositivos ZKTeco y marcaciones del día.")

    if db_ok:
        datos_dash = _obtener_resumen_dashboard()
        k1, k2, k3, k4, k5 = st.columns(5)
        k1.metric("Compañías", datos_dash["companias"])
        k2.metric("Haciendas Activas", datos_dash["haciendas"])
        k3.metric("Empleados Activos", datos_dash["empleados"])
        k4.metric("Biométricos", datos_dash["dispositivos"])
        k5.metric("Marcaciones Hoy", datos_dash["marcaciones_hoy"])

        if datos_dash["errores"]:
            st.error("Errores en consultas del dashboard:\n" + "\n".join(datos_dash["errores"]))

        st.divider()
        st.caption("Seleccione una opción del menú lateral para gestionar catálogos, sincronizar relojes o auditar reportes.")
    else:
        st.warning("La base de datos no está conectada en este momento. Revise el estado en la barra lateral.")

elif menu == "Mantenimiento de Compañías":
    companias.render()

elif menu == "Haciendas Empacadoras":
    haciendas.render()

elif menu == "Áreas y Departamentos":
    areas.render()

elif menu == "Gestión de Empleados":
    empleados.render()

elif menu == "Dispositivos Biométricos":
    dispositivos.render()

elif menu == "Reportes y Auditoría":
    reportes.render()

elif menu == "Copia de Seguridad / Restaurar":
    _render_respaldos_y_restauracion()