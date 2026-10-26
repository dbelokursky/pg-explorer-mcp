"""AES-128 in CBC mode with PKCS#7 padding, in plain Python.

The standard library has no AES, and the server must not depend on packages. This is
enough to read the credentials DBeaver keeps next to its connections (imports.py): a
few hundred bytes at most, so speed does not matter. The cipher follows FIPS 197; the
tables are computed rather than typed, and tests/test_aes.py checks the published
vectors and a round trip through openssl.
"""

from __future__ import annotations

BLOCK = 16
ROUNDS = 10  # AES-128


def _xtime(value: int) -> int:
    """Multiply by x in GF(2^8)."""
    return ((value << 1) ^ 0x1B) & 0xFF if value & 0x80 else value << 1


def _multiply(a: int, b: int) -> int:
    result = 0
    while b:
        if b & 1:
            result ^= a
        a = _xtime(a)
        b >>= 1
    return result


def _boxes() -> tuple[list[int], list[int]]:
    """The S-box and its inverse: the multiplicative inverse in GF(2^8) under the affine transform."""
    sbox = [0] * 256
    p = q = 1
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)  # p *= 3
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09  # q /= 3
        affine = q ^ (q << 1) ^ (q << 2) ^ (q << 3) ^ (q << 4)
        sbox[p] = (affine ^ (affine >> 8) ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    inverse = [0] * 256
    for index, value in enumerate(sbox):
        inverse[value] = index
    return sbox, inverse


SBOX, INVERSE_SBOX = _boxes()


def _expand(key: bytes) -> list[list[int]]:
    """The eleven round keys of a 16-byte key."""
    if len(key) != BLOCK:
        raise ValueError(f"AES-128 needs a 16-byte key, not {len(key)} bytes")
    words = [list(key[i : i + 4]) for i in range(0, BLOCK, 4)]
    rcon = 1
    for i in range(4, 4 * (ROUNDS + 1)):
        temp = list(words[i - 1])
        if i % 4 == 0:
            temp = [SBOX[b] for b in temp[1:] + temp[:1]]
            temp[0] ^= rcon
            rcon = _xtime(rcon)
        words.append([words[i - 4][j] ^ temp[j] for j in range(4)])
    return [[b for word in words[4 * r : 4 * r + 4] for b in word] for r in range(ROUNDS + 1)]


def _add_round_key(state: list[int], round_key: list[int]) -> list[int]:
    return [s ^ k for s, k in zip(state, round_key)]


def _shift_rows(state: list[int], *, inverse: bool) -> list[int]:
    sign = -1 if inverse else 1
    return [state[r + 4 * ((c + sign * r) % 4)] for c in range(4) for r in range(4)]


def _mix_columns(state: list[int], *, inverse: bool) -> list[int]:
    factors = (14, 11, 13, 9) if inverse else (2, 3, 1, 1)
    mixed: list[int] = []
    for c in range(4):
        column = state[4 * c : 4 * c + 4]
        for r in range(4):
            rotated = factors[-r:] + factors[:-r] if r else factors
            mixed.append(
                _multiply(rotated[0], column[0])
                ^ _multiply(rotated[1], column[1])
                ^ _multiply(rotated[2], column[2])
                ^ _multiply(rotated[3], column[3])
            )
    return mixed


def encrypt_block(key: bytes, block: bytes) -> bytes:
    round_keys = _expand(key)
    state = _add_round_key(list(block), round_keys[0])
    for number in range(1, ROUNDS + 1):
        state = [SBOX[b] for b in state]
        state = _shift_rows(state, inverse=False)
        if number < ROUNDS:
            state = _mix_columns(state, inverse=False)
        state = _add_round_key(state, round_keys[number])
    return bytes(state)


def decrypt_block(key: bytes, block: bytes) -> bytes:
    round_keys = _expand(key)
    state = _add_round_key(list(block), round_keys[ROUNDS])
    for number in range(ROUNDS - 1, -1, -1):
        state = _shift_rows(state, inverse=True)
        state = [INVERSE_SBOX[b] for b in state]
        state = _add_round_key(state, round_keys[number])
        if number:
            state = _mix_columns(state, inverse=True)
    return bytes(state)


def encrypt_cbc(key: bytes, iv: bytes, data: bytes) -> bytes:
    """CBC with PKCS#7 padding, as Java's AES/CBC/PKCS5Padding produces."""
    _check_iv(iv)
    padding = BLOCK - len(data) % BLOCK
    padded = data + bytes([padding]) * padding
    previous, out = iv, bytearray()
    for start in range(0, len(padded), BLOCK):
        block = bytes(a ^ b for a, b in zip(padded[start : start + BLOCK], previous))
        previous = encrypt_block(key, block)
        out += previous
    return bytes(out)


def decrypt_cbc(key: bytes, iv: bytes, data: bytes) -> bytes:
    """The plaintext, or ValueError when the data is not whole blocks or the padding is wrong."""
    _check_iv(iv)
    if not data or len(data) % BLOCK:
        raise ValueError(f"the ciphertext must be whole blocks of {BLOCK} bytes, not {len(data)} bytes")
    previous, out = iv, bytearray()
    for start in range(0, len(data), BLOCK):
        block = data[start : start + BLOCK]
        out += bytes(a ^ b for a, b in zip(decrypt_block(key, block), previous))
        previous = block
    padding = out[-1]
    if not 1 <= padding <= BLOCK or out[-padding:] != bytes([padding]) * padding:
        raise ValueError("the padding is wrong: not this key, or not AES-CBC with PKCS#7")
    return bytes(out[:-padding])


def _check_iv(iv: bytes) -> None:
    if len(iv) != BLOCK:
        raise ValueError(f"the IV must be {BLOCK} bytes, not {len(iv)}")
