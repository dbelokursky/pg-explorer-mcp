"""scripts/pgadmin_check.py: the pgAdmin job of tests.yml, its servers.json and the comparison with pgAdmin's dump."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pgadmin_check
import pytest

from tests.helpers import ROOT

SCRIPT = ROOT / "scripts" / "pgadmin_check.py"
PASSFILE = "/var/lib/pgadmin/pgpass"
NAMES = ["dev7 / orders / billing", "dev7 / orders / shipping", "ift2 / orders / billing"]


@pytest.fixture
def servers(tmp_path: Path) -> Path:
    """servers.json as the write command makes it."""
    path = tmp_path / "servers.json"
    assert pgadmin_check.main(["write", str(path), PASSFILE]) == 0
    return path


def load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def as_pgadmin_dumps(written: dict[str, Any]) -> dict[str, Any]:
    """The written servers the way a dump may hold them: another numbering and order, fields on top of ours."""
    dumped = {}
    for number, server in enumerate(reversed(list(written["Servers"].values())), start=11):
        parameters = {**server["ConnectionParameters"], "application_name": "pgAdmin 4"}
        dumped[str(number)] = {**server, "Shared": False, "Timeout": 10, "ConnectionParameters": parameters}
    return {"Servers": dumped}


def test_write_makes_three_servers_in_two_groups_with_the_password_file(servers: Path) -> None:
    data = load(servers)["Servers"]
    assert [server["Name"] for server in data.values()] == NAMES
    assert {server["Group"] for server in data.values()} == {"dev7", "ift2"}  # "2 Server Group(s) and 3 Server(s)"
    assert [server["ConnectionParameters"]["passfile"] for server in data.values()] == [PASSFILE] * 3
    assert data["1"]["ConnectionParameters"]["sslmode"] == "prefer"
    assert data["2"]["ConnectionParameters"] == {
        "sslmode": "verify-full",
        "connect_timeout": 10,
        "passfile": PASSFILE,
        "sslrootcert": "/certs/ca.pem",
    }
    assert (data["3"]["Host"], data["3"]["Port"], data["3"]["ConnectionParameters"]["sslmode"]) == (
        "ift2-db.example",
        6432,
        "require",
    )


def test_write_says_where_the_file_went_and_which_package_made_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "servers.json"
    assert pgadmin_check.main(["write", str(path), PASSFILE]) == 0
    assert capsys.readouterr().out == f"{path}: 3 servers, from {pgadmin_check.exports_module().__name__}\n"


def test_the_file_is_the_export_itself(servers: Path) -> None:
    exports = pgadmin_check.exports_module()
    assert servers.read_text(encoding="utf-8") == exports.pgadmin(pgadmin_check.connections(exports), PASSFILE)


def test_the_package_is_found_under_its_other_name_too(monkeypatch: pytest.MonkeyPatch) -> None:
    exports = pgadmin_check.exports_module()
    package = exports.__name__.partition(".")[0]
    assert package in pgadmin_check.PACKAGES
    monkeypatch.setattr(pgadmin_check, "PACKAGES", ("no_such_package", package))
    assert pgadmin_check.exports_module() is exports


def test_a_package_that_is_there_but_lacks_the_module_is_not_passed_over(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pgadmin_check, "PACKAGES", ("json", "no_such_package"))
    with pytest.raises(ModuleNotFoundError, match=r"json\.exports"):
        pgadmin_check.exports_module()
    monkeypatch.setattr(pgadmin_check, "PACKAGES", ("no_such_package",))
    with pytest.raises(ModuleNotFoundError, match=r"no package no_such_package on sys\.path"):
        pgadmin_check.exports_module()


def test_compare_passes_when_every_field_came_back(
    servers: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dumped = tmp_path / "dumped.json"
    dumped.write_text(json.dumps(as_pgadmin_dumps(load(servers))), encoding="utf-8")
    assert pgadmin_check.main(["compare", str(servers), str(dumped)]) == 0
    assert capsys.readouterr() == ("pgAdmin holds the 3 servers as written\n", "")


def test_compare_names_every_field_that_came_back_changed(
    servers: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = as_pgadmin_dumps(load(servers))
    by_name = {server["Name"]: server for server in data["Servers"].values()}
    by_name[NAMES[0]]["Port"] = 5433
    by_name[NAMES[1]]["ConnectionParameters"]["sslmode"] = "prefer"
    del by_name[NAMES[1]]["ConnectionParameters"]["sslrootcert"]
    data["Servers"] = {number: server for number, server in data["Servers"].items() if server["Name"] != NAMES[2]}
    dumped = tmp_path / "dumped.json"
    dumped.write_text(json.dumps(data), encoding="utf-8")
    assert pgadmin_check.main(["compare", str(servers), str(dumped)]) == 1
    got = {"sslmode": "prefer", "connect_timeout": 10, "passfile": PASSFILE, "sslrootcert": None}
    wrote = {"sslmode": "verify-full", "connect_timeout": 10, "passfile": PASSFILE, "sslrootcert": "/certs/ca.pem"}
    assert capsys.readouterr() == (
        "",
        f"{NAMES[0]}: Port is 5433, the export wrote 5432\n"
        f"{NAMES[1]}: ConnectionParameters is {got!r}, the export wrote {wrote!r}\n"
        f"{NAMES[2]}: not among the servers pgAdmin holds\n",
    )


@pytest.mark.parametrize(
    ("back", "expected"),
    [
        ({"Name": "s", "Port": 1, "ConnectionParameters": {"sslmode": "require", "passfile": "/p"}}, []),
        (
            {"Name": "s", "Port": 2, "ConnectionParameters": {"sslmode": "require"}},
            ["s: Port is 2, the export wrote 1"],
        ),
        (
            {"Name": "s", "Port": 1, "ConnectionParameters": {}},
            ["s: ConnectionParameters is {'sslmode': None}, the export wrote {'sslmode': 'require'}"],
        ),
        ({"Name": "s", "Port": 1}, ["s: ConnectionParameters is None, the export wrote {'sslmode': 'require'}"]),
    ],
)
def test_only_the_fields_written_are_compared(back: dict[str, Any], expected: list[str]) -> None:
    written = {"Servers": {"1": {"Name": "s", "Port": 1, "ConnectionParameters": {"sslmode": "require"}}}}
    assert pgadmin_check.mismatches(written, {"Servers": {"7": back}}) == expected


def test_both_commands_run_on_a_bare_python_from_the_checkout(tmp_path: Path) -> None:
    """The job runs compare with the runner's python3, outside uv: the script finds the package in src/ by itself."""
    python = [sys.executable, "-I", "-S", str(SCRIPT)]  # -S: no site-packages, so the venv's copy of the package is out
    written = subprocess.run(
        [*python, "write", "servers.json", PASSFILE],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert (written.returncode, written.stderr) == (0, "")
    assert written.stdout.startswith("servers.json: 3 servers, from ")
    shutil.copy(tmp_path / "servers.json", tmp_path / "dumped.json")
    compared = subprocess.run(
        [*python, "compare", "servers.json", "dumped.json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert (compared.returncode, compared.stdout, compared.stderr) == (
        0,
        "pgAdmin holds the 3 servers as written\n",
        "",
    )


def test_the_job_runs_the_script_and_holds_no_python_of_its_own() -> None:
    """What the script is for: a line of tests.yml naming the package would break the night run, which takes the
    workflow from main and the code from develop."""
    workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert "uv run python scripts/pgadmin_check.py write servers.json /var/lib/pgadmin/pgpass" in workflow
    assert "python3 scripts/pgadmin_check.py compare servers.json dumped.json" in workflow
    assert "python - <<" not in workflow
    assert ".exports" not in workflow
