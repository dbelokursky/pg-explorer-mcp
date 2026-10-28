from __future__ import annotations

import re
import shutil
from pathlib import Path

import check_vendor
import pytest

from tests.helpers import ROOT

# GitHub's dependency graph skips manifests in folders with these names (its docs, "Dependency graph errors").
GITHUB_SKIPS = (r"(3rd|[Tt]hird)[-_]?[Pp]arty/", r"(^|/)vendors?/", r"(^|/)[Ee]xtern(als?)?/", r"(^|/)[Vv]+endor/")


@pytest.fixture
def vendor(tmp_path: Path) -> Path:
    """The vendored packages in a folder of their own, for a test to spoil."""
    folder = tmp_path / "vendor"
    shutil.copytree(check_vendor.VENDOR, folder, ignore=shutil.ignore_patterns("__pycache__"))
    return folder


def test_vendored_files_match_the_wheels_they_came_from() -> None:
    assert check_vendor.problems() == []


def test_edited_and_missing_files_are_reported(vendor: Path) -> None:
    with (vendor / "six.py").open("a", encoding="utf-8") as handle:
        handle.write("\n# edited by hand\n")
    (vendor / "scramp" / "core.py").unlink()
    assert check_vendor.problems(vendor) == ["missing: scramp/core.py", "changed: six.py"]


@pytest.mark.parametrize(
    ("line", "replacement", "expected"),
    [
        ("six==1.17.0", "", ["requirements.txt lacks six==1.17.0"]),
        ("six==1.17.0", "six==1.17.0\nidna==3.10", ["requirements.txt pins idna==3.10, which is not vendored"]),
        ("scramp==1.4.6", "scramp==1.4.7", ["requirements.txt pins scramp==1.4.7, the vendored one is 1.4.6"]),
        ("six==1.17.0", "six==1.17.0\nsix==1.16.0", ["requirements.txt pins six twice"]),
        (
            "six==1.17.0",
            "six>=1.16",
            ["requirements.txt: six>=1.16 is not name==version", "requirements.txt lacks six==1.17.0"],
        ),
        # Names compare the way PyPI compares them; comments and blank lines are welcome.
        ("python-dateutil==2.9.0.post0", "Python_DateUtil==2.9.0.post0", []),
        ("six==1.17.0", "\nsix==1.17.0  # for python-dateutil", []),
    ],
)
def test_the_manifest_must_agree_with_the_dist_info(
    vendor: Path, line: str, replacement: str, expected: list[str]
) -> None:
    manifest = vendor / check_vendor.MANIFEST
    lines = manifest.read_text(encoding="utf-8").splitlines()
    lines[lines.index(line)] = replacement
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert check_vendor.problems(vendor) == expected


def test_a_missing_manifest_is_reported(vendor: Path) -> None:
    (vendor / check_vendor.MANIFEST).unlink()
    assert check_vendor.problems(vendor) == ["missing: requirements.txt"]


def test_github_reads_the_manifest_where_it_lies() -> None:
    # _vendor/ escapes the patterns; moved to vendor/ or third_party/, the packages would silently lose their alerts.
    path = (check_vendor.VENDOR / check_vendor.MANIFEST).relative_to(ROOT).as_posix()
    assert [pattern for pattern in GITHUB_SKIPS if re.search(pattern, path)] == []


def test_an_empty_folder_is_reported(tmp_path: Path) -> None:
    assert check_vendor.problems(tmp_path)[0].startswith("no *.dist-info/RECORD files")


def test_script_exit_code() -> None:
    assert check_vendor.main() == 0
