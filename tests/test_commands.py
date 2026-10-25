"""The extension's slash commands: what their descriptions tell the model they are for."""

from __future__ import annotations

from tests.helpers import ROOT


def description(command: str) -> str:
    front = (ROOT / "commands" / "pg" / f"{command}.md").read_text(encoding="utf-8").split("---")[1]
    return front.split("description:", 1)[1].strip()


def test_setup_leaves_checking_the_connection_to_status() -> None:
    """Told that /pg:setup checks the connection, a model asked to check it ran no tool at all."""
    assert description("setup") == (
        "Install or repair pg-mcp when its tools are missing or status reports the database unavailable; "
        "to check the connection, call the status tool instead"
    )
