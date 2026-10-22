#!/usr/bin/env python3
"""Check that src/pg_mcp/_vendor holds exactly the files its wheels shipped.

Every vendored package keeps the RECORD file of its wheel, which lists each
file with its SHA-256. A missing or changed file means the vendored code was
edited by hand or updated halfway: refresh it from PyPI as
src/pg_mcp/_vendor/README.md describes. Run: python3 scripts/check_vendor.py
"""

from __future__ import annotations

import base64
import csv
import hashlib
import sys
from pathlib import Path

VENDOR = Path(__file__).resolve().parents[1] / "src" / "pg_mcp" / "_vendor"


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
    return found


def main() -> int:
    found = problems()
    for problem in found:
        print(problem)
    print(f"{len(found)} problem(s) in src/pg_mcp/_vendor" if found else "src/pg_mcp/_vendor matches its wheels")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
