"""The password store (ADR 0012): the lines and where a password is taken from, the store behind them,
and what status says about it. No test comes near the system's storage: the store is in memory, and the
keychain of macOS is a script that answers as its tool does. The real one is tried when
PG_EXPLORER_MCP_TEST_KEYCHAIN=1 asks for it, on a record of the test's own that it deletes."""

from __future__ import annotations

import base64
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from pg_explorer_mcp import passwords
from pg_explorer_mcp.db import Database, Settings
from pg_explorer_mcp.passwords import (
    ENVIRONMENT,
    FILE,
    REGISTRY,
    STORE,
    DpapiFile,
    Keychain,
    Passwords,
    PasswordStoreError,
    SecretService,
    line_of,
    login_of,
    lookup,
    matches,
    without,
)
from pg_explorer_mcp.stands import Target
from pg_explorer_mcp.tools import build_tools
from tests.helpers import ROOT, Memory, unreachable_database_env

LOGIN = ("db.example", "5432", "orders", "billing")
KEYCHAIN_TEST = "PG_EXPLORER_MCP_TEST_KEYCHAIN"


class TestLines:
    def test_a_line_escapes_a_colon_and_a_backslash_and_comes_back_as_it_was(self) -> None:
        login = ("db:1", "5432", "or\\ders", "bi:ll\\ing")
        line = line_of(login, "pa:ss\\word")
        assert line == r"db\:1:5432:or\\ders:bi\:ll\\ing:pa\:ss\\word"
        assert login_of(line) == login
        assert matches([line], *login[:1], 5432, "or\\ders", "bi:ll\\ing") == "pa:ss\\word"

    def test_a_comment_or_a_broken_line_is_no_login(self) -> None:
        assert login_of("# a comment") is None
        assert login_of("db:5432:orders") is None

    def test_the_first_line_that_fits_wins_and_a_star_matches_anything(self) -> None:
        lines = ["# comment", "", "db.example:5432:orders:billing:first", "*:5432:*:billing:second"]
        assert matches(lines, "db.example", 5432, "orders", "billing") == "first"
        assert matches(lines, "other", 5432, "hr", "billing") == "second"
        assert matches(lines, "other", 6432, "hr", "billing") is None
        assert matches(lines, "db.example", 5432, "orders", "someone") is None

    def test_a_socket_directory_is_matched_against_localhost_as_libpq_does(self) -> None:
        assert matches(["localhost:5432:orders:billing:secret"], "/tmp", 5432, "orders", "billing") == "secret"  # noqa: S108  (a socket directory, as libpq takes PGHOST)

    def test_without_drops_the_lines_of_exactly_these_logins_and_keeps_the_rest(self) -> None:
        lines = ["# mine", "*:*:*:billing:star", line_of(LOGIN, "exact")]
        assert without(lines, [LOGIN]) == ["# mine", "*:*:*:billing:star"]


class TestLookup:
    @staticmethod
    def env(tmp_path: Path, text: str = "", **more: str) -> dict[str, str]:
        """The shell of a server whose password file is the test's; more variables as named."""
        passfile = tmp_path / "pgpass"
        passfile.write_text(text, encoding="utf-8")
        if sys.platform != "win32":
            passfile.chmod(0o600)
        return {"PGHOST": LOGIN[0], "PGUSER": LOGIN[3], "PGDATABASE": LOGIN[2], "PGPASSFILE": str(passfile), **more}

    def test_nothing_has_the_password(self, tmp_path: Path) -> None:
        assert lookup(self.env(tmp_path), "db.example", 5432, "orders", "billing") == (None, None)

    def test_the_environment_wins_over_the_store_the_file_and_the_registry(
        self, tmp_path: Path, own_password_store: Memory
    ) -> None:
        own_password_store.text = line_of(LOGIN, "kept")
        env = self.env(
            tmp_path, line_of(LOGIN, "written"), PGPASSWORD="given", **{passwords.REGISTRY_PASSWORD: "lined"}
        )
        assert lookup(env, "db.example", 5432, "orders", "billing") == ("given", ENVIRONMENT)

    def test_the_store_wins_over_the_file_and_the_registry(self, tmp_path: Path, own_password_store: Memory) -> None:
        own_password_store.text = line_of(LOGIN, "kept")
        env = self.env(tmp_path, line_of(LOGIN, "written"), **{passwords.REGISTRY_PASSWORD: "lined"})
        assert lookup(env, "db.example", 5432, "orders", "billing") == ("kept", STORE)

    def test_the_file_feeds_those_who_had_one_before_the_store(self, tmp_path: Path) -> None:
        env = self.env(tmp_path, line_of(LOGIN, "written"), **{passwords.REGISTRY_PASSWORD: "lined"})
        assert lookup(env, "db.example", 5432, "orders", "billing") == ("written", FILE)

    def test_a_password_written_in_the_registry_is_the_last_resort(self, tmp_path: Path) -> None:
        env = self.env(tmp_path, **{passwords.REGISTRY_PASSWORD: "lined"})
        assert lookup(env, "db.example", 5432, "orders", "billing") == ("lined", REGISTRY)

    def test_a_file_that_others_can_read_is_ignored_and_leaves_no_password(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        env = self.env(tmp_path, line_of(LOGIN, "written"))
        (tmp_path / "pgpass").chmod(0o644)
        assert lookup(env, "db.example", 5432, "orders", "billing") == (None, None)

    def test_the_settings_say_where_the_password_of_each_host_comes_from(
        self, tmp_path: Path, own_password_store: Memory
    ) -> None:
        own_password_store.text = line_of(("h1", "5432", "orders", "billing"), "kept")
        env = self.env(
            tmp_path, line_of(("h2", "6544", "orders", "billing"), "written"), PGHOST="h1,h2", PGPORT="5432,6544"
        )
        settings = Settings.from_env(env)
        assert [(endpoint.password, endpoint.source) for endpoint in settings.endpoints] == [
            ("kept", STORE),
            ("written", FILE),
        ]
        assert (settings.password, settings.source) == ("kept", STORE)

    def test_switched_off_the_store_is_left_alone_however_full_it_is(
        self, tmp_path: Path, own_password_store: Memory
    ) -> None:
        own_password_store.text = line_of(LOGIN, "kept")
        env = self.env(tmp_path, line_of(LOGIN, "written"), **{passwords.STORE_VARIABLE: "off"})
        assert passwords.system(env) is None
        passwords.use(None)
        assert lookup(env, "db.example", 5432, "orders", "billing") == ("written", FILE)
        assert own_password_store.readings == 0

    @pytest.mark.parametrize("value", ["off", "OFF", " no ", "0", "false"])
    def test_off_in_any_spelling(self, value: str) -> None:
        assert passwords.system({passwords.STORE_VARIABLE: value}) is None


class Locked:
    """A store that can neither be read nor written, as a keychain that nobody unlocked."""

    label = "locked"

    def __init__(self) -> None:
        self.tried = 0

    def read(self) -> str:
        self.tried += 1
        raise PasswordStoreError("locked")

    def write(self, _text: str) -> None:
        raise PasswordStoreError("locked")


class TestTheStore:
    @staticmethod
    def store() -> Passwords:
        return passwords.current({}) or pytest.fail("the test's store is not in use")

    def test_the_store_is_read_once_however_many_logins_ask(self, own_password_store: Memory) -> None:
        store = self.store()
        for _ in range(3):
            assert store.lookup("db.example", 5432, "orders", "billing") is None
        assert own_password_store.readings == 1

    def test_the_store_is_read_again_once_the_reading_has_grown_old(
        self, own_password_store: Memory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = self.store()
        assert store.lookup("db.example", 5432, "orders", "billing") is None
        own_password_store.text = line_of(LOGIN, "changed")
        assert store.lookup("db.example", 5432, "orders", "billing") is None  # read a moment ago
        monkeypatch.setattr(passwords, "REFRESH_S", 0)
        assert store.lookup("db.example", 5432, "orders", "billing") == "changed"
        assert own_password_store.readings == 2

    def test_put_replaces_the_line_of_the_same_login_and_keeps_every_other_line(
        self, own_password_store: Memory
    ) -> None:
        own_password_store.text = "# mine\n*:*:*:billing:star\n" + line_of(LOGIN, "old") + "\n"
        self.store().put({LOGIN: "new", ("h", "5432", "d", "u"): "x"})
        assert own_password_store.text.splitlines() == [
            "# mine",
            "*:*:*:billing:star",
            line_of(LOGIN, "new"),
            "h:5432:d:u:x",
        ]
        assert own_password_store.text.endswith("\n")

    def test_a_line_whose_fields_are_escaped_is_replaced_too(self, own_password_store: Memory) -> None:
        login = ("db:1", "5432", "or\\ders", "billing")
        own_password_store.text = line_of(login, "old") + "\n"
        self.store().put({login: "new"})
        assert own_password_store.text == line_of(login, "new") + "\n"

    def test_a_store_that_cannot_be_read_gives_no_password_and_says_why(self) -> None:
        locked = Locked()
        store = Passwords(locked)
        assert store.lookup("db.example", 5432, "orders", "billing") is None
        assert store.lookup("h", 5432, "d", "u") is None
        assert (store.problem(), locked.tried) == ("locked", 1)  # asked once, not on every login

    def test_nothing_to_put_writes_nothing_and_creates_no_record(self) -> None:
        memory = Memory()
        Passwords(memory).put({})
        assert (memory.text, memory.readings) == ("", 0)

    def test_a_store_that_cannot_be_written_keeps_what_was_read(self) -> None:
        class Sealed(Memory):
            def write(self, _text: str) -> None:
                raise PasswordStoreError("sealed")

        sealed = Sealed()
        sealed.text = line_of(LOGIN, "old") + "\n"
        store = Passwords(sealed)
        with pytest.raises(PasswordStoreError, match="sealed"):
            store.put({LOGIN: "new"})
        assert store.lookup("db.example", 5432, "orders", "billing") == "old"
        assert sealed.text == line_of(LOGIN, "old") + "\n"


class TestWhichStore:
    def test_the_choice_is_made_once_from_the_first_environment_that_asks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(passwords._process, "decided", False)
        monkeypatch.setattr(passwords._process, "passwords", None)
        assert passwords.current({passwords.STORE_VARIABLE: "off"}) is None
        assert passwords.current({}) is None  # the same process, the same answer

    def test_use_replaces_the_store_of_the_process(self, own_password_store: Memory) -> None:
        other = Memory()
        other.text = line_of(LOGIN, "other") + "\n"
        passwords.use(other)
        assert lookup({}, "db.example", 5432, "orders", "billing") == ("other", STORE)
        assert own_password_store.readings == 0

    def test_macos_has_the_keychain_when_its_tool_is_there(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/security")
        assert isinstance(passwords._of_this_system({}), Keychain)
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        assert passwords._of_this_system({}) is None

    def test_linux_has_the_keyring_of_the_desktop_when_its_tool_is_there(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/secret-tool" if name == "secret-tool" else None)
        store = passwords._of_this_system({})
        assert isinstance(store, SecretService)
        assert store._tool == "/usr/bin/secret-tool"

    def test_linux_without_a_desktop_keyring(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        # The keychain tool of macOS found under a Linux name: no keyring tool, no store.
        monkeypatch.setattr(shutil, "which", lambda name: None if name == "secret-tool" else "/usr/bin/security")
        assert passwords._of_this_system({}) is None

    def test_windows_has_a_file_under_appdata_sealed_by_dpapi(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        store = passwords._of_this_system({"APPDATA": str(tmp_path)})
        assert isinstance(store, DpapiFile)
        assert store._path == tmp_path / "pg-explorer-mcp" / "passwords"


# The keychain tool as the tests need it: the same arguments and exit codes, the records in a file next to it.
FAKE_TOOL = """
import json
import sys
import time
from pathlib import Path

here = Path(__file__).parent
state, calls = here / "state.json", here / "calls.log"
args = sys.argv[1:]
with calls.open("a") as noted:
    noted.write(json.dumps(args) + "\\n")
data = json.loads(state.read_text()) if state.exists() else {}
if "sleep" in data:
    time.sleep(data["sleep"])
if args[0] == "find-generic-password":
    if data.get("locked"):
        sys.stderr.write("security: SecKeychainSearchCopyNext: User interaction is not allowed.")
        sys.exit(36)
    record = data.get(args[args.index("-s") + 1] + "/" + args[args.index("-a") + 1])
    if record is None:
        sys.stderr.write("security: SecKeychainSearchCopyNext: The specified item could not be found.")
        sys.exit(44)
    print(record)
elif args[0] == "add-generic-password":
    if data.get("refuse"):
        sys.stderr.write("security: SecKeychainAddGenericPassword: The user name or passphrase is not correct.")
        sys.exit(36)
    data[args[args.index("-s") + 1] + "/" + args[args.index("-a") + 1]] = args[args.index("-w") + 1]
    state.write_text(json.dumps(data))
elif args[0] == "delete-generic-password":
    data.pop(args[args.index("-s") + 1] + "/" + args[args.index("-a") + 1], None)
    state.write_text(json.dumps(data))
else:
    sys.exit(1)
"""


class TestKeychain:
    @pytest.fixture
    def folder(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """A keychain whose tool is the script: it answers as the security tool does, from files in the folder."""
        script = tmp_path / "security.py"
        script.write_text(FAKE_TOOL, encoding="utf-8")
        monkeypatch.setattr(passwords, "_command", lambda *args: [sys.executable, str(script), *args])
        return tmp_path

    @staticmethod
    def records(folder: Path) -> dict[str, str]:
        loaded = json.loads((folder / "state.json").read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        return loaded

    @pytest.mark.usefixtures("folder")
    def test_nothing_is_stored_yet(self) -> None:
        assert Keychain().read() == ""

    @pytest.mark.usefixtures("folder")
    def test_what_is_written_is_read_back_with_every_line_and_letter(self) -> None:
        keychain = Keychain()
        keychain.write("db.example:5432:orders:billing:секрет\n*:*:*:billing:st\\:ar\n")
        assert keychain.read() == "db.example:5432:orders:billing:секрет\n*:*:*:billing:st\\:ar\n"

    def test_the_record_reaches_the_tool_on_one_base64_line_never_in_the_clear(self, folder: Path) -> None:
        Keychain().write("db.example:5432:orders:billing:s3cret\n")
        [record] = self.records(folder).values()
        assert "\n" not in record
        assert "s3cret" not in record
        assert base64.b64decode(record) == b"db.example:5432:orders:billing:s3cret\n"

    def test_the_tool_is_asked_by_our_service_and_account_without_a_terminal(self, folder: Path) -> None:
        Keychain().read()
        Keychain().write("x")
        asked = [json.loads(line) for line in (folder / "calls.log").read_text(encoding="utf-8").splitlines()]
        assert asked[0] == ["find-generic-password", "-s", "pg-explorer-mcp", "-a", "passwords", "-w"]
        assert asked[1][:5] == ["add-generic-password", "-U", "-s", "pg-explorer-mcp", "-a"]

    def test_a_locked_keychain_refuses_and_the_refusal_names_no_password(self, folder: Path) -> None:
        (folder / "state.json").write_text(json.dumps({"refuse": True}), encoding="utf-8")
        with pytest.raises(PasswordStoreError) as caught:
            Keychain().write("db.example:5432:orders:billing:s3cret\n")
        assert "did not answer" in str(caught.value)
        assert "s3cret" not in str(caught.value)

    def test_a_locked_keychain_cannot_be_read_and_the_error_says_why(self, folder: Path) -> None:
        (folder / "state.json").write_text(json.dumps({"locked": True}), encoding="utf-8")
        with pytest.raises(PasswordStoreError, match="did not answer: security: SecKeychainSearchCopyNext"):
            Keychain().read()

    def test_another_record_under_our_name_is_refused(self, folder: Path) -> None:
        (folder / "state.json").write_text(
            json.dumps({"pg-explorer-mcp/passwords": "not the server's"}), encoding="utf-8"
        )
        with pytest.raises(PasswordStoreError, match="another record under pg-explorer-mcp/passwords"):
            Keychain().read()

    def test_a_record_that_is_not_our_text_is_refused(self, folder: Path) -> None:
        not_text = base64.b64encode(b"\xff\xfe").decode("ascii")
        (folder / "state.json").write_text(json.dumps({"pg-explorer-mcp/passwords": not_text}), encoding="utf-8")
        with pytest.raises(PasswordStoreError, match="another record under pg-explorer-mcp/passwords"):
            Keychain().read()

    def test_a_tool_that_does_not_answer_in_time_is_refused(
        self, folder: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (folder / "state.json").write_text(json.dumps({"sleep": 5}), encoding="utf-8")
        monkeypatch.setattr(passwords, "TOOL_TIMEOUT_S", 0.5)
        with pytest.raises(PasswordStoreError, match="did not answer in time"):
            Keychain().read()

    def test_a_tool_that_cannot_be_started_is_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(passwords, "_command", lambda *_args: [str(tmp_path / "no-such-tool")])
        with pytest.raises(PasswordStoreError, match="could not be started"):
            Keychain().read()

    def test_forget_removes_the_record(self, folder: Path) -> None:
        keychain = Keychain()
        keychain.write("x")
        keychain.forget()
        assert keychain.read() == ""
        assert self.records(folder) == {}

    def test_the_record_of_the_tests_is_found_under_its_own_name(self, folder: Path) -> None:
        Keychain("pg-explorer-mcp-test", "test").write("x")
        assert self.records(folder) == {"pg-explorer-mcp-test/test": "eA=="}


@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get(KEYCHAIN_TEST) != "1",
    reason=f"the login keychain of a Mac, when {KEYCHAIN_TEST}=1 asks for it",
)
def test_the_login_keychain_of_this_mac_keeps_a_record_without_a_dialog() -> None:
    """On a record of the test's own, deleted afterwards: the user's own record is never touched."""
    keychain = Keychain(f"pg-explorer-mcp-test-{os.getpid()}", "passwords")
    try:
        keychain.write("db.example:5432:orders:billing:секрет\n")
        assert keychain.read() == "db.example:5432:orders:billing:секрет\n"
    finally:
        keychain.forget()
    assert keychain.read() == ""


def status_lines(env: dict[str, str]) -> list[str]:
    return {tool.name: tool for tool in build_tools(Database(env), str(ROOT))}["status"].call({}).splitlines()


UNSET = "password: not set. Put it into the password store: python3 install.py password"


class TestStatus:
    def test_with_no_password_anywhere(self) -> None:
        lines = status_lines(unreachable_database_env())
        assert "password store: the memory of the tests" in lines
        assert UNSET in lines

    def test_with_the_password_in_the_store(self, own_password_store: Memory) -> None:
        env = unreachable_database_env()
        own_password_store.text = line_of(("127.0.0.1", env["PGPORT"], "pg_mcp_test", "pg_mcp_test"), "s3cret")
        lines = status_lines(env)
        assert "password: from the password store" in lines
        assert not any(line.startswith("warning: the password") for line in lines)
        assert "s3cret" not in "\n".join(lines)

    def test_with_the_password_in_the_environment(self) -> None:
        assert "password: from PGPASSWORD in the environment" in status_lines(
            dict(unreachable_database_env(), PGPASSWORD="x")
        )

    def test_with_the_password_in_the_file_of_those_who_had_one(self, tmp_path: Path) -> None:
        passfile = tmp_path / "pgpass"
        passfile.write_text("*:*:*:pg_mcp_test:written\n", encoding="utf-8")
        if sys.platform != "win32":
            passfile.chmod(0o600)
        lines = status_lines(dict(unreachable_database_env(), PGPASSFILE=str(passfile)))
        assert lines.count(UNSET) == 0
        assert (
            f"warning: the password comes from {passfile} in clear text: move it into the password store "
            "with python3 install.py password --move"
        ) in lines

    def test_switched_off_by_the_variable(self) -> None:
        passwords.use(None)
        lines = status_lines(dict(unreachable_database_env(), **{passwords.STORE_VARIABLE: "off"}))
        assert "password store: off (PG_EXPLORER_MCP_PASSWORD_STORE)" in lines
        assert UNSET in lines

    def test_on_a_system_without_a_store(self) -> None:
        passwords.use(None)
        lines = status_lines(unreachable_database_env())
        assert (
            "password store: this system has none, so the password comes from PGPASSWORD or the password file" in lines
        )

    def test_a_store_that_cannot_be_read_is_a_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(passwords._process, "passwords", Passwords(Locked()))
        lines = status_lines(unreachable_database_env())
        assert "password store: locked" in lines
        assert "warning: the password store cannot be read: locked" in lines
        assert UNSET in lines

    def test_the_settings_are_read_again_so_a_new_password_needs_no_restart(
        self, own_password_store: Memory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = unreachable_database_env()
        database = Database(env)
        assert database.settings is not None
        assert database.settings.password is None
        login = ("127.0.0.1", env["PGPORT"], "pg_mcp_test", "pg_mcp_test")
        monkeypatch.setattr(passwords, "REFRESH_S", 0)  # another process wrote the record a while ago
        own_password_store.text = line_of(login, "typed later")
        assert database.settings.password is None  # read when the server started
        database.reread()
        reread = database.settings
        assert reread is not None
        assert (reread.password, reread.source) == ("typed later", STORE)

    @pytest.mark.usefixtures("russian")
    def test_in_russian(self, own_password_store: Memory) -> None:
        env = unreachable_database_env()
        own_password_store.text = line_of(("127.0.0.1", env["PGPORT"], "pg_mcp_test", "pg_mcp_test"), "s3cret")
        lines = status_lines(env)
        assert "пароль: из хранилища паролей" in lines
        assert "пароль: не задан. Положите его в хранилище паролей: python3 install.py password" in status_lines(
            unreachable_database_env()
        )


def test_a_password_of_a_registry_section_is_not_the_entry_s_and_loses_to_the_store() -> None:
    target = Target("dev1", "orders", "billing", {"host": "db", "password": "written"}, "stands.conf")
    env = target.env({"PGPASSWORD": "entry-secret"})
    assert "PGPASSWORD" not in env  # the entry's password never reaches a stand
    assert env[passwords.REGISTRY_PASSWORD] == "written"
    assert lookup(env, "db", 5432, "orders", "billing") == ("written", REGISTRY)
    store = passwords.current(env)
    assert store is not None
    store.put({("db", "5432", "orders", "billing"): "kept"})
    assert lookup(env, "db", 5432, "orders", "billing") == ("kept", STORE)


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows'")
class TestDpapiFile:
    def test_nothing_is_stored_yet(self, tmp_path: Path) -> None:
        assert DpapiFile(tmp_path / "passwords").read() == ""

    def test_what_is_written_is_read_back_by_another_store_over_the_same_file(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "passwords"
        lines = "db.example:5432:orders:billing:секрет\n*:*:*:billing:st\\:ar\n"
        DpapiFile(path).write(lines)
        assert DpapiFile(path).read() == lines

    def test_the_file_holds_nothing_in_the_clear(self, tmp_path: Path) -> None:
        path = tmp_path / "passwords"
        DpapiFile(path).write(line_of(LOGIN, "s3cret") + "\n")
        raw = path.read_bytes()
        assert not any(word in raw for word in (b"s3cret", b"db.example", b"orders", b"billing"))
        assert DpapiFile(path).label == f"the file {path}, sealed by DPAPI for this Windows account"

    def test_a_file_that_is_not_ours_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "passwords"
        path.write_bytes(b"written by someone else")
        with pytest.raises(PasswordStoreError, match="cannot be read or written"):
            DpapiFile(path).read()

    def test_the_old_file_stays_as_a_dated_copy_that_opens_the_same_way(self, tmp_path: Path) -> None:
        path = tmp_path / "passwords"
        DpapiFile(path).write("one\n")
        DpapiFile(path).write("two\n")
        [backup] = tmp_path.glob("passwords.bak-*")
        assert DpapiFile(backup).read() == "one\n"

    def test_the_store_of_the_system_is_found_again_by_the_next_process(self, tmp_path: Path) -> None:
        env = {"APPDATA": str(tmp_path)}
        first = passwords.system(env)
        assert isinstance(first, DpapiFile)
        passwords.use(first)
        passwords.current(env).put({LOGIN: "s3cret"})  # type: ignore[union-attr]
        passwords.use(passwords.system(env))  # what a server started later decides
        assert lookup({}, "db.example", 5432, "orders", "billing") == ("s3cret", STORE)


# The keyring tool as the tests need it: the same arguments and exit codes, the items in a file next to it,
# and the same limit of 8 KiB on the secret it reads.
FAKE_KEYRING = """
import json
import sys
import time
from pathlib import Path

here = Path(__file__).parent
state, calls, flags = here / "items.json", here / "calls.log", here / "flags.json"
args = sys.argv[1:]
secret = sys.stdin.read()
items = json.loads(state.read_text()) if state.exists() else {}
extra = json.loads(flags.read_text()) if flags.exists() else {}
with calls.open("a") as noted:
    noted.write(json.dumps([args, secret]) + "\\n")
if "sleep" in extra:
    time.sleep(extra["sleep"])
if extra.get("refuse"):
    sys.stderr.write("secret-tool: The name org.freedesktop.secrets was not provided by any .service files")
    sys.exit(1)
action, rest = args[0], args[1:]
if action == "store":
    rest = rest[1:]  # --label=...
attributes = dict(zip(rest[::2], rest[1::2]))
key = json.dumps(attributes, sort_keys=True)
if action == "lookup":
    if key not in items:
        sys.exit(1)
    sys.stdout.write(items[key] + ("\\n" if extra.get("newline") else ""))
elif action == "store":
    items[key] = secret.encode("utf-8")[:8192].decode("utf-8", "ignore")  # as the real one: 8 KiB at most
    state.write_text(json.dumps(items))
elif action == "clear":
    def matches(kept):
        have = json.loads(kept)
        return all(have.get(name) == value for name, value in attributes.items())

    matching = [kept for kept in items if matches(kept)]
    for kept in matching:
        del items[kept]
    state.write_text(json.dumps(items))
    sys.exit(0 if matching else 1)
else:
    sys.exit(1)
"""


def items_of(folder: Path) -> dict[str, str]:
    """The items of the fake keyring: their attributes as a sorted JSON key, and the secret."""
    items = json.loads((folder / "items.json").read_text(encoding="utf-8"))
    assert isinstance(items, dict)
    return items


def calls_of(folder: Path) -> list[tuple[list[str], str]]:
    """What the fake keyring was asked, in order: the arguments, and the secret on its input."""
    asked = []
    for line in (folder / "calls.log").read_text(encoding="utf-8").splitlines():
        arguments, secret = json.loads(line)
        asked.append((arguments, secret))
    return asked


class TestSecretService:
    """secret-tool reads 8 KiB at most: the record lies in the keyring as parts, under a generation (ADR 0012)."""

    BIG = "".join(line_of((f"host{n}", "5432", "orders", "billing"), f"пароль-{n}") + "\n" for n in range(700))

    @pytest.fixture
    def folder(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """A keyring whose tool is the script: it answers as secret-tool does, from files in the folder."""
        script = tmp_path / "secret-tool.py"
        script.write_text(FAKE_KEYRING, encoding="utf-8")
        monkeypatch.setattr(passwords, "_keyring_command", lambda _tool, *args: [sys.executable, str(script), *args])
        return tmp_path

    @pytest.mark.usefixtures("folder")
    def test_nothing_is_stored_yet(self) -> None:
        assert SecretService().read() == ""

    @pytest.mark.usefixtures("folder")
    def test_what_is_written_is_read_back_by_another_store(self) -> None:
        record = line_of(LOGIN, "секрет") + "\n"
        SecretService().write(record)
        assert SecretService().read() == record

    def test_a_long_record_lies_in_parts_the_tool_takes_whole(self, folder: Path) -> None:
        assert len(self.BIG.encode("utf-8")) > 3 * passwords.PART_BYTES
        SecretService().write(self.BIG)
        assert SecretService().read() == self.BIG  # a part too long would come back cut
        stored = [secret for args, secret in calls_of(folder) if args[0] == "store"]
        assert max(len(secret) for secret in stored) <= 8192
        assert sum("part" in args for args, _ in calls_of(folder) if args[0] == "store") >= 3

    def test_the_head_goes_in_last_and_names_the_generation(self, folder: Path) -> None:
        SecretService().write("one\n")
        stores = [args for args, _ in calls_of(folder) if args[0] == "store"]
        assert stores[-1][-2:] == ["kind", "head"]
        [head] = [secret for args, secret in calls_of(folder) if args[0] == "store" and "head" in args]
        assert base64.b64decode(head) == b"1"

    def test_a_second_writing_makes_a_new_generation_and_clears_the_old_one(self, folder: Path) -> None:
        SecretService().write("one\n" * 5000)
        SecretService().write("two\n")
        assert SecretService().read() == "two\n"
        generations = {json.loads(key)["generation"] for key in items_of(folder) if "generation" in json.loads(key)}
        assert generations == {"2"}
        heads = [key for key in items_of(folder) if json.loads(key).get("kind") == "head"]
        assert len(heads) == 1  # the tool updates the item of the same attributes and makes no duplicate

    def test_the_secret_goes_in_on_the_input_and_never_as_an_argument(self, folder: Path) -> None:
        SecretService().write(line_of(LOGIN, "s3cret") + "\n")
        for args, _secret in calls_of(folder):
            assert "s3cret" not in " ".join(args)
        given = [secret for args, secret in calls_of(folder) if args[0] == "store"]
        assert any("s3cret" in base64.b64decode(secret).decode("utf-8") for secret in given)

    def test_a_tool_that_ends_the_secret_with_a_line_end_is_understood(self, folder: Path) -> None:
        (folder / "flags.json").write_text(json.dumps({"newline": True}), encoding="utf-8")
        SecretService().write("one\ntwo\n")
        assert SecretService().read() == "one\ntwo\n"

    def test_a_keyring_that_refuses_is_an_error_that_names_no_password(self, folder: Path) -> None:
        (folder / "flags.json").write_text(json.dumps({"refuse": True}), encoding="utf-8")
        with pytest.raises(PasswordStoreError) as caught:
            SecretService().write(line_of(LOGIN, "s3cret") + "\n")
        assert "did not answer" in str(caught.value)
        assert "s3cret" not in str(caught.value)

    def test_a_tool_that_does_not_answer_in_time_is_refused(
        self, folder: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (folder / "flags.json").write_text(json.dumps({"sleep": 5}), encoding="utf-8")
        monkeypatch.setattr(passwords, "TOOL_TIMEOUT_S", 0.5)
        with pytest.raises(PasswordStoreError, match="did not answer in time"):
            SecretService().read()

    def test_a_head_that_is_not_a_number_is_another_record(self, folder: Path) -> None:
        head = json.dumps({"account": "passwords", "kind": "head", "service": "pg-explorer-mcp"}, sort_keys=True)
        (folder / "items.json").write_text(json.dumps({head: "not the server's"}), encoding="utf-8")
        with pytest.raises(PasswordStoreError, match="another record under pg-explorer-mcp/passwords"):
            SecretService().read()

    def test_a_head_without_its_parts_is_an_error_not_an_empty_record(self, folder: Path) -> None:
        SecretService().write("one\n")
        items = {key: value for key, value in items_of(folder).items() if "part" not in json.loads(key)}
        (folder / "items.json").write_text(json.dumps(items), encoding="utf-8")
        with pytest.raises(PasswordStoreError, match="names generation 1 and has no parts"):
            SecretService().read()

    def test_a_record_rewritten_between_its_head_and_its_parts_is_read_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: list[tuple[tuple[str, str], ...]] = []

        def item(_self: SecretService, *attributes: tuple[str, str]) -> str | None:
            asked.append(attributes)
            if attributes == (("kind", "head"),):
                return "1" if asked.count(attributes) == 1 else "2"
            return "kept" if attributes[:2] == (("generation", "2"), ("part", "0")) else None

        monkeypatch.setattr(SecretService, "_item", item)
        assert SecretService().read() == "kept"

    def test_forget_removes_every_item(self, folder: Path) -> None:
        SecretService().write(self.BIG)
        SecretService().forget()
        assert (items_of(folder), SecretService().read()) == ({}, "")

    @pytest.mark.usefixtures("folder")
    def test_a_collection_named_is_an_option_of_the_store(self, folder: Path) -> None:
        SecretService(collection=SESSION_COLLECTION).write("one\n")
        store = next(args for args, _ in calls_of(folder) if args[0] == "store")
        assert store[store.index("--label=pg-explorer-mcp passwords") + 1] == f"--collection={SESSION_COLLECTION}"

    def test_the_label_names_the_record(self) -> None:
        label = "the keyring of the desktop, record pg-explorer-mcp/passwords (Secret Service)"
        assert SecretService().label == label


KEYRING_TEST = "PG_EXPLORER_MCP_TEST_KEYRING"
SESSION_COLLECTION = "/org/freedesktop/secrets/collection/session"


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("secret-tool") is None or os.environ.get(KEYRING_TEST) != "1",
    reason=f"the keyring of a Linux desktop, when {KEYRING_TEST}=1 asks for it and secret-tool is there",
)
def test_the_keyring_of_this_linux_keeps_the_record() -> None:
    """On a record of the test's own, deleted afterwards: the user's own record is never touched."""
    tool = shutil.which("secret-tool")
    assert tool is not None
    account = f"passwords-test-{os.getpid()}"
    record = "".join(line_of((f"host{n}", "5432", "orders", "billing"), f"пароль-{n}") + "\n" for n in range(700))

    def keyring() -> SecretService:
        # The session collection is always there. The login one is made by the login to the desktop,
        # which a machine without a desktop never went through.
        return SecretService(tool, passwords.SERVICE, account, SESSION_COLLECTION)

    try:
        keyring().write(record)
        assert keyring().read() == record
        keyring().write("one\n")
        assert keyring().read() == "one\n"  # the head updated in place, not duplicated
    finally:
        keyring().forget()
    assert keyring().read() == ""
