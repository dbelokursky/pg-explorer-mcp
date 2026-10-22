"""The version lives in several files; scripts/release.py reads them all, and this keeps them in step."""

from __future__ import annotations

import release

import pg_mcp


def test_every_file_declares_the_version_of_the_package_and_the_changelog_describes_it() -> None:
    found = release.versions()
    assert set(found.values()) == {pg_mcp.__version__}, found
    assert release.notes(pg_mcp.__version__) is not None
