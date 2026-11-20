DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'claude_readonly') THEN
        CREATE ROLE claude_readonly LOGIN PASSWORD 'exbanac2026*';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE bananeras_db TO claude_readonly;
GRANT USAGE ON SCHEMA public TO claude_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO claude_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO claude_readonly;
REVOKE CREATE ON SCHEMA public FROM claude_readonly;