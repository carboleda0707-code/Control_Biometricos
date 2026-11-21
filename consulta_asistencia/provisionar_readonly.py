# -*- coding: utf-8 -*-
import os
import re
import sys
from pathlib import Path

_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _quote_ident(ident: str, label: str = "identificador") -> str:
    if not ident or not _IDENT_RE.fullmatch(ident):
        raise ValueError(f"Nombre de {label} no válido o inseguro: '{ident}'")
    return '"' + ident.replace('"', '""') + '"'


def _quote_password_literal(password: str) -> str:
    if "\x00" in password:
        raise ValueError("La contraseña (PGPASSWORD) no puede contener caracteres nulos.")
    return "'" + password.replace("'", "''") + "'"


def _obtener_directorio_base() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _resolver_env(base_dir: Path) -> Path:
    candidatos = [
        base_dir / ".env",
        base_dir / "consulta_asistencia" / ".env",
        Path.cwd() / ".env",
    ]
    for ruta in candidatos:
        if ruta.exists():
            return ruta
    raise FileNotFoundError(
        "No se encontró el archivo .env en ninguna de las rutas esperadas: "
        + ", ".join(str(r) for r in candidatos)
    )


def cargar_env(env_path: Path, override: bool = True) -> None:
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and (override or k not in os.environ):
                os.environ[k] = v


def conectar_admin(host: str, port: int, dbname: str, admin_user: str, admin_password: str):
    try:
        import pg8000.dbapi
        return pg8000.dbapi.connect(
            user=admin_user,
            password=admin_password,
            host=host,
            port=port,
            database=dbname,
        )
    except ImportError:
        pass

    try:
        import psycopg
        return psycopg.connect(
            host=host,
            port=port,
            dbname=dbname,
            user=admin_user,
            password=admin_password,
            sslmode="disable",
        )
    except ImportError:
        import psycopg2
        return psycopg2.connect(
            host=host,
            port=port,
            dbname=dbname,
            user=admin_user,
            password=admin_password,
            sslmode="disable",
        )


def main() -> None:
    base_dir = _obtener_directorio_base()
    env_file = _resolver_env(base_dir)
    cargar_env(env_file, override=True)

    host = os.environ["PGHOST"].strip()
    port = int(os.environ.get("PGPORT", "5432").strip())
    dbname = os.environ["PGDATABASE"].strip()
    ro_user = os.environ["PGUSER"].strip()
    ro_password = os.environ["PGPASSWORD"]

    try:
        dbname_ident = _quote_ident(dbname, "base de datos (PGDATABASE)")
        ro_user_ident = _quote_ident(ro_user, "usuario (PGUSER)")
        pwd_literal = _quote_password_literal(ro_password)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    admin_user = os.environ.get("PGADMIN_USER", "postgres").strip()
    admin_password = os.environ.get("PGADMIN_PASSWORD")
    if not admin_password:
        admin_password = input(f"Contraseña para el usuario administrador [{admin_user}]: ").strip()

    conn = conectar_admin(host, port, dbname, admin_user, admin_password)
    try:
        conn.autocommit = True
        cur = conn.cursor()

        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ro_user,))
        existe = cur.fetchone() is not None

        if not existe:
            cur.execute(f"CREATE ROLE {ro_user_ident} LOGIN PASSWORD {pwd_literal};")
        else:
            cur.execute(f"ALTER ROLE {ro_user_ident} WITH LOGIN PASSWORD {pwd_literal};")

        cur.execute(f"GRANT CONNECT ON DATABASE {dbname_ident} TO {ro_user_ident};")
        cur.execute(f"GRANT USAGE ON SCHEMA public TO {ro_user_ident};")
        cur.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {ro_user_ident};")
        cur.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {ro_user_ident};")
        cur.execute(f"REVOKE CREATE ON SCHEMA public FROM {ro_user_ident};")

        cur.close()
        print(f"OK: Rol '{ro_user}' configurado en modo solo lectura sobre '{dbname}'.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()