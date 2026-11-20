import io
from datetime import datetime, time
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from db import get_conn

def generar_excel_comedor(empacadora_id: int, fecha_str: str):
    """
    fecha_str: 'YYYY-MM-DD'
    Genera el Excel de almuerzos (11:00 a 14:00) validando asistencia previa el mismo día.
    """
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT codigo, nombre FROM empacadoras WHERE id = %s", (empacadora_id,))
    emp = cur.fetchone()
    if not emp:
        raise ValueError("Empacadora no encontrada")

    inicio_almuerzo = f"{fecha_str} 11:00:00"
    fin_almuerzo = f"{fecha_str} 14:00:00"
    inicio_dia = f"{fecha_str} 00:00:00"

    query = """
        SELECT 
            m.pin_empleado AS pin,
            COALESCE(e.nombres, 'NO REGISTRADO EN MAESTRO') AS nombres,
            COALESCE(e.cedula, '-') AS cedula,
            COALESCE(e.area, 'SIN ÁREA') AS area,
            MIN(m.fecha_hora) AS hora_almuerzo,
            d.nombre AS reloj_comedor,
            EXISTS (
                SELECT 1 
                FROM marcaciones m_prev
                JOIN dispositivos d_prev ON m_prev.sn_dispositivo = d_prev.sn
                WHERE m_prev.pin_empleado = m.pin_empleado
                  AND m_prev.fecha_hora >= %s
                  AND m_prev.fecha_hora < MIN(m.fecha_hora)
                  AND d_prev.rol IN ('EMPAQUE', 'OFICINA')
            ) AS tiene_entrada_previa
        FROM marcaciones m
        JOIN dispositivos d ON m.sn_dispositivo = d.sn
        LEFT JOIN empleados e ON m.pin_empleado = e.pin
        WHERE d.empacadora_id = %s
          AND d.rol = 'COMEDOR'
          AND m.fecha_hora BETWEEN %s AND %s
        GROUP BY m.pin_empleado, e.nombres, e.cedula, e.area, d.nombre
        ORDER BY tiene_entrada_previa ASC, hora_almuerzo ASC;
    """
    cur.execute(query, (inicio_dia, empacadora_id, inicio_almuerzo, fin_almuerzo))
    registros = cur.fetchall()
    cur.close()
    conn.close()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"Comedor {emp['codigo']}"

    # Encabezados
    headers = ["PIN", "Cédula", "Empleado", "Área", "Hora Almuerzo", "Reloj", "Estado Validación"]
    ws.append(headers)

    header_fill = PatternFill(start_color="065F46", end_color="065F46", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    alerta_fill = PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid")
    alerta_font = Font(color="92400E", bold=True)

    for col_num in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_num)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    for row_idx, r in enumerate(registros, start=2):
        estado = "VALIDADO (Asistencia Previa)" if r["tiene_entrada_previa"] else "REVISAR / VISITA / EXTRA"
        fila = [
            r["pin"],
            r["cedula"],
            r["nombres"],
            r["area"],
            r["hora_almuerzo"].strftime("%H:%M:%S"),
            r["reloj_comedor"],
            estado
        ]
        ws.append(fila)
        if not r["tiene_entrada_previa"]:
            for c in range(1, len(headers) + 1):
                ws.cell(row=row_idx, column=c).fill = alerta_fill
                ws.cell(row=row_idx, column=c).font = alerta_font

    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = max_len + 4

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer, f"Comedor_{emp['codigo']}_{fecha_str}.xlsx"