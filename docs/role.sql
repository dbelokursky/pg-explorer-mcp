-- pg-mcp: a read-only role for the agent (ADR 0002).
--
-- Run it as a superuser or as the owner of the schemas, in the database the agent will read:
--
--   psql -d app -v role=mcp_agent -v password='…' -v schemas=sales,reports -v owner=app_owner -f docs/role.sql
--
--   role      name of the new login role; PostgreSQL reserves names that start with pg_
--   password  its password; give it to users through ~/.pgpass, never in the agent settings
--   schemas   comma-separated schemas the agent may read
--   owner     role that creates tables in those schemas, so future tables become readable too
--
-- The role is the real boundary: pg-mcp's read-only session and SQL check only back it up.
-- Never grant it pg_read_all_stats or pg_monitor (they show other sessions' query texts),
-- pg_read_server_files, pg_write_server_files, pg_execute_server_program, pg_signal_backend,
-- or membership in a role that owns tables.

\set ON_ERROR_STOP on

CREATE ROLE :"role" LOGIN PASSWORD :'password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 5;

ALTER ROLE :"role" SET default_transaction_read_only = on;
ALTER ROLE :"role" SET statement_timeout = '5s';
ALTER ROLE :"role" SET idle_in_transaction_session_timeout = '60s';

SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'role') \gexec

SELECT format('GRANT USAGE ON SCHEMA %I TO %I', schema_name, :'role')
FROM unnest(string_to_array(:'schemas', ',')) AS schema_name \gexec

SELECT format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO %I', schema_name, :'role')
FROM unnest(string_to_array(:'schemas', ',')) AS schema_name \gexec

SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT SELECT ON TABLES TO %I', :'owner', schema_name, :'role')
FROM unnest(string_to_array(:'schemas', ',')) AS schema_name \gexec
