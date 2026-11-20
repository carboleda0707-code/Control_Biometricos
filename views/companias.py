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
    st.title("🏛️ Gestión de Compañías (Matriz)")
    st.write("Administración centralizada de razones sociales y datos de contacto.")

    # Pestañas limpias para no saturar la vista
    tab_lista, tab_crear, tab_editar = st.tabs(["📋 Listado General", "➕ Registrar Nueva", "✏️ Modificar / Eliminar"])

    # --------------------------------------------------------------------------
    # PESTAÑA 1: LISTADO GENERAL
    # --------------------------------------------------------------------------
    with tab_lista:
        st.subheader("Compañías Registradas")
        df_companias = run_query("""
            SELECT id, ruc, nombre, direccion, telefono, correo_principal, activa 
            FROM companias 
            ORDER BY id ASC;
        """, fetch=True)
        
        if df_companias.empty:
            st.info("No hay compañías registradas en el sistema.")
        else:
            st.dataframe(
                df_companias,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "id": "ID",
                    "ruc": "RUC",
                    "nombre": "Razón Social / Nombre",
                    "direccion": "Dirección Matriz",
                    "telefono": "Teléfono",
                    "correo_principal": "Correo Principal",
                    "activa": "Activa"
                }
            )

    # --------------------------------------------------------------------------
    # PESTAÑA 2: REGISTRAR NUEVA COMPAÑÍA
    # --------------------------------------------------------------------------
    with tab_crear:
        st.subheader("Registrar Nueva Compañía Matriz")
        
        with st.form("form_crear_compania_clean", clear_on_submit=True):
            col1, col2 = st.columns(2)
            with col1:
                c_ruc = st.text_input("RUC *", max_chars=13, placeholder="Ej: 0991234567001")
                c_nombre = st.text_input("Nombre / Razón Social *", placeholder="Ej: Exportadora Bananera S.A.")
                c_dir = st.text_input("Dirección Principal", placeholder="Ej: Av. Principal y Calle 10")
            with col2:
                c_tel = st.text_input("Teléfono", placeholder="Ej: 04-2555555")
                c_mail = st.text_input("Correo Principal", placeholder="Ej: gerencia@empresa.com")
                c_act = st.checkbox("Compañía Activa", value=True)
            
            st.divider()
            if st.form_submit_button("Guardar Compañía", type="primary", use_container_width=True):
                if not c_ruc.strip() or not c_nombre.strip():
                    st.error("El RUC y el Nombre son obligatorios.")
                else:
                    try:
                        run_query("""
                            INSERT INTO companias (ruc, nombre, direccion, telefono, correo_principal, activa)
                            VALUES (%s, %s, %s, %s, %s, %s);
                        """, (c_ruc.strip(), c_nombre.strip(), c_dir.strip(), c_tel.strip(), c_mail.strip(), c_act))
                        st.success(f"¡Compañía '{c_nombre}' creada correctamente!")
                    except Exception as e:
                        st.error(f"Error al guardar (verifique que el RUC no esté duplicado): {e}")

    # --------------------------------------------------------------------------
    # PESTAÑA 3: MODIFICAR / ELIMINAR COMPAÑÍA
    # --------------------------------------------------------------------------
    with tab_editar:
        st.subheader("Modificar o Eliminar Compañía Existente")
        
        df_comp_ed = run_query("SELECT id, ruc, nombre, direccion, telefono, correo_principal, activa FROM companias ORDER BY id ASC;", fetch=True)
        
        if df_comp_ed.empty:
            st.info("No hay compañías registradas para editar.")
        else:
            opciones_comp = {f"{row['ruc']} - {row['nombre']}": row for _, row in df_comp_ed.iterrows()}
            sel_comp_label = st.selectbox("Seleccione la Compañía a gestionar:", list(opciones_comp.keys()))
            comp_actual = opciones_comp[sel_comp_label]

            with st.form("form_editar_compania_clean"):
                col1, col2 = st.columns(2)
                with col1:
                    e_ruc = st.text_input("RUC *", value=comp_actual["ruc"], max_chars=13)
                    e_nombre = st.text_input("Nombre / Razón Social *", value=comp_actual["nombre"])
                    e_dir = st.text_input("Dirección", value=comp_actual["direccion"] or "")
                with col2:
                    e_tel = st.text_input("Teléfono", value=comp_actual["telefono"] or "")
                    e_mail = st.text_input("Correo Principal", value=comp_actual["correo_principal"] or "")
                    e_act = st.checkbox("Compañía Activa", value=bool(comp_actual["activa"]))

                st.divider()
                b1, b2 = st.columns(2)
                btn_upd = b1.form_submit_button("Actualizar Cambios", type="primary", use_container_width=True)
                btn_del = b2.form_submit_button("🗑️ Eliminar Compañía", use_container_width=True)

                if btn_upd:
                    if not e_ruc.strip() or not e_nombre.strip():
                        st.error("RUC y Nombre son obligatorios.")
                    else:
                        try:
                            run_query("""
                                UPDATE companias 
                                SET ruc=%s, nombre=%s, direccion=%s, telefono=%s, correo_principal=%s, activa=%s
                                WHERE id=%s;
                            """, (e_ruc.strip(), e_nombre.strip(), e_dir.strip(), e_tel.strip(), e_mail.strip(), e_act, int(comp_actual["id"])))
                            st.success("¡Compañía actualizada con éxito!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error al actualizar: {e}")

                if btn_del:
                    try:
                        run_query("DELETE FROM companias WHERE id=%s;", (int(comp_actual["id"]),))
                        st.success("¡Compañía eliminada correctamente!")
                        st.rerun()
                    except Exception:
                        st.error("No se puede eliminar esta compañía porque tiene haciendas empacadoras asociadas.")