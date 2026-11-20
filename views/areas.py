# -*- coding: utf-8 -*-
import streamlit as st
import pandas as pd
from db import get_conn

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

def render():
    st.title("📂 Gestión de Áreas y Departamentos")
    st.write("Administración de departamentos operativos por hacienda empacadora de forma dinámica.")

    # Consultar empacadoras activas de forma dinámica para los selectores
    df_haciendas = run_query("""
        SELECT e.id, e.codigo, e.nombre, c.nombre AS compania
        FROM empacadoras e
        JOIN companias c ON e.compania_id = c.id
        WHERE e.activa = TRUE
        ORDER BY c.nombre ASC, e.codigo ASC;
    """, fetch=True)

    tab_lista, tab_crear, tab_editar = st.tabs(["📋 Listado y Filtros", "➕ Registrar Nuevo", "✏️ Modificar / Eliminar"])

    # --------------------------------------------------------------------------
    # PESTAÑA 1: LISTADO Y FILTROS
    # --------------------------------------------------------------------------
    with tab_lista:
        st.subheader("Departamentos Registrados")

        if df_haciendas.empty:
            st.info("No hay haciendas empacadoras activas registradas.")
        else:
            filtro_hac_opts = {"Todas las haciendas": None}
            for _, r in df_haciendas.iterrows():
                filtro_hac_opts[f"[{r['codigo']}] {r['nombre']} ({r['compania']})"] = int(r["id"])

            filtro_sel = st.selectbox("Filtrar por Hacienda Empacadora:", list(filtro_hac_opts.keys()))
            hac_filtro_id = filtro_hac_opts[filtro_sel]

            if hac_filtro_id:
                df_deptos = run_query("""
                    SELECT d.id, c.nombre AS compania, e.codigo AS cod_empacadora, e.nombre AS empacadora,
                           d.empacadora_id, d.nombre AS departamento, d.descripcion, d.activo
                    FROM departamentos d
                    JOIN empacadoras e ON d.empacadora_id = e.id
                    JOIN companias c ON e.compania_id = c.id
                    WHERE d.empacadora_id = %s
                    ORDER BY d.nombre ASC;
                """, (hac_filtro_id,), fetch=True)
            else:
                df_deptos = run_query("""
                    SELECT d.id, c.nombre AS compania, e.codigo AS cod_empacadora, e.nombre AS empacadora,
                           d.empacadora_id, d.nombre AS departamento, d.descripcion, d.activo
                    FROM departamentos d
                    JOIN empacadoras e ON d.empacadora_id = e.id
                    JOIN companias c ON e.compania_id = c.id
                    ORDER BY c.nombre ASC, e.nombre ASC, d.nombre ASC;
                """, fetch=True)

            if df_deptos.empty:
                st.info("No hay departamentos registrados para el filtro seleccionado.")
            else:
                st.dataframe(
                    df_deptos.drop(columns=["empacadora_id"], errors="ignore"),
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "id": "ID",
                        "compania": "Compañía Matriz",
                        "cod_empacadora": "Cód. Hacienda",
                        "empacadora": "Hacienda Empacadora",
                        "departamento": "Departamento / Área",
                        "descripcion": "Descripción",
                        "activo": "Activo"
                    }
                )

    # --------------------------------------------------------------------------
    # PESTAÑA 2: REGISTRAR NUEVO DEPARTAMENTO
    # --------------------------------------------------------------------------
    with tab_crear:
        st.subheader("Registrar Nuevo Departamento / Área")

        if df_haciendas.empty:
            st.warning("⚠️ Debe registrar al menos una hacienda empacadora antes de crear departamentos.")
        else:
            mapa_hac = {f"[{r['codigo']}] {r['nombre']} ({r['compania']})": int(r["id"]) for _, r in df_haciendas.iterrows()}
            labels_hac = list(mapa_hac.keys())

            with st.form("form_crear_depto_clean", clear_on_submit=True):
                col1, col2 = st.columns(2)
                with col1:
                    d_hac_label = st.selectbox("Hacienda Empacadora *", labels_hac)
                    d_nombre = st.text_input("Nombre del Departamento / Área *", placeholder="Ej: Empaque, Calidad, Campo")
                with col2:
                    d_desc = st.text_input("Descripción", placeholder="Ej: Personal asignado al proceso de empaque de banano")
                    d_activo = st.checkbox("Departamento Activo", value=True)

                st.divider()
                if st.form_submit_button("Guardar Departamento", type="primary", use_container_width=True):
                    if not d_nombre.strip():
                        st.error("El nombre del departamento es obligatorio.")
                    else:
                        try:
                            run_query("""
                                INSERT INTO departamentos (empacadora_id, nombre, descripcion, activo)
                                VALUES (%s, %s, %s, %s);
                            """, (
                                mapa_hac[d_hac_label],
                                d_nombre.strip(),
                                d_desc.strip(),
                                d_activo
                            ))
                            st.success(f"¡Departamento '{d_nombre}' registrado con éxito!")
                        except Exception as e:
                            st.error(f"Error al guardar (el departamento ya puede existir en esta hacienda): {e}")

    # --------------------------------------------------------------------------
    # PESTAÑA 3: MODIFICAR / ELIMINAR DEPARTAMENTO
    # --------------------------------------------------------------------------
    with tab_editar:
        st.subheader("Modificar o Eliminar Departamento Existente")

        df_deptos_ed = run_query("""
            SELECT d.id, e.codigo AS cod_empacadora, e.nombre AS empacadora,
                   d.empacadora_id, d.nombre AS departamento, d.descripcion, d.activo
            FROM departamentos d
            JOIN empacadoras e ON d.empacadora_id = e.id
            ORDER BY e.codigo ASC, d.nombre ASC;
        """, fetch=True)

        if df_haciendas.empty or df_deptos_ed.empty:
            st.info("No hay departamentos registrados para editar.")
        else:
            mapa_hac = {f"[{r['codigo']}] {r['nombre']}": int(r["id"]) for _, r in df_haciendas.iterrows()}
            labels_hac = list(mapa_hac.keys())

            opciones_deptos = {f"[{r['cod_empacadora']}] {r['empacadora']} ➔ {r['departamento']}": r for _, r in df_deptos_ed.iterrows()}
            sel_depto_label = st.selectbox("Seleccione el Departamento a gestionar:", list(opciones_deptos.keys()))
            depto_actual = opciones_deptos[sel_depto_label]

            idx_hac_actual = 0
            for i, lbl in enumerate(labels_hac):
                if mapa_hac[lbl] == depto_actual["empacadora_id"]:
                    idx_hac_actual = i
                    break

            with st.form("form_editar_depto_clean"):
                col1, col2 = st.columns(2)
                with col1:
                    ed_hac_label = st.selectbox("Hacienda Empacadora *", labels_hac, index=idx_hac_actual)
                    ed_nombre = st.text_input("Nombre del Departamento *", value=depto_actual["departamento"])
                with col2:
                    ed_desc = st.text_input("Descripción", value=depto_actual["descripcion"] or "")
                    ed_activo = st.checkbox("Departamento Activo", value=bool(depto_actual["activo"]))

                st.divider()
                db1, db2 = st.columns(2)
                btn_d_upd = db1.form_submit_button("Actualizar Cambios", type="primary", use_container_width=True)
                btn_d_del = db2.form_submit_button("🗑️ Eliminar Departamento", use_container_width=True)

                if btn_d_upd:
                    if not ed_nombre.strip():
                        st.error("El nombre del departamento es obligatorio.")
                    else:
                        try:
                            run_query("""
                                UPDATE departamentos 
                                SET empacadora_id=%s, nombre=%s, descripcion=%s, activo=%s
                                WHERE id=%s;
                            """, (
                                mapa_hac[ed_hac_label],
                                ed_nombre.strip(),
                                ed_desc.strip(),
                                ed_activo,
                                int(depto_actual["id"])
                            ))
                            st.success("¡Departamento actualizado correctamente!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error al actualizar: {e}")

                if btn_d_del:
                    try:
                        run_query("DELETE FROM departamentos WHERE id=%s;", (int(depto_actual["id"]),))
                        st.success("¡Departamento eliminado correctamente!")
                        st.rerun()
                    except Exception:
                        st.error("No se puede eliminar este departamento porque tiene empleados asignados.")