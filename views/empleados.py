# -*- coding: utf-8 -*-
import streamlit as st
import pandas as pd
from db import get_conn
from main import encolar_comando_usuario_adms


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


def get_areas_biometricos(empacadora_id):
    """Devuelve las áreas físicas (rol) que tienen al menos un biométrico configurado en la hacienda."""
    df_disp = run_query("""
        SELECT DISTINCT rol
        FROM dispositivos
        WHERE empacadora_id = %s
          AND rol IS NOT NULL
          AND TRIM(rol) <> ''
        ORDER BY rol ASC;
    """, (int(empacadora_id),), fetch=True)
    if df_disp.empty:
        return []
    return df_disp["rol"].astype(str).tolist()


def render():
    st.title("👥 Gestión de Empleados y Autorizaciones Multihacienda")
    st.write("Directorio centralizado separando la estructura organizacional (Departamento) de los permisos físicos de marcación (Áreas con Biométricos).")

    df_haciendas = run_query("""
        SELECT e.id, e.codigo, e.nombre, c.nombre AS compania
        FROM empacadoras e
        JOIN companias c ON e.compania_id = c.id
        WHERE e.activa = TRUE
        ORDER BY c.nombre ASC, e.codigo ASC;
    """, fetch=True)

    df_departamentos = run_query("""
        SELECT id, empacadora_id, nombre
        FROM departamentos
        WHERE activo = TRUE
        ORDER BY nombre ASC;
    """, fetch=True)

    tab_lista, tab_crear, tab_editar = st.tabs(["📋 Listado y Filtros", "➕ Registrar Empleado", "✏️ Modificar / Permisos"])

    # --------------------------------------------------------------------------
    # PESTAÑA 1: LISTADO Y FILTROS
    # --------------------------------------------------------------------------
    with tab_lista:
        st.subheader("Directorio de Empleados Registrados")

        if df_haciendas.empty:
            st.info("No hay haciendas empacadoras registradas en el sistema.")
        else:
            c_f1, c_f2 = st.columns(2)
            with c_f1:
                filtro_hac_opts = {"Todas las haciendas": None}
                for _, r in df_haciendas.iterrows():
                    filtro_hac_opts[f"[{r['codigo']}] {r['nombre']} ({r['compania']})"] = int(r["id"])
                filtro_sel = st.selectbox("Filtrar por Hacienda Autorizada:", list(filtro_hac_opts.keys()))
                hac_filtro_id = filtro_hac_opts[filtro_sel]

            with c_f2:
                busqueda_txt = st.text_input("Buscar por Cédula / PIN o Nombre:", placeholder="Escriba para filtrar...")

            base_sql = """
                SELECT DISTINCT emp.pin AS cedula_pin, emp.nombres, 
                       COALESCE(d.nombre, 'Sin asignar') AS departamento_cargo,
                       emp.activo,
                       STRING_AGG(CONCAT('[', e_hac.codigo, '] ', e_hac.nombre, ' (', eh.area_autorizada, ')'), ', ') AS permisos_hacienda_area
                FROM empleados emp
                LEFT JOIN departamentos d ON emp.departamento_id = d.id
                LEFT JOIN empleado_haciendas eh ON emp.pin = eh.pin_empleado
                LEFT JOIN empacadoras e_hac ON eh.empacadora_id = e_hac.id
            """
            condiciones = []
            params = []

            if hac_filtro_id:
                condiciones.append("emp.pin IN (SELECT pin_empleado FROM empleado_haciendas WHERE empacadora_id = %s)")
                params.append(hac_filtro_id)

            if busqueda_txt.strip():
                condiciones.append("(emp.pin ILIKE %s OR emp.nombres ILIKE %s)")
                like_val = f"%{busqueda_txt.strip()}%"
                params.extend([like_val, like_val])

            if condiciones:
                base_sql += " WHERE " + " AND ".join(condiciones)

            base_sql += " GROUP BY emp.pin, emp.nombres, d.nombre, emp.activo ORDER BY emp.nombres ASC;"
            df_empleados = run_query(base_sql, tuple(params) if params else None, fetch=True)

            if df_empleados.empty:
                st.info("No hay empleados registrados para los criterios seleccionados.")
            else:
                st.dataframe(
                    df_empleados,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "cedula_pin": "Cédula / PIN Biométrico",
                        "nombres": "Apellidos y Nombres",
                        "departamento_cargo": "Departamento / Cargo Org.",
                        "activo": "Activo",
                        "permisos_hacienda_area": "Haciendas y Áreas Autorizadas (Con Biométrico)"
                    }
                )

    # --------------------------------------------------------------------------
    # PESTAÑA 2: REGISTRAR EMPLEADO
    # --------------------------------------------------------------------------
    with tab_crear:
        st.subheader("Registrar Nuevo Colaborador")

        if df_haciendas.empty:
            st.warning("⚠️ Debe registrar al menos una hacienda empacadora antes de añadir empleados.")
        else:
            mapa_hac = {f"[{r['codigo']}] {r['nombre']} ({r['compania']})": int(r["id"]) for _, r in df_haciendas.iterrows()}
            labels_hac = list(mapa_hac.keys())

            e_hac_base_label = st.selectbox("1. Hacienda Principal (Procedencia del Colaborador) *", labels_hac, key="crear_emp_hac_base")
            hac_base_id = mapa_hac[e_hac_base_label]

            deptos_hac = df_departamentos[df_departamentos["empacadora_id"] == hac_base_id] if not df_departamentos.empty else pd.DataFrame()
            areas_base_bio = get_areas_biometricos(hac_base_id)

            if deptos_hac.empty:
                st.warning("⚠️ Esta hacienda aún no tiene Departamentos / Cargos registrados. Créelos primero en su respectivo módulo.")
            else:
                mapa_dep = {r["nombre"]: int(r["id"]) for _, r in deptos_hac.iterrows()}
                labels_dep = list(mapa_dep.keys())

                col1, col2 = st.columns(2)
                with col1:
                    e_dep_label = st.selectbox("2. Departamento / Cargo Organizacional *", labels_dep, key="c_dep")
                    e_cedula_pin = st.text_input("Número de Cédula (PIN en los Relojes) *", max_chars=13, placeholder="Ej: 0912345678", key="c_pin")
                with col2:
                    e_nombres = st.text_input("Apellidos y Nombres *", placeholder="Ej: Zambrano Vera Carlos", key="c_nom")
                    e_activo = st.checkbox("Empleado Activo", value=True, key="c_act")

                if areas_base_bio:
                    e_area_bio_base = st.selectbox(
                        "3. Área Física del Biométrico (En su Hacienda de Registro) *",
                        areas_base_bio,
                        key=f"c_area_bio_base_{hac_base_id}"
                    )
                else:
                    e_area_bio_base = None
                    st.warning(f"⚠️ **{e_hac_base_label}** aún no tiene relojes biométricos configurados. Registre un dispositivo para asignar un área física de marcación.")

                st.divider()
                st.markdown("#### 🌴 Autorización de Marcación Adicional (Multihacienda)")
                st.caption(
                    f"El permiso principal en **{e_hac_base_label}**"
                    + (f" (**{e_area_bio_base}**)" if e_area_bio_base else "")
                    + " ya queda considerado desde el registro superior. Aquí solo agregue haciendas o áreas extra si aplica."
                )

                e_hacs_extra = st.multiselect(
                    "Haciendas Adicionales Autorizadas (Opcional):",
                    labels_hac,
                    key=f"c_hacs_extra_{hac_base_id}"
                )

                permisos_set = set()
                if e_area_bio_base:
                    permisos_set.add((hac_base_id, e_area_bio_base))

                if e_hacs_extra:
                    for hac_lbl in e_hacs_extra:
                        hac_id_iter = mapa_hac[hac_lbl]
                        areas_disponibles = get_areas_biometricos(hac_id_iter)

                        if not areas_disponibles:
                            st.warning(f"⚠️ **{hac_lbl}** no tiene relojes biométricos configurados en ningún área.")
                        else:
                            default_extra = [e_area_bio_base] if (hac_id_iter == hac_base_id and e_area_bio_base in areas_disponibles) else []
                            areas_sel = st.multiselect(
                                f"Áreas con biométricos en **{hac_lbl}**:",
                                areas_disponibles,
                                default=default_extra,
                                key=f"c_areas_extra_{hac_base_id}_{hac_id_iter}_{e_area_bio_base}"
                            )
                            for ar in areas_sel:
                                permisos_set.add((hac_id_iter, ar))

                asignaciones_crear = list(permisos_set)

                st.divider()
                if st.button("Guardar Empleado y Permisos Granulares", type="primary", use_container_width=True):
                    if not e_cedula_pin.strip() or not e_nombres.strip():
                        st.error("La Cédula/PIN y los Apellidos y Nombres son obligatorios.")
                    elif not asignaciones_crear:
                        st.error("La hacienda seleccionada no tiene áreas con biométrico configurado y no seleccionó ninguna área adicional válida.")
                    else:
                        try:
                            conn = get_conn()
                            cur = conn.cursor()

                            pin_limpio = e_cedula_pin.strip()
                            nombre_limpio = e_nombres.strip().upper()
                            area_principal_guardada = e_area_bio_base if e_area_bio_base else asignaciones_crear[0][1]

                            cur.execute("""
                                INSERT INTO empleados (pin, cedula, nombres, empacadora_id, departamento_id, area, activo)
                                VALUES (%s, %s, %s, %s, %s, %s, %s)
                                ON CONFLICT (pin) DO UPDATE SET
                                    cedula = EXCLUDED.cedula,
                                    nombres = EXCLUDED.nombres,
                                    empacadora_id = EXCLUDED.empacadora_id,
                                    departamento_id = EXCLUDED.departamento_id,
                                    area = EXCLUDED.area,
                                    activo = EXCLUDED.activo;
                            """, (
                                pin_limpio,
                                pin_limpio,
                                nombre_limpio,
                                hac_base_id,
                                mapa_dep[e_dep_label],
                                area_principal_guardada,
                                e_activo
                            ))

                            cur.execute("DELETE FROM empleado_haciendas WHERE pin_empleado = %s;", (pin_limpio,))
                            for h_id, area_auth in asignaciones_crear:
                                cur.execute("""
                                    INSERT INTO empleado_haciendas (pin_empleado, empacadora_id, area_autorizada)
                                    VALUES (%s, %s, %s)
                                    ON CONFLICT DO NOTHING;
                                """, (pin_limpio, h_id, area_auth))

                            conn.commit()
                            cur.close()
                            conn.close()

                            # Encolar comando ADMS para enviar a los dispositivos autorizados
                            cmds = encolar_comando_usuario_adms(
                                pin=pin_limpio,
                                nombres=nombre_limpio,
                                permisos_hacienda_area=asignaciones_crear,
                                activo=bool(e_activo)
                            )

                            msg_exito = f"¡Empleado '{nombre_limpio}' guardado con éxito con {len(asignaciones_crear)} permiso(s) de Hacienda + Área!"
                            if cmds > 0:
                                msg_exito += f" (Se encolaron {cmds} comando(s) hacia los relojes PUSH_ADMS)."
                            st.success(msg_exito)

                        except Exception as ex:
                            st.error(f"Error al guardar el empleado: {ex}")

    # --------------------------------------------------------------------------
    # PESTAÑA 3: MODIFICAR / PERMISOS
    # --------------------------------------------------------------------------
    with tab_editar:
        st.subheader("Modificar Datos Organizacionales o Permisos Físicos")

        df_emp_ed = run_query("""
            SELECT emp.pin, emp.nombres, emp.departamento_id, emp.area, emp.activo, emp.empacadora_id
            FROM empleados emp
            ORDER BY emp.nombres ASC;
        """, fetch=True)

        if df_haciendas.empty or df_emp_ed.empty:
            st.info("No hay empleados registrados para editar.")
        else:
            mapa_hac = {f"[{r['codigo']}] {r['nombre']}": int(r["id"]) for _, r in df_haciendas.iterrows()}
            labels_hac = list(mapa_hac.keys())

            opciones_emp = {f"Cédula/PIN: {r['pin']} - {r['nombres']}": r for _, r in df_emp_ed.iterrows()}
            sel_emp_label = st.selectbox("Seleccione el Empleado a gestionar:", list(opciones_emp.keys()), key="ed_sel_emp")
            emp_actual = opciones_emp[sel_emp_label]
            pin_target = emp_actual["pin"]

            df_auth = run_query("SELECT empacadora_id, area_autorizada FROM empleado_haciendas WHERE pin_empleado = %s;", (pin_target,), fetch=True)

            permisos_por_hac = {}
            permisos_anteriores_set = set()
            if not df_auth.empty:
                for _, row_a in df_auth.iterrows():
                    hid = int(row_a["empacadora_id"])
                    area_a = str(row_a["area_autorizada"]).strip()
                    permisos_por_hac.setdefault(hid, []).append(area_a)
                    permisos_anteriores_set.add((hid, area_a))

            default_hacs_labels = [k for k, v in mapa_hac.items() if v in permisos_por_hac]
            if not default_hacs_labels and emp_actual["empacadora_id"] in mapa_hac.values():
                default_hacs_labels = [k for k, v in mapa_hac.items() if v == emp_actual["empacadora_id"]]

            idx_base = 0
            for i, lbl in enumerate(labels_hac):
                if mapa_hac[lbl] == emp_actual["empacadora_id"]:
                    idx_base = i
                    break

            ee_hac_base_label = st.selectbox("Hacienda Principal (Procedencia) *", labels_hac, index=idx_base, key="edit_emp_hac_base")
            hac_edit_base_id = mapa_hac[ee_hac_base_label]

            deptos_edit = df_departamentos[df_departamentos["empacadora_id"] == hac_edit_base_id] if not df_departamentos.empty else pd.DataFrame()
            areas_edit_base_bio = get_areas_biometricos(hac_edit_base_id)

            if deptos_edit.empty:
                st.warning("⚠️ La hacienda seleccionada no tiene departamentos activos registrados.")
            else:
                mapa_dep_ed = {r["nombre"]: int(r["id"]) for _, r in deptos_edit.iterrows()}
                labels_dep_ed = list(mapa_dep_ed.keys())

                idx_dep_actual = 0
                for i, lbl in enumerate(labels_dep_ed):
                    if mapa_dep_ed[lbl] == emp_actual["departamento_id"]:
                        idx_dep_actual = i
                        break

                col1, col2 = st.columns(2)
                with col1:
                    ee_dep_label = st.selectbox("Departamento / Cargo *", labels_dep_ed, index=idx_dep_actual, key=f"ed_dep_{pin_target}")
                    ee_cedula_pin = st.text_input("Cédula / PIN Biométrico *", value=pin_target, key=f"ed_pin_{pin_target}")
                with col2:
                    ee_nombres = st.text_input("Apellidos y Nombres *", value=emp_actual["nombres"], key=f"ed_nom_{pin_target}")
                    ee_activo = st.checkbox("Empleado Activo", value=bool(emp_actual["activo"]), key=f"ed_act_{pin_target}")

                if areas_edit_base_bio:
                    idx_area_bio = 0
                    area_guardada = emp_actual.get("area")
                    if area_guardada in areas_edit_base_bio:
                        idx_area_bio = areas_edit_base_bio.index(area_guardada)
                    ee_area_bio_base = st.selectbox(
                        "Área Física del Biométrico (En su Hacienda de Registro) *",
                        areas_edit_base_bio,
                        index=idx_area_bio,
                        key=f"ed_area_bio_base_{pin_target}_{hac_edit_base_id}"
                    )
                else:
                    ee_area_bio_base = None
                    st.warning(f"⚠️ **{ee_hac_base_label}** no tiene relojes biométricos configurados.")

                st.divider()
                st.markdown("#### 🌴 Modificar Áreas con Biométricos Activos")
                ee_hacs = st.multiselect("Haciendas habilitadas:", labels_hac, default=default_hacs_labels, key=f"ed_hacs_{pin_target}_{hac_edit_base_id}")

                permisos_edit_set = set()
                if ee_area_bio_base:
                    permisos_edit_set.add((hac_edit_base_id, ee_area_bio_base))

                if ee_hacs:
                    for hac_lbl in ee_hacs:
                        hac_id_iter = mapa_hac[hac_lbl]
                        areas_disponibles = get_areas_biometricos(hac_id_iter)
                        prev_areas = [a for a in permisos_por_hac.get(hac_id_iter, []) if a in areas_disponibles]

                        areas_ed_sel = st.multiselect(
                            f"Áreas con biométricos en **{hac_lbl}**:",
                            areas_disponibles,
                            default=prev_areas,
                            key=f"ed_areas_{pin_target}_{hac_edit_base_id}_{hac_id_iter}"
                        )
                        for ar in areas_ed_sel:
                            permisos_edit_set.add((hac_id_iter, ar))

                asignaciones_editar = list(permisos_edit_set)

                st.divider()
                eb1, eb2 = st.columns(2)
                btn_e_upd = eb1.button("Actualizar Cambios", type="primary", use_container_width=True, key=f"btn_upd_{pin_target}")
                btn_e_del = eb2.button("🗑️ Eliminar Empleado", use_container_width=True, key=f"btn_del_{pin_target}")

                if btn_e_upd:
                    if not asignaciones_editar:
                        st.error("Debe seleccionar al menos un área con biométrico activo.")
                    else:
                        try:
                            conn = get_conn()
                            cur = conn.cursor()

                            pin_nuevo_limpio = ee_cedula_pin.strip()
                            nombre_nuevo_limpio = ee_nombres.strip().upper()
                            area_prin_edit = ee_area_bio_base if ee_area_bio_base else asignaciones_editar[0][1]

                            cur.execute("""
                                UPDATE empleados 
                                SET pin=%s, cedula=%s, nombres=%s, empacadora_id=%s, departamento_id=%s, area=%s, activo=%s
                                WHERE pin=%s;
                            """, (
                                pin_nuevo_limpio,
                                pin_nuevo_limpio,
                                nombre_nuevo_limpio,
                                hac_edit_base_id,
                                mapa_dep_ed[ee_dep_label],
                                area_prin_edit,
                                ee_activo,
                                pin_target
                            ))

                            cur.execute("DELETE FROM empleado_haciendas WHERE pin_empleado = %s;", (pin_nuevo_limpio,))
                            for h_id, area_auth in asignaciones_editar:
                                cur.execute("""
                                    INSERT INTO empleado_haciendas (pin_empleado, empacadora_id, area_autorizada)
                                    VALUES (%s, %s, %s) ON CONFLICT DO NOTHING;
                                """, (pin_nuevo_limpio, h_id, area_auth))

                            conn.commit()
                            cur.close()
                            conn.close()

                            # 1. Si cambió el PIN, dar de baja el PIN anterior en todos sus relojes previos
                            if pin_nuevo_limpio != pin_target and permisos_anteriores_set:
                                encolar_comando_usuario_adms(
                                    pin=pin_target,
                                    nombres=nombre_nuevo_limpio,
                                    permisos_hacienda_area=list(permisos_anteriores_set),
                                    activo=False
                                )
                            else:
                                # 2. Si se quitaron permisos de alguna hacienda/área, dar de baja en esos relojes
                                permisos_revocados = list(permisos_anteriores_set - permisos_edit_set)
                                if permisos_revocados:
                                    encolar_comando_usuario_adms(
                                        pin=pin_nuevo_limpio,
                                        nombres=nombre_nuevo_limpio,
                                        permisos_hacienda_area=permisos_revocados,
                                        activo=False
                                    )

                            # 3. Enviar datos actualizados (o baja si activo=False) a las áreas vigentes
                            cmds = encolar_comando_usuario_adms(
                                pin=pin_nuevo_limpio,
                                nombres=nombre_nuevo_limpio,
                                permisos_hacienda_area=asignaciones_editar,
                                activo=bool(ee_activo)
                            )

                            msg_upd = "¡Colaborador y permisos actualizados correctamente!"
                            if cmds > 0:
                                msg_upd += f" ({cmds} comando(s) encolados para relojes PUSH_ADMS)."
                            st.success(msg_upd)
                            st.rerun()

                        except Exception as ex:
                            st.error(f"Error al actualizar: {ex}")

                if btn_e_del:
                    try:
                        # Dar de baja en los biométricos antes de eliminarlo de la BD
                        if permisos_anteriores_set:
                            encolar_comando_usuario_adms(
                                pin=pin_target,
                                nombres=emp_actual["nombres"],
                                permisos_hacienda_area=list(permisos_anteriores_set),
                                activo=False
                            )

                        run_query("DELETE FROM empleados WHERE pin=%s;", (pin_target,))
                        st.success("¡Empleado eliminado y dado de baja en los relojes autorizados!")
                        st.rerun()
                    except Exception as ex:
                        st.error(f"No se puede eliminar este empleado: {ex}")