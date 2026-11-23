"""Two READMEs, English and Russian, each kept whole by hand: this keeps their shape the same."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import release

ENGLISH = release.ROOT / "README.md"
RUSSIAN = release.ROOT / "README.ru.md"


def numbered(path: Path, pattern: str) -> int:
    return len(re.findall(pattern, path.read_text(encoding="utf-8"), flags=re.MULTILINE))


@pytest.mark.parametrize(
    ("pattern", "what"),
    [
        (r"^- \[[^\]]+\]\(#[^)]+\)$", "sections of the table of contents"),
        (r"^## .+$", "sections"),
        (r"^### .+$", "subsections"),
    ],
)
def test_both_readmes_have_as_many(pattern: str, what: str) -> None:
    """The Russian contents went without its diagnostics entry for a release: nothing compared the two."""
    assert numbered(ENGLISH, pattern) == numbered(RUSSIAN, pattern), what
