from __future__ import annotations

import shutil
from pathlib import Path

import check_vendor


def test_vendored_files_match_the_wheels_they_came_from() -> None:
    assert check_vendor.problems() == []


def test_edited_and_missing_files_are_reported(tmp_path: Path) -> None:
    copy = tmp_path / "vendor"
    shutil.copytree(check_vendor.VENDOR, copy, ignore=shutil.ignore_patterns("__pycache__"))
    with (copy / "six.py").open("a", encoding="utf-8") as handle:
        handle.write("\n# edited by hand\n")
    (copy / "scramp" / "core.py").unlink()
    assert check_vendor.problems(copy) == ["missing: scramp/core.py", "changed: six.py"]


def test_an_empty_folder_is_reported(tmp_path: Path) -> None:
    assert check_vendor.problems(tmp_path)[0].startswith("no *.dist-info/RECORD files")


def test_script_exit_code() -> None:
    assert check_vendor.main() == 0
