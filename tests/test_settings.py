from __future__ import annotations

import errno
import os
import socket
import ssl
import sys
from pathlib import Path

import pg8000.exceptions
import pytest

from pg_mcp import db
from pg_mcp.db import Database, DatabaseUnavailableError, Settings, read_pgpass


def settings(**overrides: str) -> Settings:
    return Settings.from_env({"PGUSER": "app", "PGPASSFILE": os.devnull, **overrides})


class TestSettings:
    def test_libpq_defaults(self) -> None:
        s = settings()
        assert (s.host, s.port, s.database, s.sslmode) == ("localhost", 5432, "app", "prefer")

    def test_bad_value_is_reported_through_the_database_not_at_startup(self) -> None:
        assert "PGPORT='five' is not a number" in Database({"PGPORT": "five", "PGUSER": "app"}).describe_target()

    def test_bad_sslmode_is_explained(self) -> None:
        assert "PGSSLMODE='sometimes'" in Database({"PGSSLMODE": "sometimes"}).describe_target()

    @pytest.mark.parametrize(
        ("socket_s", "statement_ms", "expected"),
        [("10", "5000", 10), ("10", "60000", 65), ("0", "5000", 30), ("30", "0", 30)],
    )
    def test_the_socket_outwaits_the_statement_timeout(self, socket_s: str, statement_ms: str, expected: int) -> None:
        s = settings(PG_MCP_SOCKET_TIMEOUT_S=socket_s, PG_MCP_STATEMENT_TIMEOUT_MS=statement_ms)
        assert db.socket_timeout(s) == expected

    def test_connecting_waits_only_its_own_timeout(self) -> None:
        s = settings(PG_MCP_SOCKET_TIMEOUT_S="1", PG_MCP_STATEMENT_TIMEOUT_MS="5000")
        assert (db.connect_timeout(s), db.socket_timeout(s)) == (1, 10)
        assert db.connect_timeout(settings(PG_MCP_SOCKET_TIMEOUT_S="0")) == 30

    def test_settings_problem_reaches_the_tools_as_a_hint(self) -> None:
        with pytest.raises(DatabaseUnavailableError, match="Connection settings are invalid"):
            Database({"PGPORT": "five"}).server_info()

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [("disable", [False]), ("allow", [False, True]), ("prefer", [None]), ("require", [True])],
    )
    def test_sslmode_maps_to_pg8000_attempts(self, mode: str, expected: list[object]) -> None:
        assert db._ssl_attempts(settings(PGSSLMODE=mode)) == expected

    @pytest.mark.parametrize(("mode", "check_hostname"), [("verify-ca", False), ("verify-full", True)])
    def test_verifying_modes_check_certificates(self, mode: str, check_hostname: bool) -> None:  # noqa: FBT001
        (context,) = db._ssl_attempts(settings(PGSSLMODE=mode))
        assert isinstance(context, ssl.SSLContext)
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is check_hostname


class TestPgpass:
    def write(self, tmp_path: Path, text: str, mode: int = 0o600) -> Path:
        path = tmp_path / ".pgpass"
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)
        return path

    def test_first_matching_line_wins_and_star_matches_anything(self, tmp_path: Path) -> None:
        path = str(self.write(tmp_path, "# comment\n\ndb.local:5432:sales:app:first\n*:*:*:app:second\n"))
        assert read_pgpass(path, "db.local", 5432, "sales", "app") == "first"
        assert read_pgpass(path, "other", 6432, "hr", "app") == "second"
        assert read_pgpass(path, "db.local", 5432, "sales", "someone") is None

    def test_escaped_colon_and_backslash(self, tmp_path: Path) -> None:
        path = str(self.write(tmp_path, r"db:5432:sales:app:pa\:ss\\word" + "\n"))
        assert read_pgpass(path, "db", 5432, "sales", "app") == "pa:ss\\word"

    def test_socket_directory_matches_localhost(self, tmp_path: Path) -> None:
        path = str(self.write(tmp_path, "localhost:5432:sales:app:secret\n"))
        assert read_pgpass(path, "/tmp", 5432, "sales", "app") == "secret"  # noqa: S108

    def test_missing_file_gives_no_password(self, tmp_path: Path) -> None:
        assert read_pgpass(str(tmp_path / "absent"), "db", 5432, "sales", "app") is None

    @pytest.mark.skipif(sys.platform == "win32", reason="file modes are a Unix rule")
    def test_file_readable_by_others_is_ignored_like_libpq_does(self, tmp_path: Path) -> None:
        path = str(self.write(tmp_path, "*:*:*:*:secret\n", mode=0o644))
        assert read_pgpass(path, "db", 5432, "sales", "app") is None

    def test_password_file_location_follows_libpq(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert db.pgpass_path({"PGPASSFILE": "/etc/custom"}) == "/etc/custom"
        monkeypatch.setattr(sys, "platform", "win32")
        assert db.pgpass_path({"APPDATA": "C:/Users/a/AppData/Roaming"}).endswith("pgpass.conf")
        monkeypatch.setattr(sys, "platform", "linux")
        assert db.pgpass_path({}).endswith(".pgpass")


def failure(message: object, cause: BaseException | None = None) -> pg8000.exceptions.Error:
    error = pg8000.exceptions.InterfaceError(message)
    error.__cause__ = cause
    return error


@pytest.mark.parametrize(
    ("error", "hint"),
    [
        (failure("x", ConnectionRefusedError()), "Nothing accepts connections at localhost:5432"),
        (failure("x", socket.gaierror()), "cannot be resolved"),
        (failure("x", TimeoutError()), "timed out"),
        (failure({"C": "28P01", "M": "password authentication failed"}), "Put the password into"),
        (failure({"C": "3D000", "M": "database does not exist"}), "Database 'app' does not exist"),
        (failure({"C": "28000", "M": "no pg_hba.conf entry"}), "Check the role and pg_hba.conf"),
        (failure("Server refuses SSL"), "Check PGSSLMODE"),
        (failure({"C": "53300", "M": "too many connections"}), "Cannot connect to app@localhost:5432/app"),
    ],
)
def test_connection_failures_become_hints(error: pg8000.exceptions.Error, hint: str) -> None:
    assert hint in db._explain_connect_failure(error, settings(PGPASSWORD="x"))


def test_no_route_to_the_host_points_at_the_vpn() -> None:
    # pg8000 does not wrap the errors of its TLS request, so a bare OSError is explained too.
    error = OSError(errno.EHOSTUNREACH, "No route to host")
    assert "localhost:5432 is unreachable from this machine: No route to host" in db._explain_connect_failure(
        error, settings()
    )


@pytest.mark.parametrize(
    ("error", "down"),
    [
        (failure("x", ConnectionRefusedError()), True),
        (failure("x", socket.gaierror()), True),
        (failure("x", TimeoutError()), True),
        (failure("x", OSError(errno.ENETUNREACH, "Network is unreachable")), True),
        (socket.timeout("timed out"), True),
        (failure("x", ssl.SSLError("wrong version number")), False),
        (failure({"C": "28P01", "M": "password authentication failed"}), False),
        (failure("Server refuses SSL"), False),
    ],
)
def test_only_a_silent_host_counts_as_down(error: Exception, down: bool) -> None:  # noqa: FBT001
    assert db._host_down(error) is down


def test_scram_login_without_a_password_gets_the_password_file_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    def scram_without_password(**kwargs: object) -> None:  # noqa: ARG001
        # pg8000 1.31 fails on None.decode() when a SCRAM server asks for a password that is not set.
        raise AttributeError("'NoneType' object has no attribute 'decode'")

    monkeypatch.setattr("pg8000.native.Connection", scram_without_password)
    with pytest.raises(DatabaseUnavailableError, match="none is set") as caught:
        Database({"PGUSER": "app", "PGPASSFILE": os.devnull}).server_info()
    assert "Put the password into" in str(caught.value)
