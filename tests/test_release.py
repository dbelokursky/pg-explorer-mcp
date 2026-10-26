"""scripts/release.py: the version in every file, the release notes and the archive for users without git."""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
import release

import pg_explorer_mcp
from tests.helpers import ROOT

VERSION = pg_explorer_mcp.__version__


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The archive release.yml builds: HEAD's tree in a pg-mcp/ folder, minus what .gitattributes export-ignores."""
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("needs git and a checkout of the repository")
    path = tmp_path_factory.mktemp("release") / f"pg-mcp-{VERSION}.zip"
    subprocess.run(
        ["git", "-C", str(ROOT), "archive", "--format=zip", "--prefix=pg-mcp/", f"--output={path}", "HEAD"],
        check=True,
        timeout=120,
    )
    return path


def fake_root(tmp_path: Path, changelog_version: str) -> Path:
    """A tree that declares version 1.2.0 everywhere; the CHANGELOG's first section has the version asked for."""
    (tmp_path / "src" / "pg_explorer_mcp").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "pg-mcp"\nversion = "1.2.0"\n', encoding="utf-8")
    (tmp_path / "src" / "pg_explorer_mcp" / "__init__.py").write_text('__version__ = "1.2.0"\n', encoding="utf-8")
    for manifest in ("gigacode-extension.json", "qwen-extension.json"):
        (tmp_path / manifest).write_text('{"name": "pg-mcp", "version": "1.2.0"}\n', encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        f"# Изменения\n\n## Не выпущено\n\n- later\n\n## {changelog_version} — 2026-01-01\n\n- one\n- two\n\n"
        "## 1.0.0 — 2025-12-01\n\n- old\n",
        encoding="utf-8",
    )
    return tmp_path


def fake_archive(path: Path, install_py: str) -> Path:
    """An archive with every required file, all empty but install.py and the manifests."""
    with zipfile.ZipFile(path, "w") as zipped:
        for name in release.REQUIRED:
            content = install_py if name == "install.py" else '{"version": "1.2.0"}' if name.endswith(".json") else ""
            zipped.writestr(f"pg-mcp/{name}", content)
    return path


def test_the_version_is_read_from_every_file(tmp_path: Path) -> None:
    assert release.versions(fake_root(tmp_path, "1.2.0")) == {
        "pyproject.toml": "1.2.0",
        "src/pg_explorer_mcp/__init__.py": "1.2.0",
        "CHANGELOG.md": "1.2.0",
        "gigacode-extension.json": "1.2.0",
        "qwen-extension.json": "1.2.0",
    }


def test_the_notes_are_one_section_without_its_heading(tmp_path: Path) -> None:
    changelog = fake_root(tmp_path, "1.2.0") / "CHANGELOG.md"
    assert release.notes("1.2.0", changelog) == "- one\n- two\n"
    assert release.notes("1.0.0", changelog) == "- old\n"
    assert release.notes("1.2", changelog) is None


def test_the_version_command_prints_the_version_when_the_files_and_the_tag_agree(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert release.main(["version", "--tag", f"v{VERSION}"]) == 0
    assert capsys.readouterr().out == f"{VERSION}\n"


def test_the_version_command_refuses_a_tag_of_another_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert release.main(["version", "--tag", "v0.0.1"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"the tag is v0.0.1, the files say v{VERSION}" in captured.err


def test_the_version_command_names_the_file_that_disagrees(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert release.main(["version"], fake_root(tmp_path, "1.1.0")) == 1
    assert "CHANGELOG.md says 1.1.0, pyproject.toml says 1.2.0" in capsys.readouterr().err


def test_the_notes_command_prints_the_section_of_the_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert release.main(["notes"], fake_root(tmp_path, "1.2.0")) == 0
    assert capsys.readouterr().out == "- one\n- two\n"


def test_the_notes_command_fails_without_a_section_for_the_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert release.main(["notes"], fake_root(tmp_path, "1.1.0")) == 1
    assert "CHANGELOG.md has no section for 1.2.0" in capsys.readouterr().err


def test_the_archive_unpacks_into_one_folder_and_the_server_starts_from_it(
    archive: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert release.main(["check", str(archive)]) == 0
    assert f"OK: {archive.name} unpacks into pg-mcp/" in capsys.readouterr().out


def test_a_server_that_does_not_start_from_the_archive_is_reported(tmp_path: Path) -> None:
    problems = release.check(fake_archive(tmp_path / "pg-mcp-1.2.0.zip", "print('protocol 2025-06-18')\n"))
    assert "install.py --check did not say 'protocol 2025-06-18'" not in problems
    assert "install.py --check did not say 'database: unavailable'" in problems
    assert "install.py --check exited with 0, not 1 for an unreachable database" in problems


def test_a_traceback_from_the_archive_is_reported(tmp_path: Path) -> None:
    assert "install.py --check printed a traceback" in release.check(
        fake_archive(tmp_path / "pg-mcp-1.2.0.zip", "1 / 0\n")
    )


def test_an_archive_with_the_wrong_contents_is_refused_before_anything_runs(tmp_path: Path) -> None:
    archive = tmp_path / "pg-mcp-9.9.9.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("pg-mcp/.github/workflows/tests.yml", "")
        zipped.writestr("pg-mcp/qwen-extension.json", '{"version": "1.2.0"}')
        zipped.writestr("pg-mcp/run.py", "")
    problems = release.check(archive)
    assert "install.py is missing from the archive" in problems
    assert "run.py is missing from the archive" not in problems
    assert ".github/ is not for users and must stay out of the archive" in problems
    assert "pg-mcp-9.9.9.zip holds version 1.2.0" in problems


def test_an_archive_that_does_not_unpack_into_pg_mcp_is_refused(tmp_path: Path) -> None:
    archive = tmp_path / "loose.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("run.py", "")
        zipped.writestr("pg-mcp-1.2.0/install.py", "")
    assert release.check(archive) == ["the archive must unpack into the folder pg-mcp/ alone, not pg-mcp-1.2.0, run.py"]
