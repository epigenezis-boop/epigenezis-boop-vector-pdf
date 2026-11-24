"""Sign or verify a canonical VECTOR PDF update manifest with Ed25519.

The private key is accepted from ``--key`` or the ``UPDATE_SIGNING_KEY``
environment variable and is never written into the repository.  The signature
is a detached 64-byte RFC 8032 signature over the exact canonical manifest
bytes (sorted keys, ``(',', ':')`` separators, UTF-8).

Examples::

    python tools/sign_manifest.py release/manifest.json --key %USERPROFILE%\\vector-update.key
    python tools/sign_manifest.py release/manifest.json --verify
"""
from __future__ import annotations

import argparse
import base64
import binascii
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vector_pdf import update_key  # noqa: E402
from vector_pdf import updater  # noqa: E402


def read_private_seed(path):
    """Read a 32-byte Ed25519 seed from hex or base64 text."""
    try:
        text = Path(path).read_text(encoding='ascii')
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(f'Cannot read the private key: {error}') from error
    compact = ''.join(text.split())
    if not compact:
        raise ValueError('The private key file is empty.')
    raw = None
    if len(compact) in (64, 128):
        try:
            raw = bytes.fromhex(compact)
        except ValueError:
            raw = None
    if raw is None:
        try:
            raw = base64.b64decode(compact, validate=True)
        except (binascii.Error, ValueError):
            raw = None
    if raw is None or len(raw) not in (32, 64):
        raise ValueError('The private key must be a 32-byte Ed25519 seed.')
    return raw[:32]


def key_from_arguments(path):
    if path:
        return read_private_seed(path)
    environment = os.environ.get('UPDATE_SIGNING_KEY')
    if environment:
        candidate = Path(environment).expanduser()
        if candidate.is_file():
            return read_private_seed(candidate)
        compact = ''.join(environment.split())
        try:
            raw = bytes.fromhex(compact)
        except ValueError:
            try:
                raw = base64.b64decode(compact, validate=True)
            except (binascii.Error, ValueError) as error:
                raise ValueError(
                    'UPDATE_SIGNING_KEY is not a valid private key.') from error
        if len(raw) not in (32, 64):
            raise ValueError('UPDATE_SIGNING_KEY must hold 32 or 64 bytes.')
        return raw[:32]
    raise ValueError('No signing key supplied (--key or UPDATE_SIGNING_KEY).')


def load_canonical(path):
    raw = Path(path).read_bytes()
    manifest, canonical = updater.load_manifest(raw)
    if raw != canonical:
        raise ValueError('The manifest is not canonical.')
    updater.validate_manifest(manifest)
    return manifest, raw


def sign_file(manifest_path, key_path=None, output=None):
    manifest, raw = load_canonical(manifest_path)
    seed = key_from_arguments(key_path)
    public = update_key.public_key(seed)
    signature = update_key.sign(seed, raw)
    if not update_key.verify(public, raw, signature):
        raise ValueError('Generated signature failed self-verification.')
    destination = Path(output) if output else Path(str(manifest_path) + '.sig')
    destination.write_bytes(signature)
    print(f'Signed {manifest_path} version {manifest["version"]} '
          f'({len(raw)} bytes) -> {destination}')
    print(f'Public key: {public.hex()}')
    return destination


def verify_file(manifest_path, signature_path=None, allow_placeholder=False):
    manifest, raw = load_canonical(manifest_path)
    signature = Path(signature_path or (str(manifest_path) + '.sig')).read_bytes()
    if len(signature) != 64:
        raise ValueError('The signature must be exactly 64 bytes.')
    public = update_key.PUBLIC_KEY
    if public == update_key.PLACEHOLDER_PUBLIC_KEY and not allow_placeholder:
        raise ValueError(
            'The repository still uses the placeholder public key; '
            'install the production key before releasing updates.')
    if not update_key.verify(public, raw, signature):
        raise ValueError('The manifest signature is invalid.')
    print(f'Verified {manifest_path} version {manifest["version"]} '
          f'with key {update_key.key_id(public)}')
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--key', type=Path,
                        help='private key file (or UPDATE_SIGNING_KEY)')
    parser.add_argument('--output', type=Path,
                        help='signature path (default: <manifest>.sig)')
    parser.add_argument('--verify', action='store_true',
                        help='verify against vector_pdf/update_key.py')
    parser.add_argument('--signature', type=Path,
                        help='signature path for --verify')
    parser.add_argument('--allow-placeholder', action='store_true',
                        help='test only: allow the known placeholder key')
    args = parser.parse_args(argv)
    try:
        if args.verify:
            verify_file(args.manifest, args.signature,
                        args.allow_placeholder)
        else:
            sign_file(args.manifest, args.key, args.output)
    except (OSError, ValueError) as error:
        raise SystemExit(f'error: {error}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
