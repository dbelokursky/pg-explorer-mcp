#!/usr/bin/env python3
"""What a release needs checked: the version, the release notes and the archive.

    python3 scripts/release.py version [--tag v0.4.0]   the version, once every file that holds it agrees
    python3 scripts/release.py notes                    the CHANGELOG.md section of that version
    python3 scripts/release.py check pg-mcp-0.4.0.zip   unpack the archive and start the server from it

.github/workflows/release.yml runs these when a tag is pushed. The version lives in pyproject.toml,
src/pg_mcp/__init__.py, both extension manifests and the first versioned heading of CHANGELOG.md
(tests/test_version.py keeps them in step), and the tag must be v<version>. The check does what a
user without git does: unpacks the archive on its own, away from the checkout, and runs
`install.py --check` from the folder against a port where nothing listens. The server must start
from the archive's copy of the code, name that folder as its home and report the database as
unavailable, the way tests/test_install.py expects of a checkout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The archive unpacks into this folder whatever the version: an update replaces the folder, and the
# path in the agent settings and the extension link stay valid.
FOLDER = "pg-mcp"
# Without these a user cannot install, run or link the extension; the rest of the tree is git's business.
REQUIRED = (
    "run.py",
    "install.py",
    "src/pg_mcp/__init__.py",
    "src/pg_mcp/_vendor/pg8000/__init__.py",
    "scripts/check_vendor.py",
    "gigacode-extension.json",
    "qwen-extension.json",
    "pg-mcp-context.md",
    "commands/pg/setup.md",
    "README.md",
    "README.ru.md",
    "docs/role.sql",
)
NOT_FOR_USERS = (".git", ".github")  # git archive never adds the first; .gitattributes marks the second export-ignore
TIMEOUT_S = 120


def versions(root: Path = ROOT) -> dict[str, str]:
    """The version each file declares; for CHANGELOG.md, its first versioned heading."""
    found = {
        "pyproject.toml": _first(root / "pyproject.toml", r'^version = "([^"]+)"'),
        "src/pg_mcp/__init__.py": _first(root / "src" / "pg_mcp" / "__init__.py", r'^__version__ = "([^"]+)"'),
        "CHANGELOG.md": _first(root / "CHANGELOG.md", r"^## (\d+\.\d+\.\d+)(?=\s)"),
    }
    for manifest in ("gigacode-extension.json", "qwen-extension.json"):
        found[manifest] = str(json.loads((root / manifest).read_text(encoding="utf-8"))["version"])
    return found


def _first(path: Path, pattern: str) -> str:
    match = re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    if match is None:
        raise ValueError(f"no version in {path}")
    return match.group(1)


def notes(version: str, changelog: Path = ROOT / "CHANGELOG.md") -> str | None:
    """The CHANGELOG.md section of the version without its heading, or None when there is none."""
    text = changelog.read_text(encoding="utf-8")
    section = re.search(rf"^## {re.escape(version)}(?=\s).*?\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    return None if section is None else section.group(1).strip() + "\n"


def check(archive: Path) -> list[str]:
    """What is wrong with the archive; empty when a user without git can install from it."""
    with zipfile.ZipFile(archive) as zipped:
        names = zipped.namelist()
        folders = sorted({name.partition("/")[0] for name in names})
        if folders != [FOLDER]:
            return [f"the archive must unpack into the folder {FOLDER}/ alone, not {', '.join(folders)}"]
        inside = [name.partition("/")[2] for name in names]
        problems = [f"{path} is missing from the archive" for path in REQUIRED if path not in inside]
        problems += [
            f"{name}/ is not for users and must stay out of the archive"
            for name in NOT_FOR_USERS
            if any(path.partition("/")[0] == name for path in inside)
        ]
        labelled = re.fullmatch(r"pg-mcp-(.+)\.zip", archive.name)
        if labelled and "qwen-extension.json" in inside:
            version = json.loads(zipped.read(f"{FOLDER}/qwen-extension.json"))["version"]
            if version != labelled.group(1):
                problems.append(f"{archive.name} holds version {version}")
        if problems:
            return problems
        with tempfile.TemporaryDirectory(prefix="pg-mcp-release-") as temporary:
            zipped.extractall(temporary)
            folder = Path(temporary, FOLDER).resolve()
            return starts(folder) + vendor_intact(folder)


def starts(folder: Path) -> list[str]:
    """Run install.py --check from the folder the way a user does, against a port where nothing listens."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    environment.update(
        PGHOST="127.0.0.1",
        PGPORT=str(_free_port()),
        PGUSER="nobody",
        PGDATABASE="nothing",
        PGSSLMODE="disable",
        PGPASSFILE=str(folder / "no-such-pgpass"),
        PYTHONIOENCODING="utf-8",  # the status names the folder, whose path may hold any characters
    )
    proc = _run([sys.executable, str(folder / "install.py"), "--check", "--lang", "en"], environment, folder.parent)
    # The server must run from the archive's copy of the code, call the folder its home and look for the database.
    expected = ("protocol 2025-06-18", f"installed at {folder}", "database: unavailable")
    problems = [f"install.py --check did not say {text!r}" for text in expected if text not in proc.stdout]
    if proc.returncode != 1:
        problems.append(f"install.py --check exited with {proc.returncode}, not 1 for an unreachable database")
    if "Traceback" in proc.stdout + proc.stderr:
        problems.append("install.py --check printed a traceback")
    return problems


def vendor_intact(folder: Path) -> list[str]:
    """git archive converts line endings where attributes ask; the vendored packages must come out untouched."""
    proc = _run([sys.executable, str(folder / "scripts" / "check_vendor.py")], dict(os.environ), folder.parent)
    return [] if proc.returncode == 0 else ["the vendored packages in the archive differ from their wheels"]


def _run(command: list[str], environment: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    print("$", " ".join(command))
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_S,
        env=environment,
        cwd=cwd,
        check=False,
    )
    output = (proc.stdout + proc.stderr).strip()
    if output:
        print(output)
    return proc


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _version(tag: str | None, root: Path) -> int:
    found = versions(root)
    version = found["pyproject.toml"]
    problems = [
        f"{name} says {other}, pyproject.toml says {version}" for name, other in found.items() if other != version
    ]
    if tag is not None and tag != f"v{version}":
        problems.append(f"the tag is {tag}, the files say v{version}")
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print(version)
    return 0


def _notes(root: Path) -> int:
    version = versions(root)["pyproject.toml"]
    text = notes(version, root / "CHANGELOG.md")
    if text is None:
        print(f"CHANGELOG.md has no section for {version}", file=sys.stderr)
        return 1
    print(text, end="")
    return 0


def _check(archive: Path) -> int:
    problems = check(archive)
    for problem in problems:
        print(f"FAIL: {problem}")
    if not problems:
        print(f"OK: {archive.name} unpacks into {FOLDER}/ and the server starts from there")
    return 1 if problems else 0


def main(argv: Sequence[str] | None = None, root: Path = ROOT) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")  # paths in the output may not fit a narrow console code page
    parser = argparse.ArgumentParser(description="Check what a release needs: the version, the notes and the archive.")
    commands = parser.add_subparsers(dest="command", required=True)
    version_command = commands.add_parser("version", help="print the version once every file that holds it agrees")
    version_command.add_argument("--tag", help="the release tag, which must be v<version>")
    commands.add_parser("notes", help="print the CHANGELOG.md section of the version")
    check_command = commands.add_parser("check", help="unpack the archive and start the server from it")
    check_command.add_argument("archive", type=Path)
    args = parser.parse_args(argv)
    if args.command == "version":
        return _version(args.tag, root)
    if args.command == "notes":
        return _notes(root)
    return _check(args.archive)


if __name__ == "__main__":
    sys.exit(main())
