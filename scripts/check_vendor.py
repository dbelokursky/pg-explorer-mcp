#!/usr/bin/env python3
"""Check that src/pg_explorer_mcp/_vendor holds exactly the files its wheels shipped.

Every vendored package keeps the RECORD file of its wheel, which lists each
file with its SHA-256. A missing or changed file means the vendored code was
edited by hand or updated halfway: refresh it from PyPI as
src/pg_explorer_mcp/_vendor/README.md describes. The requirements.txt next to them must
pin every vendored package, and nothing else, to the version in its
*.dist-info: GitHub's dependency graph reads it, and Dependabot alerts are only
as right as it is. Run: python3 scripts/check_vendor.py
"""

from __future__ import annotations

import base64
import csv
import hashlib
import re
import sys
from email.parser import HeaderParser
from pathlib import Path

VENDOR = Path(__file__).resolve().parents[1] / "src" / "pg_explorer_mcp" / "_vendor"
# The list pip keeps as src/pip/_vendor/vendor.txt, under the name GitHub's dependency graph reads for pip.
MANIFEST = "requirements.txt"
PIN = re.compile(r"(?P<name>[A-Za-z0-9][\w.-]*)==(?P<version>[\w.!+-]+)")


def problems(vendor: Path = VENDOR) -> list[str]:
    records = sorted(vendor.glob("*.dist-info/RECORD"))
    if not records:
        return [f"no *.dist-info/RECORD files in {vendor}"]
    found = []
    for record in records:
        with record.open(newline="", encoding="utf-8") as handle:
            for row in csv.reader(handle):
                if len(row) < 2 or not row[1]:
                    continue  # RECORD lists itself without a hash
                path, (algorithm, _, expected) = row[0], row[1].partition("=")
                try:
                    digest = hashlib.new(algorithm, (vendor / path).read_bytes()).digest()
                except OSError:
                    found.append(f"missing: {path}")
                    continue
                if base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii") != expected:
                    found.append(f"changed: {path}")
    return found + _manifest_problems(vendor)


def _manifest_problems(vendor: Path) -> list[str]:
    """Where requirements.txt and the *.dist-info folders disagree on what is vendored."""
    try:
        lines = (vendor / MANIFEST).read_text(encoding="utf-8").splitlines()
    except OSError:
        return [f"missing: {MANIFEST}"]
    found = []
    pins: dict[str, re.Match[str]] = {}
    for line in lines:
        requirement = line.partition("#")[0].strip()
        if not requirement:
            continue
        match = PIN.fullmatch(requirement)
        if match is None:
            found.append(f"{MANIFEST}: {requirement} is not name==version")
        elif _canonical(match.group("name")) in pins:
            found.append(f"{MANIFEST} pins {match.group('name')} twice")
        else:
            pins[_canonical(match.group("name"))] = match
    for metadata in sorted(vendor.glob("*.dist-info/METADATA")):
        headers = HeaderParser().parsestr(metadata.read_text(encoding="utf-8"))
        name, version = headers["Name"], headers["Version"]
        pin = pins.pop(_canonical(name), None)
        if pin is None:
            found.append(f"{MANIFEST} lacks {name}=={version}")
        elif pin.group("version") != version:
            found.append(f"{MANIFEST} pins {pin.group()}, the vendored one is {version}")
    found += [f"{MANIFEST} pins {pin.group()}, which is not vendored" for pin in pins.values()]
    return found


def _canonical(name: str) -> str:
    """The name as PyPI compares it (PEP 503): python_dateutil and Python-DateUtil are python-dateutil."""
    return re.sub(r"[-_.]+", "-", name).lower()


def main() -> int:
    found = problems()
    for problem in found:
        print(problem)
    where = VENDOR.relative_to(VENDOR.parents[2]).as_posix()
    print(f"{len(found)} problem(s) in {where}" if found else f"{where} matches its wheels")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
