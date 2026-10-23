"""Export of stand connections to database tools: the formats, the files, no passwords anywhere."""

from __future__ import annotations

import dataclasses
import json
import shutil
import stat
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from pg_mcp import exports
from pg_mcp.exports import Connection, Export, ExportRefusedError
from tests.test_imports import dbeaver_workspace, postgres

BILLING = Connection("dev7", "orders", "billing", "dev7-db.example", 5432, "orders", "billing", None)
SHIPPING = Connection("dev7", "orders", "shipping", "dev7-db.example", 5432, "orders", "shipping_ro", "require")
IFT = Connection("ift2", "orders", "billing", "ift2-db.example", 6432, "orders_ift", "billing", None)
ITEMS = [BILLING, SHIPPING, IFT]
VERIFIED = Connection(
    "dev9", "orders", "billing", "h", 5432, "orders", "billing", "verify-full", "/etc/my certs/ca.pem"
)


def registry(tmp_path: Path, text: str, service: str | None = None) -> dict[str, str]:
    """PG_MCP_STANDS_FILE with this text and PGSERVICEFILE with the other, if any."""
    path = tmp_path / "stands.conf"
    path.write_text(text, encoding="utf-8")
    service_path = tmp_path / "service.conf"
    if service is not None:
        service_path.write_text(service, encoding="utf-8")
    return {"PG_MCP_STANDS_FILE": str(path), "PGSERVICEFILE": str(service_path)}


def parse(text: str) -> ET.Element:
    return ET.fromstring(text)  # noqa: S314  (the text of the test itself)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home folder where desktop applications keep their settings as on macOS."""
    monkeypatch.setattr(sys, "platform", "darwin")
    return tmp_path


class TestConnections:
    def test_the_registry_defaults_are_spelled_out(self, tmp_path: Path) -> None:
        env = registry(
            tmp_path,
            "[dev7.orders.billing]\nhost=dev7-db.example\n\n"
            "[ift2.orders.billing]\nhost=ift2-db.example\nport=6432\ndbname=orders_ift\nsslmode=require\n",
        )
        items, warnings = exports.connections(env)
        assert items == [
            BILLING,
            Connection("ift2", "orders", "billing", "ift2-db.example", 6432, "orders_ift", "billing", "require"),
        ]
        assert warnings == []
        assert items[0].source == str(tmp_path / "stands.conf")  # where it comes from, not part of equality

    def test_patterns_choose_the_stands_in_any_case(self, tmp_path: Path) -> None:
        env = registry(tmp_path, "[dev7.orders.billing]\nhost=a\n\n[ift2.orders.billing]\nhost=b\n")
        assert [item.stand for item in exports.connections(env, ["IFT*"])[0]] == ["ift2"]
        assert [item.stand for item in exports.connections(env, [" ", ""])[0]] == ["dev7", "ift2"]

    def test_no_stands_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ExportRefusedError, match="no stands to export"):
            exports.connections(registry(tmp_path, ""))

    def test_a_pattern_that_matches_nothing_names_the_stands_there_are(self, tmp_path: Path) -> None:
        env = registry(tmp_path, "[dev7.orders.billing]\nhost=a\n\n[dev8.orders.billing]\nhost=b\n")
        with pytest.raises(ExportRefusedError, match=r"no stands match qa\*, uat; the registry has dev7–dev8"):
            exports.connections(env, ["qa*", "uat"])

    def test_the_problems_of_the_registry_come_along(self, tmp_path: Path) -> None:
        env = registry(tmp_path, "[dev7.orders.billing]\nhost=a\n\n[dev7.orders.broken]\nport=1\n")
        items, warnings = exports.connections(env)
        assert [item.schema for item in items] == ["billing"]
        assert any("dev7.orders.broken" in warning for warning in warnings)

    def test_ids_are_stable_so_a_new_export_updates_the_same_connection(self) -> None:
        assert BILLING.uid() == Connection(*[*BILLING.__dict__.values()]).uid()
        assert BILLING.uid() != SHIPPING.uid()

    def test_the_jdbc_url_carries_the_tls_settings_and_the_schema_when_asked(self) -> None:
        assert exports.jdbc_url(BILLING) == "jdbc:postgresql://dev7-db.example:5432/orders"
        assert exports.jdbc_url(SHIPPING) == "jdbc:postgresql://dev7-db.example:5432/orders?sslmode=require"
        assert exports.jdbc_url(SHIPPING, current_schema=True) == (
            "jdbc:postgresql://dev7-db.example:5432/orders?currentSchema=shipping,public&sslmode=require"
        )
        assert exports.jdbc_url(SHIPPING, tls=False) == "jdbc:postgresql://dev7-db.example:5432/orders"
        assert exports.jdbc_url(VERIFIED).endswith("?sslmode=verify-full&sslrootcert=/etc/my%20certs/ca.pem")
        windows = dataclasses.replace(VERIFIED, sslrootcert="C:\\certs\\ca+1.pem")
        assert exports.jdbc_url(windows).endswith("&sslrootcert=C:%5Ccerts%5Cca%2B1.pem")  # pgjdbc decodes it

    def test_the_ca_certificate_of_a_stand_comes_along(self, tmp_path: Path) -> None:
        env = registry(
            tmp_path, "[dev9.orders.billing]\nhost=h\nsslmode=verify-full\nsslrootcert=/etc/my certs/ca.pem\n"
        )
        assert exports.connections(env)[0] == [VERIFIED]


class TestPgService:
    def test_sections_for_psql_under_the_export_line_with_the_schema_first(self) -> None:
        assert exports.pg_service([BILLING, SHIPPING, VERIFIED], "") == (
            "# pg-mcp export\n[dev7.orders.billing]\nhost=dev7-db.example\nport=5432\ndbname=orders\nuser=billing\n"
            "options=-c search_path=billing,public\n"
            "\n# pg-mcp export\n[dev7.orders.shipping]\nhost=dev7-db.example\nport=5432\ndbname=orders\n"
            "user=shipping_ro\nsslmode=require\noptions=-c search_path=shipping,public\n"
            "\n# pg-mcp export\n[dev9.orders.billing]\nhost=h\nport=5432\ndbname=orders\nuser=billing\n"
            "sslmode=verify-full\nsslrootcert=/etc/my certs/ca.pem\noptions=-c search_path=billing,public\n"
        )

    def test_an_earlier_export_is_replaced_and_everything_else_stays(self) -> None:
        earlier = exports.pg_service(
            [dataclasses.replace(BILLING, host="old"), IFT], "# my services\n[reports]\nhost=rep\n"
        )
        earlier += "\n# the reporting replica\n[replica]\nhost=replica\n"  # the user's, after the export
        assert exports.pg_service([BILLING], earlier) == (
            "# my services\n[reports]\nhost=rep\n"
            "\n# pg-mcp export\n[ift2.orders.billing]\nhost=ift2-db.example\nport=6432\ndbname=orders_ift\n"
            "user=billing\noptions=-c search_path=billing,public\n"
            "\n# the reporting replica\n[replica]\nhost=replica\n"
            "\n# pg-mcp export\n[dev7.orders.billing]\nhost=dev7-db.example\nport=5432\ndbname=orders\n"
            "user=billing\noptions=-c search_path=billing,public\n"
        )
        assert exports.pg_service([BILLING], "[reports]\nhost=rep\n\n\n").startswith(
            "[reports]\nhost=rep\n\n# pg-mcp export\n[dev7.orders.billing]\n"  # one empty line, however many before
        )

    def test_the_export_line_tells_the_sections_apart(self) -> None:
        text = (
            "[DEV7.orders.billing]\nhost=mine\n\n# pg-mcp export\n[dev8.orders.billing]\nhost=h\n"
            "\n# pg-mcp export\n\n[dev9.orders.billing]\nhost=h\n[reports]\nhost=r\n"
        )
        assert exports.service_sections(text) == {
            "dev7.orders.billing": False,  # the user's, whatever the case of the stand
            "dev8.orders.billing": True,
            "dev9.orders.billing": False,  # the line has to be right above the section
            "reports": False,
        }

    def test_a_section_the_user_wrote_is_never_touched(self, tmp_path: Path) -> None:
        mine = "[dev7.orders.billing]\nhost=dev7-db.example\nsslrootcert=/etc/ca.pem\napplication_name=me\n"
        stands = (
            "[dev7.orders.billing]\nhost=dev7-db.example\n\n"
            "[ift2.orders.billing]\nhost=ift2-db.example\nport=6432\ndbname=orders_ift\n"
        )
        env = registry(tmp_path, stands, mine)
        items, _ = exports.connections(env)  # dev7 comes from stands.conf, which the registry reads first
        export = exports.prepare("pg-service", items, env, tmp_path)
        assert export.items == [IFT]
        assert export.notes == [f"left out, already in {tmp_path / 'service.conf'}: 1"]
        [(_, text)] = export.files
        assert text.startswith(mine + "\n# pg-mcp export\n[ift2.orders.billing]\n")

    def test_the_sections_read_from_the_service_file_itself_are_left_out(self, tmp_path: Path) -> None:
        env = registry(
            tmp_path, "[dev7.orders.billing]\nhost=dev7-db.example\n", "# mine\n[dev8.crm.sales]\nhost=dev8-db\n"
        )
        items, _ = exports.connections(env)
        export = exports.prepare("pg-service", items, env, tmp_path)
        service = tmp_path / "service.conf"
        assert export.items == [BILLING]
        assert export.notes == [f"left out, already in {service}: 1"]
        [(path, text)] = export.files
        assert path == service
        assert text.startswith("# mine\n[dev8.crm.sales]\nhost=dev8-db\n\n# pg-mcp export\n[dev7.orders.billing]\n")
        assert export.next_step.startswith('Connect with psql "service=dev7.orders.billing"')

    def test_the_registry_does_not_take_exported_sections_for_stands(self, tmp_path: Path) -> None:
        env = registry(tmp_path, "[dev7.orders.billing]\nhost=dev7-db.example\n")
        items, _ = exports.connections(env)
        exports.write(exports.prepare("pg-service", items, env, tmp_path))
        (tmp_path / "stands.conf").write_text("", encoding="utf-8")  # the stand is gone from the registry
        with pytest.raises(ExportRefusedError, match="no stands to export"):
            exports.connections(env)

    def test_nothing_is_written_when_every_section_is_there_already(self, tmp_path: Path) -> None:
        env = registry(tmp_path, "", "[dev8.crm.sales]\nhost=dev8-db\n")
        items, _ = exports.connections(env)
        export = exports.prepare("pg-service", items, env, tmp_path)
        assert export.files == []
        assert exports.preview(export) == [
            f"left out, already in {tmp_path / 'service.conf'}: 1",
            "psql, pgcli (pg_service.conf) has all of these connections already; nothing to write",
        ]

    def test_a_service_file_that_cannot_be_read_is_refused(self, tmp_path: Path) -> None:
        env = registry(tmp_path, "[dev7.orders.billing]\nhost=a\n")
        (tmp_path / "service.conf").write_bytes(b"\xff\xfe not utf-8")
        items, _ = exports.connections(env)  # the registry only warns about it
        with pytest.raises(ExportRefusedError, match="cannot read"):
            exports.prepare("pg-service", items, env, tmp_path)


class TestDBeaver:
    def test_a_folder_per_stand_pgpass_and_the_schema_by_default(self) -> None:
        data = json.loads(exports.dbeaver(ITEMS))
        assert data["folders"] == {"dev7": {}, "ift2": {}}
        connection = data["connections"][f"postgres-jdbc-pg-mcp-{BILLING.uid()}"]
        assert (connection["name"], connection["folder"], connection["save-password"]) == (
            "dev7 / orders / billing",
            "dev7",
            False,
        )
        configuration = connection["configuration"]
        assert configuration["auth-model"] == "postgres_pgpass"
        assert (configuration["host"], configuration["port"], configuration["database"]) == (
            "dev7-db.example",
            "5432",
            "orders",
        )
        assert configuration["user"] == "billing"
        assert configuration["bootstrap"] == {"defaultSchema": "billing"}
        assert '"password":' not in json.dumps(data)  # only "save-password": false

    def test_tls_goes_into_the_driver_properties(self) -> None:
        connections = json.loads(exports.dbeaver([SHIPPING, VERIFIED, BILLING]))["connections"]
        shipping = connections[f"postgres-jdbc-pg-mcp-{SHIPPING.uid()}"]["configuration"]
        assert shipping["url"] == "jdbc:postgresql://dev7-db.example:5432/orders"  # DBeaver builds its own
        assert shipping["properties"] == {"sslmode": "require"}
        verified = connections[f"postgres-jdbc-pg-mcp-{VERIFIED.uid()}"]["configuration"]
        assert verified["properties"] == {"sslmode": "verify-full", "sslrootcert": "/etc/my certs/ca.pem"}
        assert "properties" not in connections[f"postgres-jdbc-pg-mcp-{BILLING.uid()}"]["configuration"]

    def test_an_unknown_user_stands_for_a_login_only_when_there_is_one(self, tmp_path: Path) -> None:
        folder = dbeaver_workspace(tmp_path / ".dbeaver", {"pg-1": postgres("dev7", "dev7-db.example", "orders")}, {})
        export = exports.prepare("dbeaver", [BILLING, SHIPPING], {}, tmp_path, workspace=folder)
        assert (export.items, export.notes) == ([BILLING, SHIPPING], [])  # two logins: which one would it be?
        assert exports.prepare("dbeaver", [BILLING], {}, tmp_path, workspace=folder).items == []

    def test_an_earlier_export_keeps_what_this_one_does_not_bring(self) -> None:
        previous = json.loads(exports.dbeaver([IFT, BILLING]))
        previous["connection-types"] = {"dev": {"name": "Development"}}  # DBeaver may save more blocks
        previous["connections"][f"postgres-jdbc-pg-mcp-{BILLING.uid()}"]["name"] = "renamed in DBeaver"
        data = json.loads(exports.dbeaver([BILLING, SHIPPING], previous))
        assert list(data["connections"]) == [
            f"postgres-jdbc-pg-mcp-{IFT.uid()}",
            f"postgres-jdbc-pg-mcp-{BILLING.uid()}",
            f"postgres-jdbc-pg-mcp-{SHIPPING.uid()}",
        ]
        assert data["connections"][f"postgres-jdbc-pg-mcp-{BILLING.uid()}"]["name"] == "dev7 / orders / billing"
        assert data["folders"] == {"ift2": {}, "dev7": {}}
        assert data["connection-types"] == {"dev": {"name": "Development"}}

    def test_what_dbeaver_has_of_its_own_is_left_out(self, tmp_path: Path) -> None:
        folder = dbeaver_workspace(
            tmp_path / ".dbeaver",
            {
                "pg-1": postgres("orders", "DEV7-DB.example", "orders"),
                "pg-2": postgres("ift", "ift2-db.example", "orders_ift", port="6432"),  # its user is not known
                "pg-3": postgres("other user", "dev7-db.example", "orders", user="admin"),
            },
            {"pg-1": {"user": "billing", "password": "s3cret"}},
        )
        export = exports.prepare("dbeaver", ITEMS, {}, tmp_path, workspace=folder)
        assert export.items == [SHIPPING]
        assert export.notes == ["left out, already in DBeaver with the same host, port, database and user: 2"]
        [(path, text)] = export.files
        assert path == folder / exports.DBEAVER_FILE
        assert list(json.loads(text)["connections"]) == [f"postgres-jdbc-pg-mcp-{SHIPPING.uid()}"]
        assert "s3cret" not in text
        assert export.next_step.startswith("Restart DBeaver")

    def test_dbeaver_with_every_connection_gets_nothing(self, tmp_path: Path) -> None:
        folder = dbeaver_workspace(
            tmp_path / ".dbeaver", {"pg-1": postgres("orders", "dev7-db.example", "orders", user="billing")}, {}
        )
        export = exports.prepare("dbeaver", [BILLING], {}, tmp_path, workspace=folder)
        assert export.files == []
        assert exports.preview(export)[-1] == "DBeaver has all of these connections already; nothing to write"

    def test_a_workspace_without_connections_is_still_a_workspace(self, tmp_path: Path) -> None:
        export = exports.prepare("dbeaver", [BILLING], {}, tmp_path, workspace=tmp_path)
        assert export.items == [BILLING]
        assert export.files[0][0] == tmp_path / exports.DBEAVER_FILE

    def test_dbeaver_needs_its_folder(self, tmp_path: Path) -> None:
        with pytest.raises(ExportRefusedError, match="is not a folder"):
            exports.prepare("dbeaver", ITEMS, {}, tmp_path, workspace=tmp_path / "missing")
        with pytest.raises(ExportRefusedError, match="DBeaver's connections were not found"):
            exports.prepare("dbeaver", ITEMS, {}, tmp_path)

    def test_the_workspace_is_found_where_dbeaver_keeps_it(self, home: Path) -> None:
        folder = dbeaver_workspace(home / "Library" / "DBeaverData" / "workspace6" / "General" / ".dbeaver", {}, {})
        export = exports.prepare("dbeaver", [BILLING], {}, home)
        assert export.files[0][0] == folder / exports.DBEAVER_FILE


class TestDataGrip:
    def test_the_project_files_carry_the_group_pgpass_and_the_schema(self) -> None:
        shared, local = exports.datagrip([SHIPPING])
        source = parse(shared).find("component/data-source")
        assert source is not None
        assert (source.get("name"), source.get("group"), source.get("uuid")) == (
            "dev7 / orders / shipping",
            "dev7",
            SHIPPING.uid(),
        )
        assert source.findtext("driver-ref") == "postgresql"
        assert source.findtext("jdbc-url") == exports.jdbc_url(SHIPPING, current_schema=True)
        login = parse(local).find("component/data-source")
        assert login is not None
        assert login.get("uuid") == SHIPPING.uid()
        assert (login.findtext("auth-provider"), login.findtext("user-name")) == ("pgpass", "shipping_ro")
        scope = login.find("schema-mapping/introspection-scope/node")
        assert scope is not None
        assert scope.get("qname") == "@"
        assert [node.get("qname") for node in scope] == ["shipping"]

    def test_names_are_escaped_for_xml(self) -> None:
        odd = Connection("dev7", "orders", "billing", "h", 5432, "a&b", "u<1>", None)
        shared, local = exports.datagrip([odd])
        url = parse(shared).findtext("component/data-source/jdbc-url")
        assert url == "jdbc:postgresql://h:5432/a&b?currentSchema=billing,public"
        assert parse(local).findtext("component/data-source/user-name") == "u<1>"

    def test_a_project_keeps_its_other_data_sources_and_comments(self, tmp_path: Path) -> None:
        idea = tmp_path / ".idea"
        idea.mkdir()
        (idea / "dataSources.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<project version="4">\n'
            '  <component name="DataSourceManagerImpl" format="xml" multifile-model="true">\n'
            "    <!-- the team database -->\n"
            '    <data-source source="LOCAL" name="team" uuid="1111">\n'
            "      <driver-ref>postgresql</driver-ref>\n"
            "    </data-source>\n"
            f'    <data-source source="LOCAL" name="old" uuid="{BILLING.uid()}">\n'
            "      <jdbc-url>old</jdbc-url>\n"
            "    </data-source>\n"
            "  </component>\n</project>\n",
            encoding="utf-8",
        )
        export = exports.prepare("datagrip", [BILLING], {}, tmp_path, project=tmp_path)
        (shared_path, shared), (local_path, local) = export.files
        assert (shared_path, local_path) == (idea / "dataSources.xml", idea / "dataSources.local.xml")
        assert "<!-- the team database -->" in shared
        sources = parse(shared).findall("component/data-source")
        assert [source.get("name") for source in sources] == ["team", "dev7 / orders / billing"]
        assert "old" not in shared
        assert local == exports.datagrip([BILLING])[1]  # no local file yet: ours as it is
        assert export.next_step.startswith(f"Open the folder {tmp_path} as a project")

    def test_a_file_without_our_component_gets_it(self, tmp_path: Path) -> None:
        path = tmp_path / "dataSources.local.xml"
        path.write_text('<project version="4"><component name="Other" /></project>', encoding="utf-8")
        merged = parse(exports.merge_xml(path, exports.datagrip([BILLING])[1]))
        assert [component.get("name") for component in merged] == ["Other", "dataSourceStorageLocal"]

    def test_a_broken_project_file_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "dataSources.xml"
        path.write_text("<project", encoding="utf-8")
        with pytest.raises(ExportRefusedError, match="cannot read"):
            exports.merge_xml(path, exports.datagrip([BILLING])[0])

    def test_the_default_project_is_next_to_the_registry(self, tmp_path: Path) -> None:
        env = registry(tmp_path, "")
        export = exports.prepare("datagrip", [BILLING], env, tmp_path)
        assert export.files[0][0] == tmp_path / "datagrip" / ".idea" / "dataSources.xml"


class TestPgAdmin:
    def test_servers_for_import_with_the_password_file_and_the_schema_first(self) -> None:
        data = json.loads(exports.pgadmin([BILLING, SHIPPING], "/home/me/.pgpass"))
        assert data["Servers"]["2"] == {
            "Name": "dev7 / orders / shipping",
            "Group": "dev7",
            "Host": "dev7-db.example",
            "Port": 5432,
            "MaintenanceDB": "orders",
            "Username": "shipping_ro",
            "DBRestriction": "orders",
            "DBRestrictionType": "databases",
            "PostConnectionSQL": 'SET search_path TO "shipping", public',
            "ConnectionParameters": {"sslmode": "require", "connect_timeout": 10, "passfile": "/home/me/.pgpass"},
        }
        assert data["Servers"]["1"]["ConnectionParameters"]["sslmode"] == "prefer"

    def test_the_ca_certificate_goes_into_the_connection_parameters(self) -> None:
        parameters = json.loads(exports.pgadmin([VERIFIED], "/p"))["Servers"]["1"]["ConnectionParameters"]
        assert parameters == {
            "sslmode": "verify-full",
            "connect_timeout": 10,
            "passfile": "/p",
            "sslrootcert": "/etc/my certs/ca.pem",
        }

    def test_a_quote_in_a_schema_name_is_doubled(self) -> None:
        assert exports.quote_ident('we"ird') == '"we""ird"'

    def test_the_file_goes_next_to_the_registry_and_names_the_password_file_in_full(self, tmp_path: Path) -> None:
        env = {**registry(tmp_path, ""), "PGPASSFILE": str(tmp_path / "secrets" / ".." / "pgpass")}
        export = exports.prepare("pgadmin", [BILLING], env, tmp_path)
        [(path, text)] = export.files
        passfile = str((tmp_path / "pgpass").resolve())
        assert path == tmp_path / "pgadmin-servers.json"
        assert json.loads(text)["Servers"]["1"]["ConnectionParameters"]["passfile"] == passfile
        assert str(path) in export.next_step
        assert passfile in export.next_step


class TestEditors:
    def test_postgresql_extension_connections_replace_ours_by_id_and_keep_the_rest(self) -> None:
        old = {"id": f"pg-mcp-{BILLING.uid()}", "server": "old"}
        settings: dict[str, Any] = {
            "editor.fontSize": 13,
            "pgsql.serverGroups": [{"id": "mine", "name": "Mine"}, {"id": "pg-mcp-dev7", "name": "old"}, "junk"],
            "pgsql.connections": [
                {"id": "mine-1", "profileName": "local"},
                old,
                {"id": f"pg-mcp-{IFT.uid()}", "profileName": "kept from an earlier export"},
            ],
        }
        result = exports.vscode_pgsql([BILLING, SHIPPING], settings)
        assert result["editor.fontSize"] == 13
        assert result["pgsql.serverGroups"] == [{"id": "mine", "name": "Mine"}, {"id": "pg-mcp-dev7", "name": "dev7"}]
        assert [entry["id"] for entry in result["pgsql.connections"]] == [
            "mine-1",
            f"pg-mcp-{IFT.uid()}",
            f"pg-mcp-{BILLING.uid()}",
            f"pg-mcp-{SHIPPING.uid()}",
        ]
        assert result["pgsql.connections"][-1] == {
            "id": f"pg-mcp-{SHIPPING.uid()}",
            "profileName": "dev7 / orders / shipping",
            "groupId": "pg-mcp-dev7",
            "server": "dev7-db.example",
            "port": "5432",
            "database": "orders",
            "user": "shipping_ro",
            "authenticationType": "SqlLogin",
            "options": "-c search_path=shipping,public",
            "sslmode": "require",
        }
        assert settings["pgsql.connections"][1] is old  # the settings given are not changed
        assert old == {"id": f"pg-mcp-{BILLING.uid()}", "server": "old"}

    def test_sqltools_connections_replace_the_same_names_and_choose_tls(self) -> None:
        verified = dataclasses.replace(VERIFIED, stand="dev10", sslrootcert=None)
        settings = {"sqltools.connections": [{"name": "local", "driver": "SQLite"}, {"name": BILLING.label}]}
        result = exports.sqltools([BILLING, SHIPPING, verified, VERIFIED], settings)
        connections = result["sqltools.connections"]
        assert [entry["name"] for entry in connections] == [
            "local",
            BILLING.label,
            SHIPPING.label,
            verified.label,
            VERIFIED.label,
        ]
        assert connections[1] == {
            "name": "dev7 / orders / billing",
            "group": "dev7",
            "driver": "PostgreSQL",
            "server": "dev7-db.example",
            "port": 5432,
            "database": "orders",
            "username": "billing",
            "password": "",
            "connectionTimeout": 15,
            "previewLimit": 50,
        }
        assert connections[2]["pgOptions"] == {"ssl": {"rejectUnauthorized": False}}
        assert connections[3]["pgOptions"] == {"ssl": True}
        assert connections[4]["pgOptions"] == {"ssl": {"rejectUnauthorized": True, "ca": "/etc/my certs/ca.pem"}}

    def test_the_postgresql_extension_gets_the_ca_certificate(self) -> None:
        [entry] = exports.vscode_pgsql([VERIFIED], {})["pgsql.connections"]
        assert (entry["sslmode"], entry["sslrootcert"]) == ("verify-full", "/etc/my certs/ca.pem")

    def test_a_setting_that_is_not_a_list_is_refused(self) -> None:
        with pytest.raises(ExportRefusedError, match=r"pgsql\.connections in the editor's settings is not a list"):
            exports.vscode_pgsql([BILLING], {"pgsql.connections": {}})

    def test_settings_are_read_only_when_they_are_plain_json(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        assert exports.read_settings(path) == {}
        for text, expected in [
            ("  \n", {}),
            ('{"a": 1}', {"a": 1}),
            ('{\n  // mine\n  "a": 1\n}', None),
            ("[1]", None),
        ]:
            path.write_text(text, encoding="utf-8")
            assert exports.read_settings(path) == expected
        with pytest.raises(ExportRefusedError, match="cannot read"):
            exports.read_settings(tmp_path)  # a folder

    def test_the_settings_folder_that_exists_wins(self, home: Path) -> None:
        support = home / "Library" / "Application Support"
        assert exports.editor_settings({}, home, "windsurf") == support / "Windsurf" / "User" / "settings.json"
        (support / "Devin" / "User").mkdir(parents=True)
        assert exports.editor_settings({}, home, "windsurf") == support / "Devin" / "User" / "settings.json"
        assert exports.editor_settings({}, home, "cursor") == support / "Cursor" / "User" / "settings.json"

    def test_an_editor_needs_its_settings_folder_and_plain_json(self, home: Path) -> None:
        user = home / "Library" / "Application Support" / "Code" / "User"
        with pytest.raises(ExportRefusedError, match="the settings of vscode were not found"):
            exports.prepare("sqltools", [BILLING], {}, home)
        user.mkdir(parents=True)
        (user / "settings.json").write_text('{\n  // mine\n  "editor.fontSize": 13\n}\n', encoding="utf-8")
        with pytest.raises(ExportRefusedError) as caught:
            exports.prepare("vscode-pgsql", [BILLING], {}, home)
        message = str(caught.value)
        assert message.startswith(f"{user / 'settings.json'} is not plain JSON")
        assert json.loads(message.partition("\n")[2])["pgsql.connections"][0]["profileName"] == BILLING.label
        (user / "settings.json").write_text('{"editor.fontSize": 13}', encoding="utf-8")
        export = exports.prepare("sqltools", [BILLING], {}, home)
        [(path, text)] = export.files
        assert path == user / "settings.json"
        assert json.loads(text)["editor.fontSize"] == 13
        assert export.next_step.startswith("Reload the editor window: the connections are in SQLTools")


class TestPreviewAndWrite:
    def test_the_preview_counts_and_names_the_stands(self) -> None:
        path = Path("servers.json")
        assert exports.preview(Export("pgadmin", [IFT], [(path, "")], "")) == [
            "pgAdmin 4: 1 connection (ift2) goes into:",
            f"  {path}",
        ]
        assert exports.preview(Export("pgadmin", ITEMS, [(path, "")], ""))[0] == (
            "pgAdmin 4: 3 connections (dev7, ift2) go into:"
        )

    @pytest.mark.usefixtures("russian")
    @pytest.mark.parametrize(("count", "words"), [(1, "1 подключение"), (3, "3 подключения"), (5, "5 подключений")])
    def test_the_preview_counts_in_russian(self, count: int, words: str) -> None:
        export = Export("pgadmin", [BILLING] * count, [(Path("servers.json"), "")], "")
        assert exports.preview(export)[0].startswith(f"pgAdmin 4: {words} (dev7)")

    def test_write_keeps_a_copy_of_the_old_file_and_its_permissions(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "pg_service.conf"
        assert exports.write(Export("pg-service", [], [(path, "one\n")], "next step")) == [
            f"written {path}",
            "next step",
        ]
        posix = sys.platform != "win32"
        if posix:
            assert stat.S_IMODE(path.stat().st_mode) == 0o600  # a new file is its owner's
            path.chmod(0o640)
        lines = exports.write(Export("pg-service", [], [(path, "two\n")], "next step"))
        [backup] = path.parent.glob("pg_service.conf.bak-*")
        assert lines[0] == f"written {path}; the old file is kept as {backup}"
        assert (path.read_text(encoding="utf-8"), backup.read_text(encoding="utf-8")) == ("two\n", "one\n")
        if posix:
            assert stat.S_IMODE(path.stat().st_mode) == stat.S_IMODE(backup.stat().st_mode) == 0o640
        assert sorted(child.name for child in path.parent.iterdir()) == ["pg_service.conf", backup.name]

    @pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need extra rights on Windows")
    def test_a_link_stays_a_link_to_the_changed_file(self, tmp_path: Path) -> None:
        dotfiles = tmp_path / "dotfiles"
        dotfiles.mkdir()
        (dotfiles / "settings.json").write_text("{}\n", encoding="utf-8")
        link = tmp_path / "settings.json"
        link.symlink_to(dotfiles / "settings.json")
        lines = exports.write(Export("sqltools", [], [(link, '{"a": 1}\n')], "next step"))
        [backup] = dotfiles.glob("settings.json.bak-*")
        assert link.is_symlink()
        assert (dotfiles / "settings.json").read_text(encoding="utf-8") == '{"a": 1}\n'
        assert not backup.is_symlink()
        assert backup.read_text(encoding="utf-8") == "{}\n"
        assert lines[0] == f"written {link}; the old file is kept as {backup}"

    def test_two_writes_within_a_second_keep_both_copies(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(exports, "_now", lambda: "20261023-120000")
        path = tmp_path / "settings.json"
        path.write_text("original\n", encoding="utf-8")
        exports.write(Export("vscode-pgsql", [], [(path, "first\n")], ""))
        exports.write(Export("sqltools", [], [(path, "second\n")], ""))
        assert (tmp_path / "settings.json.bak-20261023-120000").read_text(encoding="utf-8") == "original\n"
        assert (tmp_path / "settings.json.bak-20261023-120000-2").read_text(encoding="utf-8") == "first\n"
        assert path.read_text(encoding="utf-8") == "second\n"

    def test_a_new_text_that_cannot_be_written_changes_nothing(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        path.write_text("old\n", encoding="utf-8")
        (tmp_path / "settings.json.pg-mcp-new").mkdir()  # where the new text would go
        with pytest.raises(ExportRefusedError, match="cannot write"):
            exports.write(Export("sqltools", [], [(path, "new\n")], ""))
        assert path.read_text(encoding="utf-8") == "old\n"
        assert sorted(child.name for child in tmp_path.iterdir()) == ["settings.json", "settings.json.pg-mcp-new"]

    def test_a_file_that_cannot_be_replaced_stays_as_it_was(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first, second = tmp_path / "dataSources.xml", tmp_path / "dataSources.local.xml"
        second.write_text("old\n", encoding="utf-8")
        replace = Path.replace

        def busy(self: Path, target: Path) -> Path:
            if Path(target) == second and self.name.endswith(".pg-mcp-new"):
                raise PermissionError("the file is in use")
            return replace(self, target)

        monkeypatch.setattr(Path, "replace", busy)
        with pytest.raises(ExportRefusedError) as caught:
            exports.write(Export("datagrip", [], [(first, "one\n"), (second, "two\n")], "next step"))
        assert str(caught.value).splitlines() == [
            f"written {first}",
            f"cannot replace {second}: the file is in use. It is left as it was.",
        ]
        assert second.read_text(encoding="utf-8") == "old\n"
        assert sorted(child.name for child in tmp_path.iterdir()) == ["dataSources.local.xml", "dataSources.xml"]

    def test_a_file_that_cannot_be_written_changes_nothing(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("a file where a folder should be", encoding="utf-8")
        export = Export("datagrip", [], [(tmp_path / "first.xml", "one"), (blocker / "second.xml", "two")], "")
        with pytest.raises(ExportRefusedError, match="cannot write"):
            exports.write(export)
        assert [child.name for child in tmp_path.iterdir()] == ["blocker"]


def test_the_tools_are_told_by_their_folders_and_programs(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)  # noqa: ARG005
    assert exports.installed({}, home) == []
    support = home / "Library" / "Application Support"
    dbeaver_workspace(home / "Library" / "DBeaverData" / "workspace6" / "General" / ".dbeaver", {}, {})
    (support / "JetBrains" / "IdeaIC2025.2").mkdir(parents=True)  # the Community edition has no database tools
    (home / ".pgadmin").mkdir()
    (home / ".cursor" / "extensions" / "mtxr.sqltools-0.28.4").mkdir(parents=True)
    (home / ".vscode" / "extensions" / "ms-ossdata.vscode-pgsql-1.4.0").mkdir(parents=True)
    assert exports.installed({}, home) == [
        ("dbeaver", ""),
        ("pgadmin", ""),
        ("vscode-pgsql", "vscode"),
        ("sqltools", "cursor"),
    ]
    (support / "JetBrains" / "DataGrip2025.2").mkdir()
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/pgcli" if name == "pgcli" else None)
    assert [tool for tool, _ in exports.installed({}, home)] == [
        "dbeaver",
        "datagrip",
        "pgadmin",
        "pg-service",
        "vscode-pgsql",
        "sqltools",
    ]
