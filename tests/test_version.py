"""The version lives in several files; scripts/release.py reads them all, and this keeps them in step."""

from __future__ import annotations

import release

import pg_explorer_mcp


def test_every_file_declares_the_version_of_the_package_and_the_changelog_describes_it() -> None:
    found = release.versions()
    assert set(found.values()) == {pg_explorer_mcp.__version__}, found
    assert release.notes(pg_explorer_mcp.__version__) is not None


def test_the_status_samples_of_both_readmes_show_the_version_of_the_package() -> None:
    """The samples are real answers; left alone, they showed 0.8.0 two releases later."""
    shown = f"pg-explorer-mcp {pg_explorer_mcp.__version__} "
    for name in ("README.md", "README.ru.md"):
        assert shown in (release.ROOT / name).read_text(encoding="utf-8"), name
