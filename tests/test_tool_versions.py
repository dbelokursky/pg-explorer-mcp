"""The pre-commit hook and CI take ruff from two files; this keeps the two the same version."""

from __future__ import annotations

import re

import release

HOOK = release.ROOT / ".pre-commit-config.yaml"
LOCK = release.ROOT / "uv.lock"


def locked_version(package: str) -> str:
    """The version uv.lock holds for the package, whatever Python it is for."""
    name = re.compile(rf'^name = "{re.escape(package)}"$', flags=re.MULTILINE)
    for block in LOCK.read_text(encoding="utf-8").split("[[package]]")[1:]:
        if name.search(block):
            found = re.search(r'^version = "([^"]+)"$', block, flags=re.MULTILINE)
            assert found is not None, block
            return found.group(1)
    raise AssertionError(f"{package} is not in uv.lock")


def test_the_ruff_hook_of_pre_commit_is_the_ruff_of_the_lock() -> None:
    hook = HOOK.read_text(encoding="utf-8")
    rev = re.search(r"^\s+rev: v(\S+)\s+# the same version uv.lock pins", hook, flags=re.MULTILINE)
    assert rev is not None, "the ruff hook of .pre-commit-config.yaml names no version"
    assert rev.group(1) == locked_version("ruff")
