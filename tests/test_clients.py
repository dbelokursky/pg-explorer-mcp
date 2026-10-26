"""MCP clients: where each keeps its servers, how it wraps the entry, and writing into its file."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from pg_explorer_mcp import clients
from pg_explorer_mcp.clients import CLIENTS, ClientConfigError

ENTRY = {
    "command": "/usr/bin/python3",
    "args": ["-I", "/opt/pg-explorer-mcp/run.py"],
    "timeout": 30000,
    "env": {
        "PGHOST": "db.example",
        "PG_EXPLORER_MCP_NAME": "pg",
        "PG_EXPLORER_MCP_LANG": "ru",
        "PGAPPNAME": 'quote " and кириллица',
    },
}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(sys, "platform", "darwin")
    return tmp_path


class TestPlaces:
    @pytest.mark.parametrize(
        ("platform", "env", "expected"),
        [
            ("win32", {"APPDATA": "/roaming"}, Path("/roaming")),
            ("win32", {}, Path("/home/me/AppData/Roaming")),
            ("darwin", {}, Path("/home/me/Library/Application Support")),
            ("linux", {"XDG_CONFIG_HOME": "/xdg"}, Path("/xdg")),
            ("linux", {}, Path("/home/me/.config")),
        ],
    )
    def test_app_data_follows_the_system(
        self, monkeypatch: pytest.MonkeyPatch, platform: str, env: dict[str, str], expected: Path
    ) -> None:
        monkeypatch.setattr(sys, "platform", platform)
        assert clients.app_data(env, Path("/home/me")) == expected

    def test_every_client_knows_its_file(self, home: Path) -> None:
        support = home / "Library" / "Application Support"
        places = {key: client.path({}, home, "pg") for key, client in CLIENTS.items()}
        assert places == {
            "gigacode": home / ".gigacode" / "settings.json",
            "qwen": home / ".qwen" / "settings.json",
            "vscode": support / "Code" / "User" / "mcp.json",
            "cursor": home / ".cursor" / "mcp.json",
            "windsurf": home / ".config" / "devin" / "mcp_config.json",
            "cline": home / ".cline" / "data" / "settings" / "cline_mcp_settings.json",
            "roo": support / "Code/User/globalStorage/rooveterinaryinc.roo-cline/settings/mcp_settings.json",
            "continue": home / ".continue" / "mcpServers" / "pg.yaml",
            "lmstudio": home / ".lmstudio" / "mcp.json",
            "claude-desktop": support / "Claude" / "claude_desktop_config.json",
            "junie": home / ".junie" / "mcp" / "mcp.json",
            "jetbrains": None,
        }

    def test_windsurf_writes_where_its_file_already_is(self, home: Path) -> None:
        windsurf = CLIENTS["windsurf"]
        legacy = home / ".codeium" / "windsurf" / "mcp_config.json"
        legacy.parent.mkdir(parents=True)
        assert windsurf.path({}, home, "pg") == legacy  # only the old folder: the old file
        newest = home / ".config" / "devin" / "mcp_config.json"
        newest.parent.mkdir(parents=True)
        assert windsurf.path({}, home, "pg") == newest  # the new folder wins over the old one
        legacy.write_text("{}", encoding="utf-8")
        assert windsurf.path({}, home, "pg") == legacy  # but a file in use wins over a folder

    def test_cline_is_found_by_its_new_folder_or_the_old_extension_storage(self, home: Path) -> None:
        cline = CLIENTS["cline"]
        assert not cline.present({}, home)
        (home / "Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev").mkdir(parents=True)
        assert cline.present({}, home)
        assert cline.path({}, home, "pg") == home / ".cline" / "data" / "settings" / "cline_mcp_settings.json"

    def test_installed_clients_are_told_by_their_folders(self, home: Path) -> None:
        assert clients.installed({}, home) == []
        (home / ".cursor").mkdir()
        (home / "Library" / "Application Support" / "Claude").mkdir(parents=True)
        (home / ".qwen").mkdir()
        assert [client.key for client in clients.installed({}, home)] == ["qwen", "cursor", "claude-desktop"]
        assert [client.key for client in clients.installed({}, home, besides="qwen")] == ["cursor", "claude-desktop"]


class TestEntry:
    def test_only_the_clients_that_read_milliseconds_get_the_timeout(self) -> None:
        assert clients.server(CLIENTS["gigacode"], ENTRY)["timeout"] == 30000
        assert clients.server(CLIENTS["junie"], ENTRY)["timeout"] == 30000
        assert "timeout" not in clients.server(CLIENTS["cline"], ENTRY)  # Cline counts seconds
        assert "timeout" not in clients.server(CLIENTS["cursor"], ENTRY)

    def test_vscode_wants_a_type_and_cline_and_roo_an_enabled_flag(self) -> None:
        assert list(clients.server(CLIENTS["vscode"], ENTRY))[:2] == ["type", "command"]
        assert clients.server(CLIENTS["vscode"], ENTRY)["type"] == "stdio"
        assert clients.server(CLIENTS["cline"], ENTRY)["disabled"] is False
        assert clients.server(CLIENTS["roo"], ENTRY)["disabled"] is False

    def test_snippets_wrap_the_entry_as_each_client_does(self) -> None:
        lmstudio = json.loads(clients.snippet(CLIENTS["lmstudio"], "pg", ENTRY))
        assert lmstudio == {"mcpServers": {"pg": {key: ENTRY[key] for key in ("command", "args", "env")}}}
        assert json.loads(clients.snippet(CLIENTS["cursor"], "pg", ENTRY))["mcpServers"]["pg"]["type"] == "stdio"
        vscode = json.loads(clients.snippet(CLIENTS["vscode"], "pg-stands", ENTRY))
        assert vscode["servers"]["pg-stands"]["type"] == "stdio"

    def test_the_continue_block_is_yaml_with_json_strings(self) -> None:
        assert clients.snippet(CLIENTS["continue"], "pg", ENTRY) == (
            'name: "pg"\n'
            "version: 0.0.1\n"
            "schema: v1\n"
            "mcpServers:\n"
            '  - name: "pg"\n'
            "    type: stdio\n"
            '    command: "/usr/bin/python3"\n'
            "    args:\n"
            '      - "-I"\n'
            '      - "/opt/pg-explorer-mcp/run.py"\n'
            "    env:\n"
            '      "PGHOST": "db.example"\n'
            '      "PG_EXPLORER_MCP_NAME": "pg"\n'
            '      "PG_EXPLORER_MCP_LANG": "ru"\n'
            '      "PGAPPNAME": "quote \\" and кириллица"\n'
        )


class TestWrite:
    def test_a_new_file_is_made_with_its_folders(self, home: Path) -> None:
        client = CLIENTS["claude-desktop"]
        path = client.path({}, home, "pg")
        assert path is not None
        assert clients.write(client, path, "pg", ENTRY) is None
        assert json.loads(path.read_text(encoding="utf-8")) == {"mcpServers": {"pg": clients.server(client, ENTRY)}}

    def test_other_servers_and_settings_stay_and_a_copy_is_kept(self, tmp_path: Path) -> None:
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({"theme": "dark", "servers": {"other": {"command": "x"}}}), encoding="utf-8")
        backup = clients.write(CLIENTS["vscode"], path, "pg", ENTRY)
        assert backup is not None
        assert json.loads(backup.read_text(encoding="utf-8"))["servers"] == {"other": {"command": "x"}}
        settings = json.loads(path.read_text(encoding="utf-8"))
        assert settings["theme"] == "dark"
        assert set(settings["servers"]) == {"other", "pg"}

    def test_an_empty_file_is_as_good_as_none(self, tmp_path: Path) -> None:
        path = tmp_path / "mcp.json"
        path.write_text("  \n", encoding="utf-8")
        clients.write(CLIENTS["cursor"], path, "pg", ENTRY)
        assert set(json.loads(path.read_text(encoding="utf-8"))["mcpServers"]) == {"pg"}

    @pytest.mark.parametrize(
        "text",
        ['{\n  // comments are fine for the client, not for json\n  "servers": {}\n}', '{"servers": []}', "[]"],
    )
    def test_a_file_it_cannot_merge_into_is_left_alone(self, tmp_path: Path, text: str) -> None:
        path = tmp_path / "mcp.json"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(ClientConfigError) as caught:
            clients.write(CLIENTS["vscode"], path, "pg", ENTRY)
        assert json.loads(str(caught.value))["servers"]["pg"]["type"] == "stdio"
        assert path.read_text(encoding="utf-8") == text
        assert list(tmp_path.glob("*.bak-*")) == []

    def test_a_continue_block_replaces_the_old_one(self, home: Path) -> None:
        client = CLIENTS["continue"]
        path = client.path({}, home, "pg")
        assert path is not None
        clients.write(client, path, "pg", ENTRY)
        backup = clients.write(client, path, "pg", {**ENTRY, "args": ["-I", "/new/run.py"]})
        assert backup is not None
        assert '      - "/new/run.py"\n' in path.read_text(encoding="utf-8")
        assert '      - "/opt/pg-explorer-mcp/run.py"\n' in backup.read_text(encoding="utf-8")
