"""Putting a new text in place of a user's file (user_files.py): each waiting text a file of its own, the file
never missing while it is replaced, and one command at a time over the files it reads and writes. The commands
are other processes of the test's own: the lock is the lock of the system, or it is nothing."""

from __future__ import annotations

import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pg_explorer_mcp import user_files
from pg_explorer_mcp.user_files import StillLockedError, discard, exclusive, place, stage, unplace
from tests.helpers import ROOT

SRC = str(ROOT / "src")

WRITER = """
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from pg_explorer_mcp.user_files import exclusive, place, stage

folder, target, mine, times = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], int(sys.argv[5])
for _ in range(times):  # what a command does: read, merge, write, under the lock
    with exclusive(folder):
        lines = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
        place(stage(target, "\\n".join([*lines, mine]) + "\\n"))
"""

HOLDER = """
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from pg_explorer_mcp.user_files import exclusive

with exclusive(Path(sys.argv[2])):
    print("held", flush=True)
    sys.stdin.readline()  # until the test says so
"""


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class TestStaging:
    def test_two_texts_for_one_file_wait_each_in_a_file_of_its_own(self, tmp_path: Path) -> None:
        """They used to wait in one file: the first command published the text of the second, and the second failed."""
        target = tmp_path / "stands.conf"
        first, second = stage(target, "first\n"), stage(target, "second\n")
        assert first[2] != second[2]
        assert (first[2].read_text(encoding="utf-8"), second[2].read_text(encoding="utf-8")) == ("first\n", "second\n")
        assert place(first) is None
        assert target.read_text(encoding="utf-8") == "first\n"
        assert place(second) is not None
        assert target.read_text(encoding="utf-8") == "second\n"
        assert [child.name for child in tmp_path.iterdir() if user_files.NEW in child.name] == []

    def test_a_waiting_text_is_readable_by_its_owner_alone_whatever_the_umask(self, tmp_path: Path) -> None:
        staged = stage(tmp_path / "pgpass", "db.example:5432:orders:billing:s3cret\n")
        if sys.platform != "win32":
            assert mode_of(staged[2]) == 0o600
        discard([staged])
        assert not list(tmp_path.iterdir())

    def test_bytes_go_as_they_are_and_the_path_named_is_the_one_taken_back(self, tmp_path: Path) -> None:
        sealed = bytes(range(256))
        named, real, waiting = stage(tmp_path / "data" / "passwords", sealed)
        assert waiting.read_bytes() == sealed
        assert (named, real.parent) == (tmp_path / "data" / "passwords", tmp_path / "data")
        discard([(named, real, waiting)])
        assert not waiting.exists()


class TestReplacing:
    def test_the_file_is_there_at_every_moment_of_its_replacement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It used to be moved to its dated copy first and replaced a moment after: a reader could find no file."""
        target = tmp_path / "stands.conf"
        target.write_text("old\n", encoding="utf-8")
        replace = Path.replace
        found: list[bool] = []

        def watching(self: Path, to: Path) -> Path:
            moved = replace(self, to)
            if self == target.resolve() or Path(to) == target.resolve():  # the file itself moved, or was replaced
                found.append(target.exists())
            return moved

        monkeypatch.setattr(Path, "replace", watching)
        assert place(stage(target, "new\n")) is not None
        assert found == [True]
        assert target.read_text(encoding="utf-8") == "new\n"

    def test_a_text_that_cannot_be_put_in_place_leaves_the_file_and_no_dated_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "stands.conf"
        target.write_text("old\n", encoding="utf-8")
        replace = Path.replace

        def busy(self: Path, to: Path) -> Path:
            if Path(to) == target.resolve() and user_files.NEW in self.name:
                raise PermissionError(13, "The file is in use")
            return replace(self, to)

        monkeypatch.setattr(Path, "replace", busy)
        staged = stage(target, "new\n")
        with pytest.raises(PermissionError, match="in use"):
            place(staged)
        assert target.read_text(encoding="utf-8") == "old\n"
        discard([staged])
        assert [child.name for child in tmp_path.iterdir()] == ["stands.conf"]  # no dated copy of a failure

    def test_the_new_text_takes_the_permissions_of_the_old_file_and_its_dated_copy_keeps_them(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "settings.json"
        target.write_text("old\n", encoding="utf-8")
        if sys.platform != "win32":
            target.chmod(0o640)
        backup = place(stage(target, "new\n"))
        assert backup is not None
        assert (target.read_text(encoding="utf-8"), backup.read_text(encoding="utf-8")) == ("new\n", "old\n")
        if sys.platform != "win32":
            assert (mode_of(target), mode_of(backup)) == (0o640, 0o640)

    def test_a_file_that_may_hold_passwords_is_left_to_its_owner_alone_with_its_dated_copy(
        self, tmp_path: Path
    ) -> None:
        if sys.platform == "win32":
            pytest.skip("no mode bits to speak of")
        target = tmp_path / "pgpass"
        target.write_text("old-secret\n", encoding="utf-8")
        target.chmod(0o644)
        backup = place(stage(target, "new-secret\n"), owner_only=True)
        assert backup is not None
        assert (mode_of(target), mode_of(backup)) == (0o600, 0o600)

    def test_taken_back_the_file_is_what_it_was_and_a_first_file_goes(self, tmp_path: Path) -> None:
        target = tmp_path / "stands.conf"
        target.write_text("old\n", encoding="utf-8")
        staged = stage(target, "new\n")
        unplace(staged, place(staged))
        assert [child.name for child in tmp_path.iterdir()] == ["stands.conf"]
        assert target.read_text(encoding="utf-8") == "old\n"
        first = stage(tmp_path / "first.conf", "new\n")
        unplace(first, place(first))
        assert [child.name for child in tmp_path.iterdir()] == ["stands.conf"]


class TestTheLock:
    def test_one_command_at_a_time_reads_merges_and_writes_so_that_no_change_is_lost(self, tmp_path: Path) -> None:
        """Four commands at once, twenty lines each.

        Without the lock each wrote from what it read before the others wrote, and a line was lost.
        """
        target = tmp_path / "stands.conf"
        writers = [
            subprocess.Popen(
                [sys.executable, "-I", "-c", WRITER, SRC, str(tmp_path), str(target), f"line{number}", "20"]
            )
            for number in range(4)
        ]
        assert [writer.wait(timeout=120) for writer in writers] == [0, 0, 0, 0]
        lines = target.read_text(encoding="utf-8").splitlines()
        assert sorted(lines) == sorted(f"line{number}" for number in range(4) for _ in range(20))

    def test_the_next_command_waits_for_the_one_before_it_no_longer_than_it_may(self, tmp_path: Path) -> None:
        before = subprocess.Popen(
            [sys.executable, "-I", "-c", HOLDER, SRC, str(tmp_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert before.stdout is not None
            assert before.stdin is not None
            assert before.stdout.readline().strip() == "held"
            got_in = False
            with pytest.raises(StillLockedError), exclusive(tmp_path, timeout=0.2):
                got_in = True
            assert not got_in  # nothing of the block ran
            if sys.platform != "win32":
                assert mode_of(tmp_path / "lock") == 0o600
            before.stdin.write("over\n")  # the command before is done
            before.stdin.flush()
            with exclusive(tmp_path):
                waited = True
            assert waited
        finally:
            before.kill()
            before.wait(timeout=10)

    def test_a_command_that_dies_lets_its_lock_go(self, tmp_path: Path) -> None:
        before = subprocess.Popen(
            [sys.executable, "-I", "-c", HOLDER, SRC, str(tmp_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert before.stdout is not None
            assert before.stdout.readline().strip() == "held"
            before.kill()  # it never got to the end of its block
            before.wait(timeout=10)
            started = time.monotonic()
            with exclusive(tmp_path, timeout=5):
                took = time.monotonic() - started
            assert took < 5
        finally:
            before.kill()
            before.wait(timeout=10)

    def test_where_no_lock_can_be_made_the_work_goes_on_without_one(self, tmp_path: Path) -> None:
        (tmp_path / "not-a-folder").write_text("", encoding="utf-8")
        ran = False
        with exclusive(tmp_path / "not-a-folder"):  # its lock file cannot even be made
            ran = True
        assert ran
