"""TLS between the server and PostgreSQL, against the real test database behind a TLS front.

The PostgreSQL of CI has no certificates, so these tests put a small TLS front in between:
it answers the client's SSLRequest, completes the handshake with a certificate that openssl
makes here, and forwards the rest to the real server in plain text. Under test is the
client side: which sslmode connects, which refuses, and what the refusal says.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import ssl
import struct
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from pg_explorer_mcp.db import Database, Settings
from pg_explorer_mcp.tools import build_tools
from tests.helpers import ROOT

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl to make the certificates"),
]

SSL_REQUEST = struct.pack("!ii", 8, 80877103)  # what a libpq client sends first when it wants TLS
HOSTNAME = "db.example.test"  # the certificate's name; the tests connect to 127.0.0.1, which is not it
TLS, PLAIN, FAILED = "tls", "plain", "failed"


@pytest.fixture(scope="module")
def certificates(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """A throwaway CA and a server certificate it signed, valid for HOSTNAME only."""
    folder = tmp_path_factory.mktemp("tls")
    ca_key, ca, key, request, cert = (
        folder / name for name in ("ca.key", "ca.crt", "server.key", "server.csr", "server.crt")
    )
    extensions = folder / "san.cnf"
    extensions.write_text(f"subjectAltName=DNS:{HOSTNAME}\n", encoding="ascii")
    new_key = ("-newkey", "rsa:2048", "-nodes")
    san = ["-extfile", extensions]
    commands = [
        ["req", "-x509", *new_key, "-keyout", ca_key, "-out", ca, "-subj", "/CN=pg-explorer-mcp test CA"],
        ["req", *new_key, "-keyout", key, "-out", request, "-subj", f"/CN={HOSTNAME}"],
        ["x509", "-req", "-in", request, "-CA", ca, "-CAkey", ca_key, "-CAcreateserial", "-out", cert, *san],
    ]  # fmt: skip
    for command in commands:
        subprocess.run(["openssl", *map(str, command), "-days", "3650"], check=True, capture_output=True)
    return {"ca": ca, "cert": cert, "key": key}


class TlsFront:
    """Accepts connections for the real PostgreSQL and does the TLS part of the protocol itself.

    Every session is recorded as TLS, PLAIN or FAILED (the client refused the certificate).
    """

    def __init__(self, backend: tuple[str, int], cert: Path, key: Path) -> None:
        self.backend = backend
        self.sessions: list[str] = []
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(str(cert), str(key))
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(5)
        self.port: int = self.server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self) -> None:
        self.server.close()

    def sessions_after(self, count: int) -> list[str]:
        """The sessions recorded since there were count of them; the front's thread may lag a little."""
        deadline = time.monotonic() + 5
        while len(self.sessions) <= count and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.sessions[count:]

    def _serve(self) -> None:
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return  # closed
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        client.settimeout(5)
        head = _exactly(client, len(SSL_REQUEST))
        if head == SSL_REQUEST:
            client.sendall(b"S")
            try:
                client = self.context.wrap_socket(client, server_side=True)
            except (ssl.SSLError, OSError):
                self.sessions.append(FAILED)
                client.close()
                return
            self.sessions.append(TLS)
            head = b""
        else:
            self.sessions.append(PLAIN)
        backend = socket.create_connection(self.backend, timeout=5)
        backend.sendall(head)  # without an SSLRequest, these are the startup message's first bytes
        threading.Thread(target=_pump, args=(client, backend), daemon=True).start()
        _pump(backend, client)


def _exactly(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            break
        data += chunk
    return data


def _pump(source: socket.socket, sink: socket.socket) -> None:
    with contextlib.suppress(OSError):
        while True:
            data = source.recv(65536)
            if not data:
                break
            sink.sendall(data)
    for sock in (source, sink):
        with contextlib.suppress(OSError):
            sock.close()


@pytest.fixture(scope="module")
def front(certificates: dict[str, Path]) -> Iterator[TlsFront]:
    settings = Settings.from_env(os.environ)
    host = "localhost" if settings.host.startswith("/") else settings.host
    front = TlsFront((host, settings.port), certificates["cert"], certificates["key"])
    yield front
    front.close()


def status_through(front: TlsFront, **overrides: str) -> str:
    env = {key: value for key, value in os.environ.items() if key not in ("PGSSLMODE", "PGSSLROOTCERT")}
    env.update(PGHOST="127.0.0.1", PGPORT=str(front.port), **overrides)
    database = Database(env)
    try:
        return build_tools(database, str(ROOT))[0].call({})
    finally:
        database.close()


def test_require_takes_any_certificate(front: TlsFront) -> None:
    seen = len(front.sessions)
    assert "database: connected to" in status_through(front, PGSSLMODE="require")
    assert front.sessions_after(seen) == [TLS]


def test_verify_ca_trusts_the_authority_it_is_given(front: TlsFront, certificates: dict[str, Path]) -> None:
    seen = len(front.sessions)
    text = status_through(front, PGSSLMODE="verify-ca", PGSSLROOTCERT=str(certificates["ca"]))
    assert "database: connected to" in text
    assert front.sessions_after(seen) == [TLS]


def test_verify_full_also_wants_the_host_name_to_match(front: TlsFront, certificates: dict[str, Path]) -> None:
    seen = len(front.sessions)
    text = status_through(front, PGSSLMODE="verify-full", PGSSLROOTCERT=str(certificates["ca"]))
    assert f"database: unavailable. TLS with 127.0.0.1:{front.port} failed" in text
    assert "Check PGSSLMODE and PGSSLROOTCERT" in text
    assert front.sessions_after(seen) == [FAILED]


def test_an_authority_the_machine_does_not_know_is_refused(front: TlsFront) -> None:
    seen = len(front.sessions)
    text = status_through(front, PGSSLMODE="verify-ca")  # the system's authorities, not the test CA
    assert f"database: unavailable. TLS with 127.0.0.1:{front.port} failed" in text
    assert front.sessions_after(seen) == [FAILED]


def test_prefer_takes_tls_when_the_server_offers_it(front: TlsFront) -> None:
    seen = len(front.sessions)
    assert "database: connected to" in status_through(front, PGSSLMODE="prefer")
    assert front.sessions_after(seen) == [TLS]


@pytest.mark.parametrize("mode", ["disable", "allow"])
def test_disable_and_allow_start_in_plain_text(front: TlsFront, mode: str) -> None:
    seen = len(front.sessions)
    assert "database: connected to" in status_through(front, PGSSLMODE=mode)
    assert front.sessions_after(seen) == [PLAIN]
