#!/usr/bin/env python3
"""Make the demo database of the README, or remove it.

    python3 scripts/demo.py                  create pg_mcp_demo and the role mcp_demo_agent
    python3 scripts/demo.py --password s3cret
    python3 scripts/demo.py --drop           remove them again

Connects with the PG* variables of the shell as whoever may create databases and roles (a
superuser on a fresh PostgreSQL), creates the database, runs docs/demo.sql in it and makes
the role of the agent, which reads the shop schema and nothing else: the same grants as
docs/role.sql. Then it prints what to put where so that the agent reads the demo.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "pg_explorer_mcp" / "_vendor"))  # the driver ships with the server

import pg8000.exceptions  # noqa: E402
import pg8000.native  # noqa: E402

from pg_explorer_mcp.db import Settings  # noqa: E402

DEMO_SQL = ROOT / "docs" / "demo.sql"
DATABASE = "pg_mcp_demo"
ROLE = "mcp_demo_agent"
PASSWORD = "demo"  # noqa: S105 (a demo on localhost; the README says so)
SCHEMA = "shop"


def connect(settings: Settings, database: str) -> pg8000.native.Connection:
    return pg8000.native.Connection(
        user=settings.user,
        password=settings.password,
        host=settings.host,
        port=settings.port,
        database=database,
        timeout=settings.socket_timeout_s,
    )


def create(settings: Settings, database: str, role: str, password: str) -> None:
    admin = connect(settings, settings.database)
    try:
        if admin.run("SELECT 1 FROM pg_database WHERE datname = :name", name=database):
            raise SystemExit(f"the database {database} exists already; run with --drop first to make it anew")
        admin.run(f"CREATE DATABASE \"{database}\" TEMPLATE template0 ENCODING 'UTF8'")
        # CREATE ROLE takes no parameters, so the password goes in as a literal, quotes doubled.
        literal = "'" + password.replace("'", "''") + "'"
        if admin.run("SELECT 1 FROM pg_roles WHERE rolname = :name", name=role):
            admin.run(f'ALTER ROLE "{role}" WITH LOGIN PASSWORD {literal}')
        else:
            admin.run(f'CREATE ROLE "{role}" LOGIN PASSWORD {literal}')
    finally:
        admin.close()
    demo = connect(settings, database)
    try:
        for statement in statements(DEMO_SQL.read_text(encoding="utf-8")):
            demo.run(statement)
        # The agent's role: connect, see the shop schema, read its tables now and later; nothing in hr.
        demo.run(f'GRANT CONNECT ON DATABASE "{database}" TO "{role}"')
        demo.run(f'GRANT USAGE ON SCHEMA {SCHEMA} TO "{role}"')
        demo.run(f'GRANT SELECT ON ALL TABLES IN SCHEMA {SCHEMA} TO "{role}"')
        demo.run(f'ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} GRANT SELECT ON TABLES TO "{role}"')
    finally:
        demo.close()


def drop(settings: Settings, database: str, role: str) -> None:
    admin = connect(settings, settings.database)
    try:
        admin.run(f'DROP DATABASE IF EXISTS "{database}"')
        if admin.run("SELECT 1 FROM pg_roles WHERE rolname = :name", name=role):
            admin.run(f'DROP ROLE "{role}"')
    finally:
        admin.close()


def statements(sql: str) -> list[str]:
    """The statements of a script without dollar quoting: split on the semicolons that end lines."""
    found, current = [], []
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        current.append(line)
        if stripped.endswith(";"):
            found.append("\n".join(current).rstrip(";"))
            current = []
    if current:
        found.append("\n".join(current))
    return found


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Make the demo database of the README, or remove it.")
    parser.add_argument("--database", default=DATABASE, help=f"database to create (default: {DATABASE})")
    parser.add_argument("--role", default=ROLE, help=f"the agent's role (default: {ROLE})")
    parser.add_argument("--password", default=PASSWORD, help=f"the role's password (default: {PASSWORD})")
    parser.add_argument("--drop", action="store_true", help="remove the database and the role")
    args = parser.parse_args(argv)
    settings = Settings.from_env(os.environ)
    try:
        if args.drop:
            drop(settings, args.database, args.role)
            print(f"dropped the database {args.database} and the role {args.role}")
            return 0
        create(settings, args.database, args.role, args.password)
    except (pg8000.exceptions.Error, OSError) as exc:
        print(f"could not do it as {settings.describe()}: {exc}", file=sys.stderr)
        return 1
    print(f"created the database {args.database} and the role {args.role}, which reads the {SCHEMA} schema")
    print("next, as a line of the password file (~/.pgpass, chmod 600), with the password you chose:")
    print(f"  {settings.host}:{settings.port}:{args.database}:{args.role}:<password>")
    print("then register the server for the demo:")
    print(f"  PGDATABASE={args.database} PGUSER={args.role} python3 {ROOT / 'install.py'} --profile analyst")
    return 0


if __name__ == "__main__":
    sys.exit(main())
