# Contexto del Proyecto — Consultas de Asistencia y Comedor (`bananeras_db`)

## 1. Modo de Ejecución, Entorno Híbrido y Seguridad
- Este sistema opera en modo híbrido: **PC servidor local (LAN/VLAN)** para operación en planta y **réplica en la nube (Railway)** para teletrabajo / consulta remota.
- Para consultar la base de datos PostgreSQL configurada en el `.env` activo (`bananeras_db` local o réplica en Railway), ejecuta:
  - Si existe `consultar.exe` en la carpeta: `.\consultar.exe "<CONSULTA_SQL>"`
  - Si se ejecuta desde Python: `python consultar.py "<CONSULTA_SQL>"`
- Si necesitas cambiar entre entorno local y teletrabajo, reemplaza o actualiza el archivo `.env` a partir de `.env.local` o `.env.nube` según corresponda e indica claramente en tu respuesta qué entorno (`PGHOST` / `PGDATABASE`) se consultó.
- Usa **exclusivamente consultas `SELECT` o `WITH`**.
- **Nunca** intentes modificar datos, alterar tablas ni editar archivos de esta carpeta salvo solicitud explícita del usuario.
- Zona horaria operativa: `America/Guayaquil`.

## 2. Esquema Real y Relaciones Clave
- `companias` (`id`, `ruc`, `nombre`, `activa`): razón social matriz.
- `empacadoras` (`id`, `compania_id`, `codigo`, `nombre`, `ubicacion`, `activa`): haciendas / empacadoras (`EMP01` a `EMP05`, etc.).
- `departamentos` (`id`, `empacadora_id`, `nombre`, `activo`): áreas o departamentos por empacadora.
- `dispositivos` (`sn`, `empacadora_id`, `nombre`, `rol`, `tipo_conexion`, `ip_address`, `port`, `ultima_conexion`): relojes biométricos; `rol` puede ser `'COMEDOR'`, `'EMPAQUE'`, `'PALETIZADO'`, `'OFICINA'` o `'GARITA'`.
- `empleados` (`pin`, `cedula`, `nombres`, `empacadora_id`, `departamento_id`, `area`, `activo`): ficha del colaborador (`pin` es clave primaria).
- `empleado_haciendas` (`pin_empleado`, `empacadora_id`, `area_autorizada`): relación **1:N** que permite múltiples haciendas y áreas autorizadas por colaborador. Al filtrar o agrupar por hacienda/empacadora o área, cruza esta tabla y usa `DISTINCT` o agregación explícita para evitar filas duplicadas.
- `marcaciones` (`id`, `pin_empleado`, `fecha_hora`, `sn_dispositivo`, `estado_verificacion`, `creado_en`): marcaciones de los biométricos (`uq_marcacion_unica` evita duplicados).

## 3. Normalización de PINs
- Los PINs numéricos pueden venir o guardarse **sin ceros a la izquierda** (por ejemplo `'00145'` vs `'145'`).
- Al unir `empleados.pin`, `empleado_haciendas.pin_empleado` y `marcaciones.pin_empleado` (o filtrar por PIN), normaliza los PINs numéricos sin ceros a la izquierda (`NULLIF(LTRIM(pin, '0'), '')`) para no perder coincidencias.

## 4. Lógica Operativa de Comedor (Cuadre de las 14:15)
- La franja oficial de comedor configurada en el sistema es de **11:00:00 a 14:00:00**.
- Cuando pidan el listado o conteo de quiénes comieron por hacienda (`empacadoras`) para el cuadre de las **14:15**:
  - Filtra `marcaciones.fecha_hora` en la fecha solicitada entre las `11:00:00` y las `14:00:00` (salvo que indiquen otro rango).
  - Si existen relojes con `dispositivos.rol = 'COMEDOR'`, evalúa filtrarlos o priorizarlos según la consulta.
  - Normaliza los PINs, cruza con `empleados` y `empleado_haciendas` / `empacadoras` evitando duplicados por colaborador/hacienda, y entrega una tabla clara lista para cuadrar con el personal del comedor.