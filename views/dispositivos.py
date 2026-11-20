# -*- coding: utf-8 -*-
from datetime import datetime
import streamlit as st
import pandas as pd
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from db import get_conn
from sync_biometricos import sincronizar_dispositivos_seleccionados


def run_query(query, params=None, fetch=False):
    conn = get_conn()
    cur = conn.cursor()
    try:
        if params:
            cur.execute(query, params)
        else:
            cur.execute(query)
        if fetch:
            cols = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            return pd.DataFrame(rows, columns=cols)
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cur.close()
        conn.close()


def probar_ip_puerto(ip, port=4370, timeout=0.5):
    """Comprueba si un dispositivo ZKTeco responde en TCP/IP."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((ip, port))
            return ip
    except Exception:
        return None


def _esta_conectado_push(ultima_conexion, max_segundos: int = 300) -> bool:
    """Evalúa si un reloj PUSH_ADMS reportó recientemente."""
    if not ultima_conexion or pd.isna(ultima_conexion):
        return False
    if isinstance(ultima_conexion, pd.Timestamp):
        dt_ult = ultima_conexion.to_pydatetime()
    elif isinstance(ultima_conexion, str):
        try:
            dt_ult = datetime.fromisoformat(ultima_conexion)
        except ValueError:
            return False
    else:
        dt_ult = ultima_conexion

    if getattr(dt_ult, "tzinfo", None) is not None:
        dt_ult = dt_ult.replace(tzinfo=None)

    return (datetime.now() - dt_ult).total_seconds() <= max_segundos


def _esta_conectado_pull(ip, port, timeout: float = 0.8) -> bool:
    """Evalúa si un reloj PULL_SDK responde por TCP en su IP y puerto."""
    ip_limpia = str(ip or "").strip()
    if not ip_limpia:
        return False
    puerto = int(port or 4370)
    try:
        with socket.create_connection((ip_limpia, puerto), timeout=timeout):
            return True
    except Exception:
        return False


def _evaluar_conexion_dispositivo(row) -> bool:
    """Determina si el dispositivo está conectado según su modo (PUSH_ADMS o PULL_SDK)."""
    modo = str(row.get("tipo_conexion") or "PUSH_ADMS").strip().upper()
    if modo == "PUSH_ADMS":
        return _esta_conectado_push(row.get("ultima_conexion"), max_segundos=300)
    return _esta_conectado_pull(row.get("ip_address"), row.get("port"), timeout=0.8)


def _estilizar_tabla_dispositivos(df_disp: pd.DataFrame):
    """Pinta la celda de 'tipo_conexion' en verde si está conectado y en rojo si no."""
    df_base = df_disp.drop(columns=["empacadora_id"], errors="ignore").copy()
    estados = [_evaluar_conexion_dispositivo(r) for _, r in df_disp.iterrows()]
    df_base["_conectado"] = estados

    def _aplicar_estilo_fila(row):
        estilos = [""] * len(row)
        if "tipo_conexion" in row.index:
            idx_col = row.index.get_loc("tipo_conexion")
            if row["_conectado"]:
                estilos[idx_col] = (
                    "background-color: #dcfce7; color: #166534; font-weight: 700;"
                )
            else:
                estilos[idx_col] = (
                    "background-color: #fee2e2; color: #991b1b; font-weight: 700;"
                )
        return estilos

    styler = df_base.style.apply(_aplicar_estilo_fila, axis=1)
    return styler


def obtener_sn_zk(ip: str, port: int = 4370, timeout: int = 3) -> str:
    """Intenta leer el Número de Serie (SN) de un reloj ZKTeco por red."""
    try:
        from zk import ZK

        zk = ZK(ip, port=int(port or 4370), timeout=timeout)
        conn = zk.connect()
        try:
            sn = conn.get_serialnumber()
            if sn:
                return str(sn).strip()
            return "No reportado"
        finally:
            conn.disconnect()
    except Exception:
        return "No disponible por SDK"


def _normalizar_pin_zk(pin_str: str) -> str:
    """Limpia espacios y quita ceros a la izquierda si es numérico (ej. 0914592936 -> 914592936)."""
    clean = str(pin_str or "").strip()
    if clean.isdigit():
        stripped = clean.lstrip("0")
        return stripped if stripped else "0"
    return clean


def _uid_desde_pin(pin_str: str, usados: set | None = None) -> int:
    """Genera un UID de 16 bits válido (1..65535) para el formato 'H' de pyzk."""
    usados = usados if usados is not None else set()
    digits = "".join(ch for ch in str(pin_str) if ch.isdigit())

    if digits:
        base_uid = (int(digits) % 65534) + 1
    else:
        base_uid = 1

    uid_candidato = base_uid
    intentos = 0
    while uid_candidato in usados and intentos < 65534:
        uid_candidato = (uid_candidato % 65534) + 1
        intentos += 1

    usados.add(uid_candidato)
    return uid_candidato


def enviar_empleados_a_reloj(ip: str, port: int, empleados_df: pd.DataFrame):
    """Conecta a un reloj ZKTeco y sube/actualiza los empleados recibidos."""
    from zk import ZK

    zk = ZK(ip, port=int(port or 4370), timeout=5)
    conn_zk = zk.connect()
    enviados = 0
    errores = []

    try:
        uid_por_user_id = {}
        uids_en_uso = set()
        try:
            usuarios_actuales = conn_zk.get_users() or []
            for u in usuarios_actuales:
                u_uid = int(getattr(u, "uid", 0) or 0)
                u_pin = _normalizar_pin_zk(getattr(u, "user_id", "") or "")
                if 1 <= u_uid <= 65535:
                    uids_en_uso.add(u_uid)
                    if u_pin:
                        uid_por_user_id[u_pin] = u_uid
        except Exception:
            pass

        for _, emp in empleados_df.iterrows():
            pin_raw = str(emp["pin"]).strip()
            pin_zk = _normalizar_pin_zk(pin_raw)
            nombre_str = str(emp["nombres"]).strip()[:24]
            try:
                if pin_zk in uid_por_user_id:
                    uid_asignado = uid_por_user_id[pin_zk]
                elif pin_raw in uid_por_user_id:
                    uid_asignado = uid_por_user_id[pin_raw]
                else:
                    uid_asignado = _uid_desde_pin(pin_zk, uids_en_uso)

                conn_zk.set_user(
                    uid=uid_asignado,
                    name=nombre_str,
                    privilege=0,
                    password="",
                    card=0,
                    user_id=pin_zk,
                )
                enviados += 1
            except Exception as sub_ex:
                errores.append(f"PIN {pin_raw} (zk:{pin_zk}): {sub_ex}")
    finally:
        conn_zk.disconnect()

    return enviados, errores


def render():
    st.title("🖨️ Dispositivos Biométricos (ZKTeco)")
    st.write("Administración de relojes biométricos, escáner de red por segmento VLAN/IP y sincronización estricta por Hacienda + Área.")

    df_haciendas = run_query("""
        SELECT e.id, e.codigo, e.nombre, c.nombre AS compania
        FROM empacadoras e
        JOIN companias c ON e.compania_id = c.id
        WHERE e.activa = TRUE
        ORDER BY c.nombre ASC, e.codigo ASC;
    """, fetch=True)

    tab_lista, tab_crear, tab_editar, tab_sync, tab_escaner = st.tabs([
        "📋 Listado y Estado",
        "➕ Registrar Dispositivo",
        "✏️ Modificar / Eliminar",
        "🔄 Sincronización Red",
        "🔍 Escáner de Red (IP/VLAN)"
    ])

    # --------------------------------------------------------------------------
    # PESTAÑA 1: LISTADO Y FILTROS
    # --------------------------------------------------------------------------
    with tab_lista:
        st.subheader("Relojes Biométricos Registrados")

        if df_haciendas.empty:
            st.info("No hay haciendas empacadoras registradas en el sistema.")
        else:
            filtro_hac_opts = {"Todas las haciendas": None}
            for _, r in df_haciendas.iterrows():
                filtro_hac_opts[f"[{r['codigo']}] {r['nombre']} ({r['compania']})"] = int(r["id"])

            filtro_sel = st.selectbox("Filtrar dispositivos por Hacienda Empacadora:", list(filtro_hac_opts.keys()))
            hac_filtro_id = filtro_hac_opts[filtro_sel]

            base_sql = """
                SELECT d.sn, d.nombre AS dispositivo, e.codigo AS cod_emp, e.nombre AS empacadora,
                       d.rol AS area_fisica, d.tipo_conexion, d.ip_address, d.port, d.ultima_conexion, d.empacadora_id
                FROM dispositivos d
                JOIN empacadoras e ON d.empacadora_id = e.id
            """
            params = []
            if hac_filtro_id:
                base_sql += " WHERE d.empacadora_id = %s"
                params.append(hac_filtro_id)

            base_sql += " ORDER BY e.codigo ASC, d.nombre ASC;"
            df_disp = run_query(base_sql, tuple(params) if params else None, fetch=True)

            if df_disp.empty:
                st.info("No hay dispositivos biométricos registrados para el filtro seleccionado.")
            else:
                styler_disp = _estilizar_tabla_dispositivos(df_disp)
                st.dataframe(
                    styler_disp,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "sn": "Número de Serie (SN)",
                        "dispositivo": "Nombre / Alias del Reloj",
                        "cod_emp": "Cód. Hacienda",
                        "empacadora": "Hacienda Empacadora",
                        "area_fisica": "Área Física de Control",
                        "tipo_conexion": "Conexión",
                        "ip_address": "Dirección IP",
                        "port": "Puerto",
                        "ultima_conexion": "Última Conexión Activa",
                        "_conectado": None,
                    }
                )

    # --------------------------------------------------------------------------
    # PESTAÑA 2: REGISTRAR DISPOSITIVO
    # --------------------------------------------------------------------------
    with tab_crear:
        st.subheader("Registrar Nuevo Dispositivo ZKTeco")

        if df_haciendas.empty:
            st.warning("⚠️ Debe registrar al menos una hacienda empacadora antes de añadir dispositivos.")
        else:
            mapa_hac = {f"[{r['codigo']}] {r['nombre']} ({r['compania']})": int(r["id"]) for _, r in df_haciendas.iterrows()}
            labels_hac = list(mapa_hac.keys())

            with st.form("form_crear_dispositivo_clean", clear_on_submit=True):
                col1, col2 = st.columns(2)
                with col1:
                    d_hac_label = st.selectbox("Hacienda Empacadora Asignada *", labels_hac)
                    d_sn = st.text_input("Número de Serie (SN) *", placeholder="Ej: CZHX23456789 (único del equipo)")
                    d_nombre = st.text_input(
                        "Nombre / Alias Descriptivo del Reloj *",
                        placeholder="Ej: Reloj Biométrico Comedor 1 / Puerta Norte"
                    )
                    d_rol = st.selectbox(
                        "Área Física de Control (Para permisos de personal) *",
                        ["COMEDOR", "EMPAQUE", "PALETIZADO", "OFICINA", "GARITA"]
                    )
                with col2:
                    d_tipo = st.selectbox("Tipo de Conexión *", ["PUSH_ADMS", "PULL_SDK"])
                    d_ip = st.text_input("Dirección IP (Ej: 192.168.30.50 o 172.16.4.15)", placeholder="Ej: 192.168.100.12")
                    d_port = st.number_input("Puerto de Comunicación", min_value=1, max_value=65535, value=4370)

                st.divider()
                if st.form_submit_button("Guardar Dispositivo", type="primary", use_container_width=True):
                    if not d_sn.strip() or not d_nombre.strip():
                        st.error("El Número de Serie (SN) y el Nombre / Alias del dispositivo son obligatorios.")
                    else:
                        try:
                            run_query("""
                                INSERT INTO dispositivos (sn, empacadora_id, nombre, rol, tipo_conexion, ip_address, port)
                                VALUES (%s, %s, %s, %s, %s, %s, %s);
                            """, (
                                d_sn.strip(),
                                mapa_hac[d_hac_label],
                                d_nombre.strip(),
                                d_rol,
                                d_tipo,
                                d_ip.strip() if d_ip else None,
                                int(d_port)
                            ))
                            st.success(f"¡Dispositivo '{d_nombre}' registrado en el área '{d_rol}' con éxito!")
                        except Exception as ex:
                            st.error(f"Error al guardar (el Serial '{d_sn}' ya se encuentra registrado): {ex}")

    # --------------------------------------------------------------------------
    # PESTAÑA 3: MODIFICAR / ELIMINAR DISPOSITIVO
    # --------------------------------------------------------------------------
    with tab_editar:
        st.subheader("Modificar o Eliminar Dispositivo Existente")

        df_disp_ed = run_query("""
            SELECT d.sn, d.nombre, d.empacadora_id, d.rol, d.tipo_conexion, d.ip_address, d.port,
                   e.codigo AS cod_emp, e.nombre AS empacadora
            FROM dispositivos d
            JOIN empacadoras e ON d.empacadora_id = e.id
            ORDER BY e.codigo ASC, d.nombre ASC;
        """, fetch=True)

        if df_haciendas.empty or df_disp_ed.empty:
            st.info("No hay dispositivos registrados para editar.")
        else:
            mapa_hac = {f"[{r['codigo']}] {r['nombre']}": int(r["id"]) for _, r in df_haciendas.iterrows()}
            labels_hac = list(mapa_hac.keys())

            opciones_disp = {f"SN: {r['sn']} - {r['nombre']} ([{r['cod_emp']}] {r['empacadora']})": r for _, r in df_disp_ed.iterrows()}
            sel_disp_label = st.selectbox("Seleccione el Dispositivo a gestionar:", list(opciones_disp.keys()))
            disp_actual = opciones_disp[sel_disp_label]

            idx_hac_actual = 0
            for i, lbl in enumerate(labels_hac):
                if mapa_hac[lbl] == disp_actual["empacadora_id"]:
                    idx_hac_actual = i
                    break

            areas_validas = ["COMEDOR", "EMPAQUE", "PALETIZADO", "OFICINA", "GARITA"]
            idx_rol = areas_validas.index(disp_actual["rol"]) if disp_actual["rol"] in areas_validas else 0
            idx_tipo = ["PUSH_ADMS", "PULL_SDK"].index(disp_actual["tipo_conexion"]) if disp_actual["tipo_conexion"] in ["PUSH_ADMS", "PULL_SDK"] else 0

            with st.form("form_editar_dispositivo_clean"):
                col1, col2 = st.columns(2)
                with col1:
                    ed_hac_label = st.selectbox("Hacienda Empacadora *", labels_hac, index=idx_hac_actual)
                    ed_sn = st.text_input("Número de Serie (SN) *", value=disp_actual["sn"])
                    ed_nombre = st.text_input("Nombre / Alias Descriptivo del Reloj *", value=disp_actual["nombre"])
                    ed_rol = st.selectbox("Área Física de Control *", areas_validas, index=idx_rol)
                with col2:
                    ed_tipo = st.selectbox("Tipo de Conexión *", ["PUSH_ADMS", "PULL_SDK"], index=idx_tipo)
                    ed_ip = st.text_input("Dirección IP", value=disp_actual["ip_address"] or "")
                    ed_port = st.number_input("Puerto", min_value=1, max_value=65535, value=int(disp_actual["port"] or 4370))

                st.divider()
                db1, db2 = st.columns(2)
                btn_d_upd = db1.form_submit_button("Actualizar Cambios", type="primary", use_container_width=True)
                btn_d_del = db2.form_submit_button("🗑️ Eliminar Dispositivo", use_container_width=True)

                if btn_d_upd:
                    if not ed_sn.strip() or not ed_nombre.strip():
                        st.error("Serial (SN) y Nombre / Alias son obligatorios.")
                    else:
                        try:
                            run_query("""
                                UPDATE dispositivos 
                                SET empacadora_id=%s, nombre=%s, rol=%s, tipo_conexion=%s, ip_address=%s, port=%s
                                WHERE sn=%s;
                            """, (
                                mapa_hac[ed_hac_label],
                                ed_nombre.strip(),
                                ed_rol,
                                ed_tipo,
                                ed_ip.strip() if ed_ip else None,
                                int(ed_port),
                                disp_actual["sn"]
                            ))
                            st.success("¡Dispositivo actualizado correctamente!")
                            st.rerun()
                        except Exception as ex:
                            st.error(f"Error al actualizar: {ex}")

                if btn_d_del:
                    try:
                        run_query("DELETE FROM dispositivos WHERE sn=%s;", (disp_actual["sn"],))
                        st.success("¡Dispositivo eliminado correctamente!")
                        st.rerun()
                    except Exception as ex:
                        st.error(f"No se puede eliminar este dispositivo porque tiene marcaciones asociadas: {ex}")

    # --------------------------------------------------------------------------
    # PESTAÑA 4: SINCRONIZACIÓN RED (ORDENADA EN ACCIÓN DIRECTA + SUB-PESTAÑAS)
    # --------------------------------------------------------------------------
    with tab_sync:
        st.subheader("🔄 Sincronización Bidireccional por Red Local (PULL)")

        df_pull = run_query("""
            SELECT d.sn, d.nombre, d.ip_address, d.port, d.empacadora_id, d.rol AS area_fisica,
                   e.codigo AS cod_emp, e.nombre AS emp_nombre
            FROM dispositivos d
            JOIN empacadoras e ON d.empacadora_id = e.id
            WHERE d.ip_address IS NOT NULL AND TRIM(d.ip_address) <> ''
            ORDER BY e.codigo ASC, d.nombre ASC;
        """, fetch=True)

        if not df_pull.empty:
            pull_dict = {
                f"{r['nombre']} (Área: {r['area_fisica']}) - [{r['cod_emp']}] {r['emp_nombre']} ({r['ip_address']}:{r['port']})": (
                    r["sn"],
                    r["ip_address"].strip(),
                    int(r["port"] or 4370),
                    int(r["empacadora_id"]),
                    r["area_fisica"],
                )
                for _, r in df_pull.iterrows()
            }
            sel_pull_label = st.selectbox("Seleccione el Reloj Biométrico objetivo:", list(pull_dict.keys()))
            sn_target, ip_target, port_target, hac_id_target, area_fisica_target = pull_dict[sel_pull_label]

            col_s1, col_s2 = st.columns(2)

            # ACCIÓN DIRECTA 1: DESCARGAR MARCACIONES DESDE EL RELOJ HACIA LA BD
            with col_s1:
                st.markdown("#### 📥 Descargar Asistencias")
                st.caption("Extrae las nuevas marcaciones del reloj físico y guárdalas en PostgreSQL.")
                if st.button("🚀 Descargar Marcaciones Ahora", type="primary"):
                    try:
                        from zk import ZK
                        with st.spinner(f"Conectando al reloj en {ip_target}:{port_target}..."):
                            zk = ZK(ip_target, port=port_target, timeout=5)
                            conn_zk = zk.connect()
                            attendance = conn_zk.get_attendance()
                            conn_zk.disconnect()

                        if attendance:
                            nuevos_count = 0
                            conn_db = get_conn()
                            cur_db = conn_db.cursor()
                            for att in attendance:
                                pin_emp = _normalizar_pin_zk(str(att.user_id))
                                f_hora = att.timestamp.strftime("%Y-%m-%d %H:%M:%S")
                                status = int(att.status) if hasattr(att, "status") else 1

                                try:
                                    cur_db.execute("""
                                        INSERT INTO marcaciones (pin_empleado, fecha_hora, sn_dispositivo, estado_verificacion)
                                        VALUES (%s, %s, %s, %s)
                                        ON CONFLICT ON CONSTRAINT uq_marcacion_unica DO NOTHING;
                                    """, (pin_emp, f_hora, sn_target, status))
                                    if cur_db.rowcount > 0:
                                        nuevos_count += 1
                                except Exception:
                                    pass
                            conn_db.commit()
                            cur_db.close()
                            conn_db.close()
                            st.success(f"¡Sincronización exitosa! Se guardaron {nuevos_count} marcaciones nuevas.")
                        else:
                            st.info("El reloj respondió, pero no devolvió registros nuevos.")
                    except Exception as ex:
                        st.error(f"Error al conectar con el reloj: {ex}")

            # ACCIÓN DIRECTA 2: SUBIR EMPLEADOS AL RELOJ SELECCIONADO SEGÚN HACIENDA Y ÁREA
            with col_s2:
                st.markdown("#### 📤 Enviar Empleados al Reloj Seleccionado")
                st.caption(f"Sube colaboradores autorizados en esta Hacienda y Área Física: **{area_fisica_target}**.")

                df_emps_reloj = run_query("""
                    SELECT DISTINCT e.pin, e.nombres
                    FROM empleados e
                    JOIN empleado_haciendas eh ON e.pin = eh.pin_empleado
                    WHERE eh.empacadora_id = %s
                      AND eh.area_autorizada = %s
                      AND e.activo = TRUE
                    ORDER BY e.nombres ASC;
                """, (hac_id_target, area_fisica_target), fetch=True)

                st.info(f"💡 Empleados autorizados para **{area_fisica_target}** en esta hacienda: **{len(df_emps_reloj)}**")

                if st.button("👥 Enviar Base de Empleados al Biométrico", type="secondary"):
                    if df_emps_reloj.empty:
                        st.warning(f"No hay empleados activos autorizados para esta hacienda y el área '{area_fisica_target}'.")
                    else:
                        try:
                            with st.spinner(f"Enviando {len(df_emps_reloj)} empleados al reloj {ip_target}:{port_target}..."):
                                enviados, errores = enviar_empleados_a_reloj(ip_target, port_target, df_emps_reloj)
                            if errores:
                                st.warning(f"Se enviaron {enviados} empleados con {len(errores)} incidencia(s): {' | '.join(errores[:5])}")
                            else:
                                st.success(f"¡Sincronización completa! Se grabaron {enviados} empleados en el biométrico.")
                        except Exception as ex:
                            st.error(f"Error al conectar con el dispositivo: {ex}")

            st.divider()

            sub_hac, sub_emp, sub_usr, sub_marc = st.tabs([
                "🏡 Por Hacienda",
                "🎯 Por Colaborador",
                "🛠️ Usuarios del Reloj",
                "📊 Marcaciones Recientes"
            ])

            with sub_hac:
                st.markdown("#### 🏡 Sincronizar Biométricos por Localidad / Hacienda → Servidor")
                st.caption(
                    "Seleccione la localidad (hacienda) y marque con el checkbox los biométricos que desea sincronizar "
                    "(importa colaboradores nuevos, acumula permisos de área/hacienda y descarga marcaciones sin duplicar)."
                )

                loc_opts = {}
                for _, r_hac in df_haciendas.iterrows():
                    lbl_h = f"[{r_hac['codigo']}] {r_hac['nombre']} ({r_hac['compania']})"
                    loc_opts[lbl_h] = int(r_hac["id"])

                sel_loc_label = st.selectbox("Localidad / Hacienda Empacadora:", list(loc_opts.keys()), key="sel_loc_sync_chk")
                loc_id_sel = loc_opts[sel_loc_label]

                df_devs_loc = df_pull[df_pull["empacadora_id"] == loc_id_sel]

                if df_devs_loc.empty:
                    st.info("Esta localidad no tiene biométricos con IP configurada.")
                else:
                    sns_marcados = []
                    for idx_d, r_d in df_devs_loc.iterrows():
                        sn_d = str(r_d["sn"]).strip()
                        chk_val = st.checkbox(
                            f"**{r_d['nombre']}** — Área: `{r_d['area_fisica']}` | IP: `{r_d['ip_address']}:{r_d['port']}` (SN: `{sn_d}`)",
                            value=True,
                            key=f"chk_sync_loc_{loc_id_sel}_{sn_d}_{idx_d}",
                        )
                        if chk_val:
                            sns_marcados.append(sn_d)

                    solo_cedulas_loc = st.checkbox(
                        "Ignorar IDs cortos de prueba (1..999) y sincronizar automáticamente solo cédulas reales (9-10 dígitos)",
                        value=True,
                        key=f"chk_solo_ced_loc_{loc_id_sel}",
                    )

                    if st.button("🔄 Sincronizar Biométricos Marcados de Esta Localidad", type="primary", key=f"btn_sync_loc_{loc_id_sel}"):
                        if not sns_marcados:
                            st.warning("Marque al menos un biométrico con el checkbox para sincronizar.")
                        else:
                            try:
                                with st.spinner(f"Sincronizando {len(sns_marcados)} biométrico(s) de la localidad..."):
                                    rep = sincronizar_dispositivos_seleccionados(
                                        sns_seleccionados=sns_marcados,
                                        solo_cedulas_reales=solo_cedulas_loc,
                                    )

                                c1, c2, c3, c4 = st.columns(4)
                                c1.metric("Biométricos OK", f"{rep['dispositivos_ok']} / {rep['total_dispositivos']}")
                                c2.metric("Empleados Nuevos", rep["empleados_nuevos"])
                                c3.metric("Permisos Área/Hac. Nuevos", rep["permisos_agregados"])
                                c4.metric("Marcaciones Nuevas", rep["marcaciones_nuevas"])

                                if rep["ids_cortos_omitidos"] > 0:
                                    st.info(f"ℹ️ Se omitieron {rep['ids_cortos_omitidos']} registros con IDs cortos antiguos.")

                                if rep["detalle_dispositivos"]:
                                    st.dataframe(pd.DataFrame(rep["detalle_dispositivos"]), use_container_width=True, hide_index=True)
                            except Exception as ex_loc:
                                st.error(f"Error al sincronizar los biométricos seleccionados: {ex_loc}")

            with sub_emp:
                st.markdown("#### 🎯 Sincronizar Colaborador Específico con Sus Biométricos Autorizados")
                st.caption("Envía automáticamente a un colaborador a **todos** los relojes biométricos que coincidan con sus permisos de Hacienda + Área.")

                df_emps_activos = run_query("""
                    SELECT e.pin, e.nombres,
                           STRING_AGG(CONCAT('[', emp.codigo, '] ', emp.nombre, ' (', eh.area_autorizada, ')'), ', ') AS permisos
                    FROM empleados e
                    JOIN empleado_haciendas eh ON e.pin = eh.pin_empleado
                    JOIN empacadoras emp ON eh.empacadora_id = emp.id
                    WHERE e.activo = TRUE
                    GROUP BY e.pin, e.nombres
                    ORDER BY e.nombres ASC;
                """, fetch=True)

                if df_emps_activos.empty:
                    st.info("No hay empleados activos con permisos Hacienda + Área asignados.")
                else:
                    emp_sync_map = {
                        f"{r['nombres']} [PIN: {r['pin']}] -> {r['permisos']}": (str(r["pin"]), str(r["nombres"]))
                        for _, r in df_emps_activos.iterrows()
                    }
                    sel_emp_sync = st.selectbox("Seleccione el colaborador a sincronizar:", list(emp_sync_map.keys()))
                    pin_sync, nom_sync = emp_sync_map[sel_emp_sync]

                    df_relojes_emp = run_query("""
                        SELECT DISTINCT d.sn, d.nombre, d.ip_address, d.port, d.rol AS area_fisica,
                                        emp.codigo AS cod_emp, emp.nombre AS emp_nombre
                        FROM empleado_haciendas eh
                        JOIN dispositivos d
                          ON d.empacadora_id = eh.empacadora_id
                         AND d.rol = eh.area_autorizada
                        JOIN empacadoras emp ON d.empacadora_id = emp.id
                        WHERE eh.pin_empleado = %s
                          AND d.ip_address IS NOT NULL
                          AND TRIM(d.ip_address) <> '';
                    """, (pin_sync,), fetch=True)

                    if df_relojes_emp.empty:
                        st.warning("⚠️ Este colaborador no tiene relojes biométricos con IP configurada en sus áreas autorizadas.")
                    else:
                        st.caption(f"Relojes destino detectados ({len(df_relojes_emp)}): " + ", ".join(
                            [f"{r['nombre']} [{r['cod_emp']}/{r['area_fisica']}] ({r['ip_address']})" for _, r in df_relojes_emp.iterrows()]
                        ))

                        if st.button("📡 Sincronizar Colaborador en Todos Sus Biométricos", type="primary"):
                            uno_df = pd.DataFrame([{"pin": pin_sync, "nombres": nom_sync}])
                            ok_relojes = []
                            falla_relojes = []

                            with st.spinner(f"Sincronizando a {nom_sync} en {len(df_relojes_emp)} biométrico(s)..."):
                                for _, r_dev in df_relojes_emp.iterrows():
                                    dev_label = f"{r_dev['nombre']} ({r_dev['ip_address']}:{r_dev['port']})"
                                    try:
                                        env, errs = enviar_empleados_a_reloj(r_dev["ip_address"].strip(), int(r_dev["port"] or 4370), uno_df)
                                        if env > 0 and not errs:
                                            ok_relojes.append(dev_label)
                                        else:
                                            falla_relojes.append(f"{dev_label}: {', '.join(errs) if errs else 'sin confirmación'}")
                                    except Exception as dev_ex:
                                        falla_relojes.append(f"{dev_label}: {dev_ex}")

                            for ok_lbl in ok_relojes:
                                st.success(f"✅ Sincronizado en **{ok_lbl}**")
                            for fl_lbl in falla_relojes:
                                st.error(f"❌ Error en **{fl_lbl}**")

            with sub_usr:
                st.markdown("#### 🛠️ Consultar Usuarios del Biométrico y Modificar Datos (Sin Alterar Marcaciones)")
                st.caption(
                    f"Lee los usuarios almacenados en **{sn_target}** ({ip_target}:{port_target}). "
                    "Marque el checkbox del usuario que desea modificar para actualizar sus datos sin tocar marcaciones."
                )

                btn_leer_zk = st.button("🔎 Consultar Base de Usuarios del Biométrico", key=f"btn_leer_zk_{sn_target}")

                if btn_leer_zk:
                    try:
                        from zk import ZK
                        with st.spinner(f"Leyendo usuarios desde {ip_target}:{port_target}..."):
                            zk_read = ZK(ip_target, port=port_target, timeout=5)
                            conn_read = zk_read.connect()
                            try:
                                raw_users = conn_read.get_users() or []
                            finally:
                                conn_read.disconnect()

                        usuarios_leidos = []
                        for u in raw_users:
                            usuarios_leidos.append({
                                "uid": int(getattr(u, "uid", 0) or 0),
                                "user_id": str(getattr(u, "user_id", "") or "").strip(),
                                "name": str(getattr(u, "name", "") or "").strip(),
                                "privilege": int(getattr(u, "privilege", 0) or 0),
                            })

                        st.session_state[f"zk_users_{sn_target}"] = usuarios_leidos
                        if not usuarios_leidos:
                            st.info("El biométrico no devolvió usuarios registrados.")
                    except Exception as ex_read:
                        st.error(f"No se pudo consultar la base de usuarios del biométrico: {ex_read}")

                usuarios_cache = st.session_state.get(f"zk_users_{sn_target}", [])

                if usuarios_cache:
                    st.markdown("**Seleccione con el checkbox los usuarios a modificar:**")
                    seleccionados_mod = []

                    for idx, u_item in enumerate(usuarios_cache):
                        u_uid = u_item["uid"]
                        u_pin = u_item["user_id"]
                        u_nom = u_item["name"]

                        col_chk, col_info = st.columns([1, 5])
                        with col_chk:
                            marcado = st.checkbox(
                                "Editar",
                                key=f"chk_zk_user_{sn_target}_{u_uid}_{idx}",
                            )
                        with col_info:
                            st.markdown(f"**UID:** `{u_uid}` | **ID / PIN en Reloj:** `{u_pin or 'Sin ID'}` | **Nombre actual:** `{u_nom or 'Sin Nombre'}`")

                        if marcado:
                            c_ed1, c_ed2, c_ed3 = st.columns([1, 2, 2])
                            with c_ed1:
                                nuevo_uid = st.number_input(
                                    "UID (1..65535)",
                                    min_value=1,
                                    max_value=65535,
                                    value=u_uid if 1 <= u_uid <= 65535 else 10,
                                    key=f"inp_uid_{sn_target}_{u_uid}_{idx}"
                                )
                            with c_ed2:
                                nuevo_pin = st.text_input(
                                    "Nuevo ID / PIN (Cédula)",
                                    value=u_pin,
                                    key=f"inp_pin_{sn_target}_{u_uid}_{idx}"
                                )
                            with c_ed3:
                                nuevo_nombre = st.text_input(
                                    "Nuevo Nombre",
                                    value=u_nom,
                                    key=f"inp_nom_{sn_target}_{u_uid}_{idx}"
                                )

                            sincronizar_bd_emp = st.checkbox(
                                f"Guardar / actualizar también en base de datos del servidor (Hacienda ID {hac_id_target} / Área {area_fisica_target})",
                                value=True,
                                key=f"chk_sync_bd_{sn_target}_{u_uid}_{idx}"
                            )

                            seleccionados_mod.append({
                                "uid_original": u_uid,
                                "pin_original": u_pin,
                                "nuevo_uid": int(nuevo_uid),
                                "nuevo_pin": nuevo_pin.strip(),
                                "nuevo_nombre": nuevo_nombre.strip()[:24],
                                "sync_bd": sincronizar_bd_emp,
                            })

                    if seleccionados_mod:
                        if st.button("💾 Guardar Cambios en Biométrico y BD (Sin Tocar Marcaciones)", type="primary", key=f"btn_save_zk_users_{sn_target}"):
                            from zk import ZK

                            ok_updates = []
                            err_updates = []

                            try:
                                zk_w = ZK(ip_target, port=port_target, timeout=5)
                                conn_w = zk_w.connect()
                                try:
                                    for item in seleccionados_mod:
                                        if not item["nuevo_pin"] or not item["nuevo_nombre"]:
                                            err_updates.append(f"UID {item['uid_original']}: PIN y Nombre son obligatorios.")
                                            continue
                                        try:
                                            conn_w.set_user(
                                                uid=item["nuevo_uid"],
                                                name=item["nuevo_nombre"],
                                                privilege=0,
                                                password="",
                                                card=0,
                                                user_id=item["nuevo_pin"],
                                            )
                                            ok_updates.append(f"ID {item['nuevo_pin']} ({item['nuevo_nombre']})")
                                        except Exception as ex_set:
                                            err_updates.append(f"ID {item['nuevo_pin']}: {ex_set}")
                                finally:
                                    conn_w.disconnect()
                            except Exception as ex_conn:
                                err_updates.append(f"Conexión al reloj: {ex_conn}")

                            for item in seleccionados_mod:
                                if item["sync_bd"] and item["nuevo_pin"] and item["nuevo_nombre"]:
                                    try:
                                        conn_db = get_conn()
                                        cur_db = conn_db.cursor()
                                        cur_db.execute("""
                                            INSERT INTO empleados (pin, cedula, nombres, empacadora_id, area, activo)
                                            VALUES (%s, %s, %s, %s, %s, TRUE)
                                            ON CONFLICT (pin) DO UPDATE SET
                                                cedula = EXCLUDED.cedula,
                                                nombres = EXCLUDED.nombres,
                                                empacadora_id = EXCLUDED.empacadora_id,
                                                area = EXCLUDED.area,
                                                activo = TRUE;
                                        """, (
                                            item["nuevo_pin"],
                                            item["nuevo_pin"],
                                            item["nuevo_nombre"].upper(),
                                            hac_id_target,
                                            area_fisica_target,
                                        ))
                                        cur_db.execute("""
                                            INSERT INTO empleado_haciendas (pin_empleado, empacadora_id, area_autorizada)
                                            VALUES (%s, %s, %s)
                                            ON CONFLICT DO NOTHING;
                                        """, (item["nuevo_pin"], hac_id_target, area_fisica_target))
                                        conn_db.commit()
                                        cur_db.close()
                                        conn_db.close()
                                    except Exception as ex_db:
                                        err_updates.append(f"BD empleado {item['nuevo_pin']}: {ex_db}")

                            for ok_msg in ok_updates:
                                st.success(f"✅ Guardado y actualizado: **{ok_msg}** (marcaciones intactas)")
                            for err_msg in err_updates:
                                st.error(f"❌ {err_msg}")

                            if ok_updates:
                                st.session_state.pop(f"zk_users_{sn_target}", None)

            with sub_marc:
                st.markdown("#### 📊 Visor Rápido de Marcaciones Recientes en Base de Datos")
                df_ultimas = run_query("""
                    SELECT m.pin_empleado AS pin, 
                           COALESCE(e.nombres, 'Desconocido / Sin Registro') AS empleado, 
                           m.fecha_hora, 
                           d.nombre AS reloj, 
                           emp.nombre AS hacienda
                    FROM marcaciones m
                    LEFT JOIN empleados e
                      ON LTRIM(m.pin_empleado, '0') = LTRIM(e.pin, '0')
                    LEFT JOIN dispositivos d ON m.sn_dispositivo = d.sn
                    LEFT JOIN empacadoras emp ON d.empacadora_id = emp.id
                    ORDER BY m.fecha_hora DESC
                    LIMIT 30;
                """, fetch=True)

                if not df_ultimas.empty:
                    st.dataframe(df_ultimas, use_container_width=True, hide_index=True, column_config={
                        "pin": "PIN / Cédula",
                        "empleado": "Colaborador",
                        "fecha_hora": "Fecha y Hora",
                        "reloj": "Dispositivo Origen",
                        "hacienda": "Hacienda Empacadora"
                    })
                else:
                    st.info("Aún no se registran marcaciones en la base de datos.")
        else:
            st.info("No hay dispositivos con Dirección IP configurada para red local.")

    # --------------------------------------------------------------------------
    # PESTAÑA 5: ESCÁNER DE RED (IP / VLAN PERSONALIZADO + EXTRACCIÓN SN)
    # --------------------------------------------------------------------------
    with tab_escaner:
        st.subheader("🔍 Escáner de Red Local para Biométricos ZKTeco")
        st.markdown("""
        Ideal para tus diferentes segmentos de red y VLANs (Ej: **172.16.5.x** para Guayaquil, **192.168.30.x** para Edmita, **172.16.4.x** para Guisella_1).
        Indique el prefijo de red y el sistema escaneará las IPs de la `.1` a la `.254` buscando dispositivos activos en el puerto **4370** e intentará leer su **Número de Serie (SN)**.
        """)

        col_e1, col_e2 = st.columns([2, 1])
        with col_e1:
            segmento_input = st.text_input(
                "Prefijo de Red / Segmento IP *",
                value="192.168.30",
                placeholder="Ej: 172.16.4 o 192.168.30 o 172.16.5"
            )
        with col_e2:
            st.write("")
            st.write("")
            btn_escanear = st.button("🚀 Iniciar Escáner IP", type="primary", use_container_width=True)

        if btn_escanear:
            if not segmento_input.strip():
                st.error("Por favor ingrese un segmento de red válido (Ej: 192.168.30).")
            else:
                prefix = segmento_input.strip().rstrip(".")
                st.info(f"Escaneando el segmento `{prefix}.1` al `{prefix}.254` en el puerto 4370...")

                ips_a_probar = [f"{prefix}.{i}" for i in range(1, 255)]
                ips_activas = []

                progress_bar = st.progress(0)
                status_text = st.empty()

                completed_count = 0
                with ThreadPoolExecutor(max_workers=50) as executor:
                    futures = {executor.submit(probar_ip_puerto, ip): ip for ip in ips_a_probar}
                    for future in as_completed(futures):
                        completed_count += 1
                        if completed_count % 10 == 0:
                            progress_bar.progress(completed_count / 254)
                            status_text.text(f"Analizando IPs... ({completed_count}/254)")

                        result_ip = future.result()
                        if result_ip:
                            ips_activas.append(result_ip)

                ips_activas.sort(key=lambda x: int(x.split(".")[-1]))
                hallazgos = []

                if ips_activas:
                    status_text.text(f"Consultando Número de Serie en {len(ips_activas)} equipo(s)...")
                    for ip_ok in ips_activas:
                        sn_detectado = obtener_sn_zk(ip_ok, port=4370, timeout=3)
                        hallazgos.append({
                            "Dirección IP Activa": ip_ok,
                            "Puerto": 4370,
                            "Número de Serie (SN)": sn_detectado
                        })

                progress_bar.progress(1.0)
                status_text.text("¡Escaneo completado!")

                if hallazgos:
                    st.success(f"¡Se encontraron {len(hallazgos)} dispositivo(s) activo(s) respondiendo en el segmento `{prefix}.x`!")

                    df_hallazgos = pd.DataFrame(hallazgos)
                    st.dataframe(df_hallazgos, use_container_width=True, hide_index=True)

                    st.info("💡 Copie el Número de Serie (SN) y la dirección IP detectada para registrar o actualizar el reloj en 'Registrar Dispositivo' o 'Modificar / Eliminar'.")
                else:
                    st.warning(f"No se detectaron dispositivos ZKTeco activos en el segmento `{prefix}.x` (verifique que el reloj esté encendido y accesible en la red o VPN).")