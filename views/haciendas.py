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
    st.title("🌴 Gestión de Haciendas Empacadoras")
    st.write("Administración de haciendas, ubicación geográfica, encargados y asignación por compañía.")

    df_comp_activas = run_query("SELECT id, ruc, nombre FROM companias ORDER BY nombre ASC;", fetch=True)
    
    # Pestañas limpias para ordenar la interfaz
    tab_lista, tab_crear, tab_editar = st.tabs(["📋 Listado y Filtros", "➕ Registrar Nueva", "✏️ Modificar / Eliminar"])

    # --------------------------------------------------------------------------
    # PESTAÑA 1: LISTADO GENERAL Y FILTROS
    # --------------------------------------------------------------------------
    with tab_lista:
        st.subheader("Haciendas Registradas en el Sistema")
        
        # Filtro de consulta por compañía
        filtro_opts = {"Todas las compañías": None}
        for _, r in df_comp_activas.iterrows():
            filtro_opts[f"{r['nombre']} ({r['ruc']})"] = int(r["id"])
            
        filtro_sel = st.selectbox("Filtrar listado por Compañía Matriz:", list(filtro_opts.keys()))
        filtro_id = filtro_opts[filtro_sel]

        if filtro_id:
            df_haciendas = run_query("""
                SELECT e.id, c.nombre AS compania, e.compania_id, e.codigo, e.nombre, 
                       e.ubicacion, e.correo, e.encargado, e.activa
                FROM empacadoras e
                LEFT JOIN companias c ON e.compania_id = c.id
                WHERE e.compania_id = %s
                ORDER BY e.codigo ASC;
            """, (filtro_id,), fetch=True)
        else:
            df_haciendas = run_query("""
                SELECT e.id, c.nombre AS compania, e.compania_id, e.codigo, e.nombre, 
                       e.ubicacion, e.correo, e.encargado, e.activa
                FROM empacadoras e
                LEFT JOIN companias c ON e.compania_id = c.id
                ORDER BY c.nombre ASC, e.codigo ASC;
            """, fetch=True)

        if df_haciendas.empty:
            st.info("No hay haciendas empacadoras registradas.")
        else:
            st.dataframe(
                df_haciendas.drop(columns=["compania_id"], errors="ignore"),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "id": "ID",
                    "compania": "Compañía Matriz",
                    "codigo": "Código",
                    "nombre": "Nombre Hacienda / Empacadora",
                    "ubicacion": "Ubicación",
                    "correo": "Correo Hacienda",
                    "encargado": "Encargado / Administrador",
                    "activa": "Activa"
                }
            )

    # --------------------------------------------------------------------------
    # PESTAÑA 2: REGISTRAR NUEVA HACIENDA
    # --------------------------------------------------------------------------
    with tab_crear:
        st.subheader("Registrar Nueva Hacienda Empacadora")
        
        if df_comp_activas.empty:
            st.warning("⚠️ Primero debe registrar al menos una Compañía en el módulo de Compañías.")
        else:
            mapa_comp_id = {f"{r['nombre']} ({r['ruc']})": int(r["id"]) for _, r in df_comp_activas.iterrows()}
            labels_comp = list(mapa_comp_id.keys())

            with st.form("form_crear_hacienda_clean", clear_on_submit=True):
                col1, col2 = st.columns(2)
                with col1:
                    h_comp_label = st.selectbox("Compañía Matriz *", labels_comp)
                    h_cod = st.text_input("Código Único *", max_chars=20, placeholder="Ej: EMP06")
                    h_nom = st.text_input("Nombre de la Hacienda *", placeholder="Ej: Hacienda Santa Rita")
                    h_ubi = st.text_input("Ubicación", placeholder="Ej: Km 18 Vía El Triunfo")
                with col2:
                    h_cor = st.text_input("Correo de la Hacienda", placeholder="Ej: santarita@bananeras.com")
                    h_enc = st.text_input("Encargado / Administrador", placeholder="Ej: Ing. Juan Pérez")
                    h_act = st.checkbox("Hacienda Activa", value=True)

                st.divider()
                if st.form_submit_button("Guardar Hacienda", type="primary", use_container_width=True):
                    if not h_cod.strip() or not h_nom.strip():
                        st.error("El Código y el Nombre de la hacienda son obligatorios.")
                    else:
                        try:
                            run_query("""
                                INSERT INTO empacadoras (compania_id, codigo, nombre, ubicacion, correo, encargado, activa)
                                VALUES (%s, %s, %s, %s, %s, %s, %s);
                            """, (
                                mapa_comp_id[h_comp_label],
                                h_cod.strip().upper(),
                                h_nom.strip(),
                                h_ubi.strip(),
                                h_cor.strip(),
                                h_enc.strip(),
                                h_act
                            ))
                            st.success(f"¡Hacienda '{h_nom}' registrada con éxito!")
                        except Exception as e:
                            st.error(f"Error al guardar (verifique que el código '{h_cod}' no exista ya): {e}")

    # --------------------------------------------------------------------------
    # PESTAÑA 3: MODIFICAR / ELIMINAR HACIENDA
    # --------------------------------------------------------------------------
    with tab_editar:
        st.subheader("Modificar o Eliminar Hacienda Existente")
        
        df_hac_ed = run_query("""
            SELECT e.id, c.nombre AS compania, e.compania_id, e.codigo, e.nombre, 
                   e.ubicacion, e.correo, e.encargado, e.activa
            FROM empacadoras e
            LEFT JOIN companias c ON e.compania_id = c.id
            ORDER BY e.codigo ASC;
        """, fetch=True)

        if df_comp_activas.empty or df_hac_ed.empty:
            st.info("No hay haciendas registradas para editar.")
        else:
            mapa_comp_id = {f"{r['nombre']} ({r['ruc']})": int(r["id"]) for _, r in df_comp_activas.iterrows()}
            labels_comp = list(mapa_comp_id.keys())

            opciones_hac = {f"[{r['codigo']}] {r['nombre']}": r for _, r in df_hac_ed.iterrows()}
            sel_hac_label = st.selectbox("Seleccione la Hacienda a gestionar:", list(opciones_hac.keys()))
            hac_actual = opciones_hac[sel_hac_label]

            idx_comp_actual = 0
            for i, lbl in enumerate(labels_comp):
                if mapa_comp_id[lbl] == hac_actual["compania_id"]:
                    idx_comp_actual = i
                    break

            with st.form("form_editar_hacienda_clean"):
                col1, col2 = st.columns(2)
                with col1:
                    eh_comp_label = st.selectbox("Compañía Matriz *", labels_comp, index=idx_comp_actual)
                    eh_cod = st.text_input("Código Único *", value=hac_actual["codigo"], max_chars=20)
                    eh_nom = st.text_input("Nombre Hacienda *", value=hac_actual["nombre"])
                    eh_ubi = st.text_input("Ubicación", value=hac_actual["ubicacion"] or "")
                with col2:
                    eh_cor = st.text_input("Correo Hacienda", value=hac_actual["correo"] or "")
                    eh_enc = st.text_input("Encargado", value=hac_actual["encargado"] or "")
                    eh_act = st.checkbox("Hacienda Activa", value=bool(hac_actual["activa"]))

                st.divider()
                hb1, hb2 = st.columns(2)
                btn_h_upd = hb1.form_submit_button("Actualizar Cambios", type="primary", use_container_width=True)
                btn_h_del = hb2.form_submit_button("🗑️ Eliminar Hacienda", use_container_width=True)

                if btn_h_upd:
                    if not eh_cod.strip() or not eh_nom.strip():
                        st.error("Código y Nombre son obligatorios.")
                    else:
                        try:
                            run_query("""
                                UPDATE empacadoras 
                                SET compania_id=%s, codigo=%s, nombre=%s, ubicacion=%s, correo=%s, encargado=%s, activa=%s
                                WHERE id=%s;
                            """, (
                                mapa_comp_id[eh_comp_label],
                                eh_cod.strip().upper(),
                                eh_nom.strip(),
                                eh_ubi.strip(),
                                eh_cor.strip(),
                                eh_enc.strip(),
                                eh_act,
                                int(hac_actual["id"])
                            ))
                            st.success("¡Hacienda actualizada correctamente!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error al actualizar: {e}")

                if btn_h_del:
                    try:
                        run_query("DELETE FROM empacadoras WHERE id=%s;", (int(hac_actual["id"]),))
                        st.success("¡Hacienda eliminada correctamente!")
                        st.rerun()
                    except Exception:
                        st.error("No se puede eliminar esta hacienda porque tiene dispositivos o empleados asignados.")