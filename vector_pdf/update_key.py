"""Trusted update-signing key and dependency-free Ed25519 (RFC 8032).

The release archive is authenticated with a detached Ed25519 signature over the
canonical bytes of ``manifest.json``.  Verification lives here so the portable
build does not need a new runtime dependency.

The RFC 8032 placeholder key is reserved for tests and is never trusted by
the updater. The generated block holds the public release key. Keep the
private key outside the repository and out of release packages.

Sign manifests with:

    python tools/sign_manifest.py release/manifest.json --key <private-key>
"""
from __future__ import annotations

import hashlib

# BEGIN GENERATED KEY - rewritten by tools/update_keygen.py
KEY_ID = 'placeholder-rfc8032-test-vector-1'
PUBLIC_KEY_HEX = 'd75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a'
IS_PLACEHOLDER = True
# END GENERATED KEY

# RFC 8032 test vector 1 public key.  Kept as a constant so the updater can
# fail closed even after a real key is generated: a key equal to this value is
# never trusted.
PLACEHOLDER_PUBLIC_KEY = bytes.fromhex(
    'd75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a')

PUBLIC_KEY = bytes.fromhex(PUBLIC_KEY_HEX)

# ---------------------------------------------------------------------------
# Ed25519, RFC 8032 section 5.1.  This is verification-first but the signing
# primitives are shared with tools/sign_manifest.py and update_keygen.py so the
# same code is exercised by the RFC test vectors.
# ---------------------------------------------------------------------------

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_D2 = (2 * _D) % _P
_I = pow(2, (_P - 1) // 4, _P)
_BY = (4 * pow(5, _P - 2, _P)) % _P
_BX = pow((_BY * _BY - 1) * pow(_D * _BY * _BY + 1, _P - 2, _P) % _P,
          (_P + 3) // 8, _P)
if (_BX * _BX - (_BY * _BY - 1) * pow(_D * _BY * _BY + 1, _P - 2, _P)) % _P:
    _BX = (_BX * _I) % _P
if _BX & 1:
    _BX = _P - _BX
_BASE = (_BX, _BY, 1, (_BX * _BY) % _P)


def _inv(value):
    return pow(value, _P - 2, _P)


def _recover_x(y, sign):
    if not 0 <= y < _P:
        return None
    x2 = (y * y - 1) * _inv(_D * y * y + 1) % _P
    if x2 == 0:
        return 0 if sign == 0 else None
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _I % _P
    if (x * x - x2) % _P:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


def _decode_point(data):
    if len(data) != 32:
        return None
    value = int.from_bytes(data, 'little')
    sign = value >> 255
    y = value & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _encode_point(point):
    x, y, z, _ = point
    zinv = _inv(z)
    x = x * zinv % _P
    y = y * zinv % _P
    return (y | ((x & 1) << 255)).to_bytes(32, 'little')


def _add(left, right):
    x1, y1, z1, t1 = left
    x2, y2, z2, t2 = right
    a = (y1 - x1) * (y2 - x2) % _P
    b = (y1 + x1) * (y2 + x2) % _P
    c = t1 * _D2 * t2 % _P
    d = z1 * 2 * z2 % _P
    e = b - a
    f = d - c
    g = d + c
    h = b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _double(point):
    x1, y1, z1, _ = point
    a = x1 * x1 % _P
    b = y1 * y1 % _P
    c = 2 * z1 * z1 % _P
    h = a + b
    e = h - (x1 + y1) * (x1 + y1) % _P
    g = a - b
    f = c + g
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _multiply(scalar, point):
    result = (0, 1, 1, 0)
    while scalar:
        if scalar & 1:
            result = _add(result, point)
        point = _double(point)
        scalar >>= 1
    return result


def _secret_scalar(seed):
    if len(seed) != 32:
        raise ValueError('An Ed25519 seed must be exactly 32 bytes.')
    digest = hashlib.sha512(seed).digest()
    scalar = bytearray(digest[:32])
    scalar[0] &= 248
    scalar[31] &= 127
    scalar[31] |= 64
    return int.from_bytes(scalar, 'little'), digest[32:]


def public_key(seed):
    """Return the 32-byte Ed25519 public key for a 32-byte seed."""
    scalar, _ = _secret_scalar(seed)
    return _encode_point(_multiply(scalar, _BASE))


def sign(seed, message):
    """Return the 64-byte detached Ed25519 signature of *message*."""
    if not isinstance(message, (bytes, bytearray, memoryview)):
        raise TypeError('The signed message must be bytes.')
    scalar, prefix = _secret_scalar(seed)
    public = _encode_point(_multiply(scalar, _BASE))
    message = bytes(message)
    nonce = int.from_bytes(hashlib.sha512(prefix + message).digest(), 'little') % _L
    encoded_r = _encode_point(_multiply(nonce, _BASE))
    challenge = int.from_bytes(
        hashlib.sha512(encoded_r + public + message).digest(), 'little') % _L
    s = (nonce + challenge * scalar) % _L
    return encoded_r + s.to_bytes(32, 'little')


def verify(public, message, signature):
    """Verify a detached Ed25519 signature.  Never raises for bad input."""
    try:
        if len(public) != 32 or len(signature) != 64:
            return False
        if not isinstance(message, (bytes, bytearray, memoryview)):
            return False
        a = _decode_point(public)
        r = _decode_point(signature[:32])
        if a is None or r is None:
            return False
        s = int.from_bytes(signature[32:], 'little')
        if s >= _L:
            return False
        message = bytes(message)
        challenge = int.from_bytes(
            hashlib.sha512(signature[:32] + public + message).digest(),
            'little') % _L
        return _encode_point(_multiply(s, _BASE)) == _encode_point(
            _add(r, _multiply(challenge, a)))
    except (ValueError, TypeError):
        return False


def key_id(public=None):
    """Short stable identifier for a public key (not a secret)."""
    material = PUBLIC_KEY if public is None else public
    return hashlib.sha256(material).hexdigest()[:16]


# Readable aliases used by the signing tools and external test harnesses.
ed25519_verify = verify
ed25519_sign = sign
public_key_from_seed = public_key
