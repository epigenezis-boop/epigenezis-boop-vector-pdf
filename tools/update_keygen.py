"""Generate an Ed25519 update-signing keypair and install its public half.

The private key is written once, outside the repository, and is never printed or
copied into the source tree.  Only the 32-byte public key is written into
``vector_pdf/update_key.py`` (between the generated-key markers).

Example::

    python tools/update_keygen.py --private-key %USERPROFILE%\\vector-update.key

Then sign a release manifest with ``tools/sign_manifest.py``.  The placeholder
key in the repository is intentionally rejected by the updater at runtime.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vector_pdf import update_key  # noqa: E402

BEGIN = '# BEGIN GENERATED KEY'
END = '# END GENERATED KEY'
_MARKERS = re.compile(
    r'# BEGIN GENERATED KEY[^\n]*\n.*?# END GENERATED KEY', re.DOTALL)


def generated_block(public_hex, key_id, placeholder=False):
    return (f'{BEGIN} - rewritten by tools/update_keygen.py\n'
            f"KEY_ID = '{key_id}'\n"
            f"PUBLIC_KEY_HEX = '{public_hex}'\n"
            f'IS_PLACEHOLDER = {placeholder}\n'
            f'{END}')


def replace_public_key(source, public_hex, key_id, placeholder=False):
    block = generated_block(public_hex, key_id, placeholder)
    if not _MARKERS.search(source):
        raise ValueError('The module does not contain generated-key markers.')
    return _MARKERS.sub(block, source, count=1)


def write_private_key(path, seed, force=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT
    flags |= os.O_TRUNC if force else os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='ascii', newline='') as stream:
            stream.write(seed.hex() + '\n')
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-key', required=True, type=Path,
                        help='destination for the 32-byte seed (hex text)')
    parser.add_argument('--module', type=Path,
                        default=ROOT / 'vector_pdf' / 'update_key.py',
                        help='trusted public-key module to update')
    parser.add_argument('--force', action='store_true',
                        help='overwrite an existing private key/module block')
    parser.add_argument('--print-public', action='store_true',
                        help='only print the public key; write nothing')
    args = parser.parse_args(argv)

    private = args.private_key.expanduser().resolve()
    try:
        private.relative_to(ROOT)
    except ValueError:
        pass
    else:
        raise SystemExit(
            'Refusing to write the private key inside the repository.')
    if private.exists() and not args.force and not args.print_public:
        raise SystemExit(f'Private key already exists: {private}')

    seed = os.urandom(32)
    public = update_key.public_key(seed)
    public_hex = public.hex()
    identifier = update_key.key_id(public)
    if args.print_public:
        print(public_hex)
        return 0

    write_private_key(private, seed, args.force)
    module = args.module.expanduser().resolve()
    try:
        source = module.read_text(encoding='utf-8')
        updated = replace_public_key(source, public_hex, identifier)
    except (OSError, ValueError) as error:
        try:
            private.unlink(missing_ok=True)
        except OSError:
            pass
        raise SystemExit(f'Could not update the public-key module: {error}')
    module.write_text(updated, encoding='utf-8', newline='\n')
    print(f'Private key: {private}')
    print(f'Public key:  {public_hex}')
    print(f'Key id:      {identifier}')
    print(f'Module:      {module}')
    print('Keep the private key offline and outside the repository.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
