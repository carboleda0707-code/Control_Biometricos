-- Plantilla de aprovisionamiento de rol de solo lectura para bananeras_db.
-- Los valores __PGUSER__ y __PGPASSWORD__ se inyectan en tiempo de ejecución desde .env;
-- este archivo no debe contener contraseñas reales.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '__PGUSER__') THEN
        CREATE ROLE __PGUSER__ LOGIN PASSWORD '__PGPASSWORD__';
    ELSE
        ALTER ROLE __PGUSER__ WITH LOGIN PASSWORD '__PGPASSWORD__';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE bananeras_db TO __PGUSER__;
GRANT USAGE ON SCHEMA public TO __PGUSER__;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO __PGUSER__;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO __PGUSER__;
REVOKE CREATE ON SCHEMA public FROM __PGUSER__;