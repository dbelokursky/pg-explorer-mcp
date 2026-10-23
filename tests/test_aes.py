"""AES-128-CBC in plain Python: the published vectors, openssl's answer and the error cases."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from pg_mcp import aes

KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")


def test_the_fips_197_vector() -> None:
    plain = bytes.fromhex("00112233445566778899aabbccddeeff")
    cipher = aes.encrypt_block(KEY, plain)
    assert cipher.hex() == "69c4e0d86a7b0430d8cdb78070b4c55a"
    assert aes.decrypt_block(KEY, cipher) == plain


def test_the_sbox_is_the_published_one() -> None:
    assert (aes.SBOX[0x00], aes.SBOX[0x53], aes.SBOX[0xFF]) == (0x63, 0xED, 0x16)
    assert all(aes.INVERSE_SBOX[aes.SBOX[value]] == value for value in range(256))


@pytest.mark.parametrize("size", [0, 1, 15, 16, 17, 100])
def test_cbc_round_trip_with_padding(size: int) -> None:
    key, iv, data = os.urandom(16), os.urandom(16), os.urandom(size)
    cipher = aes.encrypt_cbc(key, iv, data)
    assert len(cipher) % 16 == 0
    assert len(cipher) > size
    assert aes.decrypt_cbc(key, iv, cipher) == data


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
def test_openssl_agrees(tmp_path: Path) -> None:
    key, iv, data = os.urandom(16), os.urandom(16), b'{"postgres-jdbc-1": {"#connection": {"user": "u"}}}'
    source = tmp_path / "plain"
    source.write_bytes(data)
    command = ["openssl", "enc", "-aes-128-cbc", "-K", key.hex(), "-iv", iv.hex(), "-in", str(source)]
    cipher = subprocess.run(command, capture_output=True, check=True).stdout
    assert aes.encrypt_cbc(key, iv, data) == cipher
    assert aes.decrypt_cbc(key, iv, cipher) == data


@pytest.mark.parametrize("last_byte", [0, 17, 2])
def test_broken_padding_is_an_error(last_byte: int) -> None:
    """A wrong key leaves random bytes in the last block; these are the ways they fail the check."""
    key, iv = os.urandom(16), os.urandom(16)
    # 0 and 17 are no padding lengths; 2 would need the byte before it to be 2 as well.
    block = bytes(14) + bytes([1, last_byte])
    cipher = aes.encrypt_block(key, bytes(a ^ b for a, b in zip(block, iv)))
    with pytest.raises(ValueError, match="padding is wrong"):
        aes.decrypt_cbc(key, iv, cipher)


def test_broken_input_is_an_error() -> None:
    key, iv = os.urandom(16), os.urandom(16)
    cipher = aes.encrypt_cbc(key, iv, b"hello")
    with pytest.raises(ValueError, match="whole blocks"):
        aes.decrypt_cbc(key, iv, cipher[:-1])
    with pytest.raises(ValueError, match="whole blocks"):
        aes.decrypt_cbc(key, iv, b"")
    with pytest.raises(ValueError, match="16-byte key"):
        aes.encrypt_block(b"short", b"\0" * 16)
    with pytest.raises(ValueError, match="IV must be"):
        aes.encrypt_cbc(key, b"short", b"x")
