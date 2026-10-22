#!/usr/bin/env python3
"""Check that server/vendor holds exactly the files its wheels shipped.

Every vendored package keeps the RECORD file of its wheel, which lists each
file with its SHA-256. A missing or changed file means the vendored code was
edited by hand or updated halfway: refresh it from PyPI as server/vendor/README.md
describes. Run: python3 scripts/check_vendor.py
"""
from __future__ import annotations

import base64
import csv
import glob
import hashlib
import os
import sys
from typing import List

VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server", "vendor")


def problems(vendor: str = VENDOR) -> List[str]:
    records = sorted(glob.glob(os.path.join(vendor, "*.dist-info", "RECORD")))
    if not records:
        return [f"no *.dist-info/RECORD files in {vendor}"]
    found = []
    for record in records:
        with open(record, newline="", encoding="utf-8") as handle:
            for row in csv.reader(handle):
                if len(row) < 2 or not row[1]:
                    continue  # RECORD lists itself without a hash
                path, (algorithm, _, expected) = row[0], row[1].partition("=")
                try:
                    with open(os.path.join(vendor, *path.split("/")), "rb") as vendored:
                        digest = hashlib.new(algorithm, vendored.read()).digest()
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
    print(f"{len(found)} problem(s) in server/vendor" if found else "server/vendor matches its wheels")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
