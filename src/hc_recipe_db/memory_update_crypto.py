from __future__ import annotations

import hashlib


# Small RFC 8032 Ed25519 implementation used only for signing/verifying tiny
# Field Crafter memory-update manifests. It has no network or file access.
_Q = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _Q - 2, _Q)) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, _Q - 2, _Q) % _Q
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q != 0:
        x = (x * _I) % _Q
    if x & 1:
        x = _Q - x
    return x


_BY = (4 * pow(5, _Q - 2, _Q)) % _Q
_BX = _xrecover(_BY)
_B = (_BX, _BY)


def _is_on_curve(point: tuple[int, int]) -> bool:
    x, y = point
    return (
        (-x * x + y * y - 1 - _D * x * x * y * y) % _Q
    ) == 0


def _add(p: tuple[int, int], q: tuple[int, int]) -> tuple[int, int]:
    x1, y1 = p
    x2, y2 = q
    common = (_D * x1 * x2 * y1 * y2) % _Q
    x3 = ((x1 * y2 + x2 * y1) * pow(1 + common, _Q - 2, _Q)) % _Q
    y3 = ((y1 * y2 + x1 * x2) * pow(1 - common, _Q - 2, _Q)) % _Q
    return x3, y3


def _scalarmult(point: tuple[int, int], scalar: int) -> tuple[int, int]:
    result = (0, 1)
    addend = point
    value = int(scalar)
    while value:
        if value & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        value >>= 1
    return result


def _encode_point(point: tuple[int, int]) -> bytes:
    x, y = point
    encoded = y | ((x & 1) << 255)
    return int(encoded).to_bytes(32, "little")


def _decode_point(data: bytes) -> tuple[int, int]:
    if len(data) != 32:
        raise ValueError("Ed25519 point must be 32 bytes")
    raw = int.from_bytes(data, "little")
    sign = (raw >> 255) & 1
    y = raw & ((1 << 255) - 1)
    if y >= _Q:
        raise ValueError("Invalid Ed25519 point encoding")
    x = _xrecover(y)
    if (x & 1) != sign:
        x = _Q - x
    point = (x, y)
    if not _is_on_curve(point):
        raise ValueError("Point is not on Ed25519 curve")
    return point


def _hint(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little")


def public_key_from_seed(seed: bytes) -> bytes:
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be exactly 32 bytes")
    digest = hashlib.sha512(seed).digest()
    scalar_bytes = bytearray(digest[:32])
    scalar_bytes[0] &= 248
    scalar_bytes[31] &= 63
    scalar_bytes[31] |= 64
    scalar = int.from_bytes(scalar_bytes, "little")
    return _encode_point(_scalarmult(_B, scalar))


def sign(seed: bytes, message: bytes) -> bytes:
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be exactly 32 bytes")
    digest = hashlib.sha512(seed).digest()
    scalar_bytes = bytearray(digest[:32])
    scalar_bytes[0] &= 248
    scalar_bytes[31] &= 63
    scalar_bytes[31] |= 64
    scalar = int.from_bytes(scalar_bytes, "little")
    prefix = digest[32:]
    public_key = _encode_point(_scalarmult(_B, scalar))
    r = _hint(prefix + message) % _L
    encoded_r = _encode_point(_scalarmult(_B, r))
    challenge = _hint(encoded_r + public_key + message) % _L
    s = (r + challenge * scalar) % _L
    return encoded_r + int(s).to_bytes(32, "little")


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    try:
        if len(public_key) != 32 or len(signature) != 64:
            return False
        encoded_r = signature[:32]
        s = int.from_bytes(signature[32:], "little")
        if s >= _L:
            return False
        a = _decode_point(public_key)
        r = _decode_point(encoded_r)
        challenge = _hint(encoded_r + public_key + message) % _L
        left = _scalarmult(_B, s)
        right = _add(r, _scalarmult(a, challenge))
        return _encode_point(left) == _encode_point(right)
    except Exception:
        return False
