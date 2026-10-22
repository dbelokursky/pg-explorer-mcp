"""The version lives in several files; this keeps them in step."""

from __future__ import annotations

import json
import re

import pg_mcp
from tests.helpers import ROOT


def declared_version() -> str:
    match = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.MULTILINE)
    assert match is not None
    return match.group(1)


def test_package_manifests_and_changelog_agree_with_pyproject() -> None:
    version = declared_version()
    assert pg_mcp.__version__ == version
    for manifest in ("gigacode-extension.json", "qwen-extension.json"):
        assert json.loads((ROOT / manifest).read_text(encoding="utf-8"))["version"] == version, manifest
    assert f"## {version} " in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
