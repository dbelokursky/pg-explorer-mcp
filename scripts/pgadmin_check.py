#!/usr/bin/env python3
"""The pgAdmin job of tests.yml: servers.json as the export writes it, and what pgAdmin gave back, field by field.

    uv run python scripts/pgadmin_check.py write servers.json /var/lib/pgadmin/pgpass
    python3 scripts/pgadmin_check.py compare servers.json dumped.json

The job loads the file into a real pgAdmin (PGADMIN_SERVER_JSON_FILE), has it dump its servers
(setup.py dump-servers) and checks that every field came back as written, under its own server.
The Python lives here rather than in the workflow: GitHub takes the night run from the tests.yml
of the default branch, main, while the run checks out develop, so code inside the workflow stays
main's until a release, and the package it named broke the night run when develop renamed the
package (ADR 0007). A script comes from the checkout, with the package it imports, under whichever
name the checkout has for it; compare needs no package at all.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # a plain python3 from the checkout, without uv, finds the package too
# The name since 0.8.0 (ADR 0007) and the one main keeps until the release: the same file serves both branches.
PACKAGES = ("pg_explorer_mcp", "pg_mcp")


def exports_module() -> ModuleType:
    """The exports module of the server package, under whichever of its names the checkout has.

    Only a missing package is passed over: what is wrong inside one that is there stays an error.
    """
    for package in PACKAGES:
        if importlib.util.find_spec(package) is not None:
            return importlib.import_module(f"{package}.exports")
    raise ModuleNotFoundError(f"no package {' or '.join(PACKAGES)} on sys.path")


def connections(exports: ModuleType) -> list[Any]:
    """The connections the job loads: a plain one, one whose server certificate is checked, one on another port.

    tests.yml waits for pgAdmin to report 2 server groups and 3 servers: the stands here, and their number.
    """
    connection = exports.Connection
    host = "dev7-db.example"  # one host for the two dev7 connections, as a stand has one for its databases
    return [
        connection("dev7", "orders", "billing", host, 5432, "orders", "billing", None),
        connection("dev7", "orders", "shipping", host, 5432, "orders", "shipping_ro", "verify-full", "/certs/ca.pem"),
        connection("ift2", "orders", "billing", "ift2-db.example", 6432, "orders_ift", "billing", "require"),
    ]


def mismatches(written: Mapping[str, Any], dumped: Mapping[str, Any]) -> list[str]:
    """Where the dump differs from the file: a line per field, each naming its server.

    pgAdmin numbers the servers its own way and adds fields of its own, so a server is found by its
    name and only the fields the export wrote are compared; of ConnectionParameters, only its keys.
    """
    held = {server["Name"]: server for server in dumped["Servers"].values()}
    found: list[str] = []
    for server in written["Servers"].values():
        name = server["Name"]
        back = held.get(name)
        if back is None:
            found.append(f"{name}: not among the servers pgAdmin holds")
            continue
        for key, value in server.items():
            got = back.get(key)
            if isinstance(value, dict) and isinstance(got, dict):
                got = {k: got.get(k) for k in value}
            if got != value:
                found.append(f"{name}: {key} is {got!r}, the export wrote {value!r}")
    return found


def _write(servers: Path, passfile: str) -> int:
    exports = exports_module()
    items = connections(exports)
    text: str = exports.pgadmin(items, passfile)
    servers.write_text(text, encoding="utf-8")
    print(f"{servers}: {len(items)} servers, from {exports.__name__}")
    return 0


def _compare(servers: Path, dumped: Path) -> int:
    written = _load(servers)
    found = mismatches(written, _load(dumped))
    for problem in found:
        print(problem, file=sys.stderr)
    if found:
        return 1
    print(f"pgAdmin holds the {len(written['Servers'])} servers as written")
    return 0


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The pgAdmin job of tests.yml: write servers.json, compare the dump.")
    commands = parser.add_subparsers(dest="command", required=True)
    write = commands.add_parser("write", help="write servers.json for the connections of the job")
    write.add_argument("servers", type=Path, help="where to write it")
    write.add_argument("passfile", help="the password file the servers name, as a path inside pgAdmin's container")
    compare = commands.add_parser("compare", help="check that every field came back from pgAdmin as written")
    compare.add_argument("servers", type=Path, help="the file write made")
    compare.add_argument("dumped", type=Path, help="what setup.py dump-servers gave back")
    args = parser.parse_args(argv)
    if args.command == "write":
        return _write(args.servers, args.passfile)
    return _compare(args.servers, args.dumped)


if __name__ == "__main__":
    sys.exit(main())
