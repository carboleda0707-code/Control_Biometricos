import psycopg2

try:
    print("Intentando conectar a PostgreSQL...")
    conn = psycopg2.connect(
        dbname="postgres",
        user="postgres",
        password="Banano2026*",
        host="localhost",
        port=5432
    )
    print("¡Conexion exitosa con postgres!")
    conn.close()
except Exception as e:
    print(f"Error detectado: {e}")