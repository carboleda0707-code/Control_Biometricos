# -*- coding: utf-8 -*-
import os
import sys
from pathlib import Path


def _directorio_base() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def cargar_env() -> None:
    candidatos = [
        _directorio_base() / ".env",
        Path.cwd() / ".env",
    ]
    for env_path in candidatos:
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
            break


def conectar():
    host = os.environ["PGHOST"]
    port = int(os.environ.get("PGPORT", "5432"))
    dbname = os.environ["PGDATABASE"]
    user = os.environ["PGUSER"]
    password = os.environ["PGPASSWORD"]
    sslmode = os.environ.get("PGSSLMODE", "disable").strip() or "disable"

    try:
        import pg8000.dbapi
        conn = pg8000.dbapi.connect(
            user=user,
            password=password,
            host=host,
            port=port,
            database=dbname,
        )
        cur = conn.cursor()
        cur.execute("SET default_transaction_read_only = on;")
        cur.close()
        return conn
    except ImportError:
        pass

    try:
        import psycopg
        return psycopg.connect(
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password,
            sslmode=sslmode,
            options="-c default_transaction_read_only=on",
        )
    except ImportError:
        import psycopg2
        return psycopg2.connect(
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password,
            sslmode=sslmode,
            options="-c default_transaction_read_only=on",
        )


def main() -> None:
    cargar_env()

    if len(sys.argv) < 2:
        print('Uso: consultar.exe "SELECT ..."  (o  python consultar.py "SELECT ...")')
        sys.exit(1)

    sql = sys.argv[1].strip()
    if not sql.upper().startswith(("SELECT", "WITH")):
        print("ERROR: Solo se permiten consultas de lectura (SELECT / WITH).")
        sys.exit(1)

    conn = conectar()
    try:
        cur = conn.cursor()
        cur.execute(sql)
        cols = [desc[0] for desc in cur.description] if cur.description else []
        rows = cur.fetchall()

        print(" | ".join(cols))
        print("-" * 70)
        for row in rows:
            print(" | ".join(str(val) if val is not None else "" for val in row))

        cur.close()
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()