# -*- coding: utf-8 -*-
import streamlit as st
import pandas as pd
from datetime import date
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


def construir_sql_inconsistencias(hac_id=None, area_filtro=None, tipo_inc=None):
    """Construye la consulta de inconsistencias aplicando filtros de hacienda, área e incidencia en SQL."""
    filtros_inc_marcas = ["(sin_registro_maestro OR MOD(total_marcas, 2) <> 0)"]
    params_inc_marcas = []

    if hac_id is not None:
        filtros_inc_marcas.append("hacienda_id = %s")
        params_inc_marcas.append(hac_id)

    if area_filtro and area_filtro != "Todas las áreas":
        filtros_inc_marcas.append("area = %s")
        params_inc_marcas.append(area_filtro)

    if tipo_inc == "Marcación sin registro (PIN desconocido)":
        filtros_inc_marcas.append("sin_registro_maestro = TRUE")
    elif tipo_inc == "Marcación impar (falta entrada o salida)":
        filtros_inc_marcas.append("(sin_registro_maestro = FALSE AND MOD(total_marcas, 2) <> 0)")

    filtros_ausencias = ["e.activo = TRUE"]
    params_ausencias = []

    if hac_id is not None:
        filtros_ausencias.append("eh.empacadora_id = %s")
        params_ausencias.append(hac_id)

    if area_filtro and area_filtro != "Todas las áreas":
        filtros_ausencias.append("eh.area_autorizada = %s")
        params_ausencias.append(area_filtro)

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

    # Orden de %s: generate_series (2), BETWEEN (2), luego los filtros dinámicos
    todos_params = [None, None, None, None] + params_finales
    return sql, todos_params


def render():
    st.title("📊 Módulo de Reportes y Auditoría")
    st.write("Generación de reportes detallados de personal por área/hacienda y control de marcaciones con filtros por cédula, fechas y roles operativos.")

    df_haciendas = run_query("""
        SELECT e.id, e.codigo, e.nombre, c.nombre AS compania
        FROM empacadoras e
        JOIN companias c ON e.compania_id = c.id
        WHERE e.activa = TRUE
        ORDER BY c.nombre ASC, e.codigo ASC;
    """, fetch=True)

    df_areas_base = run_query(
        "SELECT DISTINCT area FROM empleados WHERE area IS NOT NULL AND TRIM(area) <> '' ORDER BY area ASC;",
        fetch=True,
    )
    areas_disponibles = df_areas_base["area"].tolist() if not df_areas_base.empty else []

    tab_rep_emps, tab_rep_marc, tab_rep_inc = st.tabs([
        "👥 Reporte de Empleados por Área / Hacienda",
        "⏱️ Reporte de Marcaciones (Asistencia / Comedor)",
        "⚠️ Inconsistencias y Ausencias"
    ])

    # --------------------------------------------------------------------------
    # PESTAÑA 1: REPORTE DE EMPLEADOS POR ÁREA / HACIENDA
    # --------------------------------------------------------------------------
    with tab_rep_emps:
        st.subheader("Filtrar Personal y Autorizaciones")

        if df_haciendas.empty:
            st.info("No hay haciendas registradas en el sistema.")
        else:
            col_f1, col_f2, col_f3 = st.columns(3)
            with col_f1:
                hac_opts = {"Todas las haciendas": None}
                for _, r in df_haciendas.iterrows():
                    hac_opts[f"[{r['codigo']}] {r['nombre']}"] = int(r["id"])
                sel_hac_lbl = st.selectbox("Hacienda:", list(hac_opts.keys()), key="rep_emp_hac")
                hac_id_filtro = hac_opts[sel_hac_lbl]

            with col_f2:
                area_opts = ["Todas las áreas"] + areas_disponibles
                sel_area = st.selectbox("Área / Departamento:", area_opts, key="rep_emp_area")

            with col_f3:
                busqueda_cedula = st.text_input("Buscar por Cédula / PIN exacto o parcial:", placeholder="Ej: 0928...", key="rep_emp_cedula")

            sql_emp = """
                SELECT DISTINCT emp.pin AS cedula, emp.nombres, 
                       COALESCE(emp.area, 'Sin área') AS area,
                       emp.activo,
                       STRING_AGG(CONCAT('[', e_hac.codigo, '] ', e_hac.nombre), ', ') AS haciendas_autorizadas
                FROM empleados emp
                LEFT JOIN empleado_haciendas eh ON emp.pin = eh.pin_empleado
                LEFT JOIN empacadoras e_hac ON eh.empacadora_id = e_hac.id
            """
            conds = []
            params_emp = []

            if hac_id_filtro:
                conds.append("emp.pin IN (SELECT pin_empleado FROM empleado_haciendas WHERE empacadora_id = %s)")
                params_emp.append(hac_id_filtro)

            if sel_area != "Todas las áreas":
                conds.append("emp.area = %s")
                params_emp.append(sel_area)

            if busqueda_cedula.strip():
                conds.append("emp.pin ILIKE %s")
                params_emp.append(f"%{busqueda_cedula.strip()}%")

            if conds:
                sql_emp += " WHERE " + " AND ".join(conds)

            sql_emp += " GROUP BY emp.pin, emp.nombres, emp.area, emp.activo ORDER BY emp.nombres ASC;"
            df_resultado_emps = run_query(sql_emp, tuple(params_emp) if params_emp else None, fetch=True)

            st.divider()
            if not df_resultado_emps.empty:
                st.success(f"Se encontraron {len(df_resultado_emps)} colaboradores con los filtros aplicados.")
                st.dataframe(
                    df_resultado_emps,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "cedula": "Cédula / PIN",
                        "nombres": "Apellidos y Nombres",
                        "area": "Área / Departamento",
                        "activo": "Estado Activo",
                        "haciendas_autorizadas": "Haciendas Autorizadas"
                    }
                )
            else:
                st.info("No se encontraron registros de empleados para los filtros seleccionados.")

    # --------------------------------------------------------------------------
    # PESTAÑA 2: REPORTE DE MARCACIONES (ASISTENCIA / COMEDOR)
    # --------------------------------------------------------------------------
    with tab_rep_marc:
        st.subheader("Auditoría de Marcaciones (Empaque vs Comedor)")

        if df_haciendas.empty:
            st.info("No hay haciendas registradas en el sistema.")
        else:
            c_m1, c_m2, c_m3, c_m4 = st.columns(4)
            with c_m1:
                f_ini = st.date_input("Fecha Desde:", value=date.today(), key="rep_f_ini")
            with c_m2:
                f_fin = st.date_input("Fecha Hasta:", value=date.today(), key="rep_f_fin")
            with c_m3:
                hac_marc_opts = {"Todas las haciendas": None}
                for _, r in df_haciendas.iterrows():
                    hac_marc_opts[f"[{r['codigo']}] {r['nombre']}"] = int(r["id"])
                sel_hac_m_lbl = st.selectbox("Hacienda Ubicación:", list(hac_marc_opts.keys()), key="rep_marc_hac")
                hac_m_id = hac_marc_opts[sel_hac_m_lbl]
            with c_m4:
                rol_opts = ["Todos los roles", "EMPAQUE", "COMEDOR", "PALETIZADO", "OFICINA", "GARITA"]
                sel_rol = st.selectbox("Rol del Dispositivo:", rol_opts, key="rep_marc_rol")

            c_m5, c_m6 = st.columns(2)
            with c_m5:
                filtro_cedula_marc = st.text_input("Filtrar por Cédula / PIN específico (Opcional):", placeholder="Ej: 0912345678", key="rep_marc_cedula")
            with c_m6:
                area_marc_opts = ["Todas las áreas"] + areas_disponibles
                sel_area_m = st.selectbox("Área del Colaborador:", area_marc_opts, key="rep_marc_area")

            sql_marc = """
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
            conds_m = [
                "m.fecha_hora::date >= %s",
                "m.fecha_hora::date <= %s"
            ]
            params_m = [f_ini, f_fin]

            if hac_m_id:
                conds_m.append("d.empacadora_id = %s")
                params_m.append(hac_m_id)

            if sel_rol != "Todos los roles":
                conds_m.append("d.rol = %s")
                params_m.append(sel_rol)

            if filtro_cedula_marc.strip():
                conds_m.append("m.pin_empleado ILIKE %s")
                params_m.append(f"%{filtro_cedula_marc.strip()}%")

            if sel_area_m != "Todas las áreas":
                conds_m.append("e.area = %s")
                params_m.append(sel_area_m)

            if conds_m:
                sql_marc += " WHERE " + " AND ".join(conds_m)

            sql_marc += " ORDER BY m.fecha_hora DESC;"
            df_resultado_marc = run_query(sql_marc, tuple(params_m), fetch=True)

            st.divider()
            if not df_resultado_marc.empty:
                st.success(f"Se encontraron {len(df_resultado_marc)} marcaciones bajo los criterios seleccionados.")
                st.dataframe(
                    df_resultado_marc,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "fecha_hora": "Fecha y Hora",
                        "cedula": "Cédula / PIN",
                        "empleado": "Colaborador",
                        "area_empleado": "Área de Trabajo",
                        "dispositivo": "Reloj Biométrico",
                        "rol_dispositivo": "Rol (Empaque/Comedor)",
                        "hacienda_ubicacion": "Hacienda Ubicación",
                        "estado_verificacion": "Estado"
                    }
                )
            else:
                st.info("No se registran marcaciones en el rango de fechas y filtros seleccionados.")

    # --------------------------------------------------------------------------
    # PESTAÑA 3: INCONSISTENCIAS, MARCACIONES SIN REGISTRO Y AUSENCIAS
    # --------------------------------------------------------------------------
    with tab_rep_inc:
        st.subheader("Inconsistencias, Marcaciones Sin Registro y Ausencias")
        st.caption("Detecta PINs desconocidos, marcaciones impares (falta entrada o salida) y colaboradores activos sin marcación.")

        if df_haciendas.empty:
            st.info("No hay haciendas registradas en el sistema.")
        else:
            c_i1, c_i2, c_i3 = st.columns(3)
            with c_i1:
                f_inc_ini = st.date_input("Fecha Desde:", value=date.today(), key="rep_inc_desde")
            with c_i2:
                f_inc_fin = st.date_input("Fecha Hasta:", value=date.today(), key="rep_inc_hasta")
            with c_i3:
                tipo_inc = st.selectbox(
                    "Tipo de incidencia:",
                    [
                        "Todas las incidencias",
                        "Marcación sin registro (PIN desconocido)",
                        "Marcación impar (falta entrada o salida)",
                        "Activo sin marcación",
                    ],
                    key="rep_inc_tipo",
                )

            c_i4, c_i5 = st.columns(2)
            with c_i4:
                hac_inc_opts = {"Todas las haciendas": None}
                for _, r in df_haciendas.iterrows():
                    hac_inc_opts[f"[{r['codigo']}] {r['nombre']}"] = int(r["id"])
                sel_hac_i_lbl = st.selectbox("Hacienda:", list(hac_inc_opts.keys()), key="rep_inc_hac")
                hac_i_id = hac_inc_opts[sel_hac_i_lbl]
            with c_i5:
                area_inc_opts = ["Todas las áreas"] + areas_disponibles
                sel_area_i = st.selectbox("Área:", area_inc_opts, key="rep_inc_area")

            st.divider()
            if f_inc_ini > f_inc_fin:
                st.warning("La fecha 'Desde' no puede ser mayor que 'Hasta'.")
            else:
                sql_inc, params_inc = construir_sql_inconsistencias(
                    hac_id=hac_i_id,
                    area_filtro=sel_area_i,
                    tipo_inc=tipo_inc,
                )

                df_inc = pd.DataFrame()
                if sql_inc:
                    params_inc[0] = f_inc_ini
                    params_inc[1] = f_inc_fin
                    params_inc[2] = f_inc_ini
                    params_inc[3] = f_inc_fin
                    df_inc = run_query(sql_inc, tuple(params_inc), fetch=True)

                if df_inc.empty:
                    st.info("No se encontraron incidencias para el rango y filtros seleccionados.")
                else:
                    st.dataframe(
                        df_inc,
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "fecha": "Fecha",
                            "pin": "PIN / Cédula",
                            "colaborador": "Colaborador",
                            "hacienda": "Hacienda",
                            "area": "Área",
                            "dispositivo_sn": "Dispositivo / SN",
                            "total_marcas": "Total Marcas",
                            "primera_marca": "Primera Marca",
                            "ultima_marca": "Última Marca",
                            "incidencia": "Diagnóstico / Incidencia",
                        },
                    )
                    csv_data = df_inc.to_csv(index=False).encode("utf-8-sig")
                    st.download_button(
                        "📥 Descargar Inconsistencias (CSV)",
                        data=csv_data,
                        file_name=f"inconsistencias_{f_inc_ini}_{f_inc_fin}.csv",
                        mime="text/csv",
                    )