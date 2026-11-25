"""Signed, side-by-side Windows updates with health checks and rollback.

A release is accepted only when all four assets are present at the exact
``github.com/<repo>/releases/download/v<version>/`` URL:

* ``VECTOR-PDF-<version>-win-x64.zip``
* ``VECTOR-PDF-<version>-win-x64.zip.sha256``
* ``manifest.json``
* ``manifest.json.sig`` (detached raw Ed25519 signature)

``manifest.json`` is canonical UTF-8 JSON (sorted keys, compact separators) and
lists every file in the archive with its size and SHA-256.  The signature is
verified before any manifest field is trusted.  Installation downloads into an
``incoming-*`` directory, validates the complete file set, runs the staged
runtime health check, and only then flips the ``active``/``previous`` pointers.

Automatic updates are intentionally controlled by the UI and disabled unless the
user opts in.  ``dispatch`` runs before Qt starts, hands the new process a
one-time readiness token, and rolls back if the new process does not confirm
startup within the timeout or if the installed hashes no longer match.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import stat
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory, mkstemp
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

import truststore

from . import update_key
from .version import REPOSITORY, VERSION

MAX_DOWNLOAD = 200 * 1024**2
MAX_EXPANDED = 800 * 1024**2
MAX_MANIFEST = 8 * 1024**2
MAX_ENTRIES = 20_000
MAX_CENTRAL_DIRECTORY = 64 * 1024**2
MAX_PATH_LENGTH = 1024
MAX_POINTER = 256
LOCK_STALE = 30 * 60
BROKEN_RETENTION = 7 * 24 * 60 * 60
INCOMING_RETENTION = 60 * 60
READY_TIMEOUT = 30.0

# Exact HTTPS origins.  Port 443 (explicit or implied) only; the host is
# compared after urllib's case folding, and userinfo/fragments are rejected.
HOSTS = {
    'github.com',
    'api.github.com',
    'release-assets.githubusercontent.com',
    'objects.githubusercontent.com',
    'github-releases.githubusercontent.com',
}

# Full component set that must be present in every installed version.  These
# files are also part of the signed manifest; the hard-coded list protects
# against a manifest that was legitimately signed but accidentally incomplete.
REQUIRED_FILES = (
    'VectorPDF.exe',
    'runtime/pythonw.exe',
    'runtime/python.exe',
    'runtime/python312.dll',
    'runtime/python312.zip',
    'runtime/python312._pth',
    'runtime/qt.conf',
    'runtime/Lib/site-packages/PySide6/Qt6Core.dll',
    'runtime/Lib/site-packages/PySide6/Qt6Gui.dll',
    'runtime/Lib/site-packages/PySide6/Qt6Widgets.dll',
    'runtime/Lib/site-packages/PySide6/plugins/platforms/qwindows.dll',
    'vector_pdf/__main__.py',
    'vector_pdf/ui.py',
    'vector_pdf/writer.py',
    'vector_pdf/fonts.py',
    'vector_pdf/version.py',
)
REQUIRED_PREFIXES = ('vector_pdf/assets/', 'licenses/')
REQUIRED = REQUIRED_FILES  # backwards-compatible alias

_VERSION_RE = re.compile(
    r'(?:0|[1-9][0-9]{0,5})(?:\.(?:0|[1-9][0-9]{0,5})){2}')
_SHA256_RE = re.compile('[0-9a-f]{64}')
_RESERVED_DOS = {
    'CON', 'PRN', 'AUX', 'NUL', 'CLOCK$', 'CONIN$', 'CONOUT$',
    *(f'COM{i}' for i in range(0, 10)),
    *(f'LPT{i}' for i in range(0, 10)),
    *(f'COM{s}' for s in '¹²³'),
    *(f'LPT{s}' for s in '¹²³'),
}
_INVALID_WINDOWS_CHARS = set('<>:"|?*')

# Trusted key imported from update_key.  Tests (and a future key-rotation
# mechanism) may replace this value; the placeholder is rejected below.
PUBLIC_KEY = update_key.PUBLIC_KEY
LOG_PATH = None


def version_tuple(value):
    if not isinstance(value, str) or not _VERSION_RE.fullmatch(value):
        raise ValueError('Invalid release version.')
    return tuple(map(int, value.split('.')))


def root():
    return Path(os.environ['LOCALAPPDATA']) / 'VectorPDF' / 'updates'


def log_path():
    if LOG_PATH is not None:
        return Path(LOG_PATH)
    local = os.environ.get('LOCALAPPDATA')
    base = Path(local) if local else Path.home() / 'AppData' / 'Local'
    return base / 'VectorPDF' / 'logs' / 'update.log'


def log(message):
    """Append a diagnostic line; logging must never break an update path."""
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        text = str(message).replace('\r', ' ').replace('\n', ' | ')[:4000]
        with path.open('a', encoding='utf-8') as stream:
            stream.write(f'{stamp} {text}\n')
    except OSError:
        pass


def trusted(url):
    """Accept only exact HTTPS hosts on the default port."""
    if not isinstance(url, str) or not url:
        raise ValueError('Unexpected download origin.')
    parsed = urlsplit(url)
    if parsed.scheme.lower() != 'https' or parsed.username is not None or \
            parsed.password is not None or parsed.fragment:
        raise ValueError('Unexpected download origin.')
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError('Unexpected download origin.') from error
    if port not in (None, 443) or (parsed.hostname or '') not in HOSTS:
        raise ValueError('Unexpected download origin.')
    return url


class GitHubRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers,
                                        trusted(newurl))


def fetch(url):
    request = Request(trusted(url), headers={'User-Agent': f'VECTOR-PDF/{VERSION}'})
    handlers = [GitHubRedirects()]
    if sys.platform == 'win32':
        # Windows can complete certificate chains with missing intermediates.
        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        handlers.append(HTTPSHandler(context=context))
    return build_opener(*handlers).open(request, timeout=25)


@dataclass(frozen=True)
class Release:
    version: str
    archive: str
    checksum: str
    manifest: str = ''
    signature: str = ''


def release_assets(data):
    """Return a Release only for a complete, exact, stable release."""
    if not isinstance(data, dict):
        raise ValueError('Invalid GitHub release data.')
    if data.get('draft') or data.get('prerelease'):
        return None
    tag = data.get('tag_name')
    if not isinstance(tag, str) or not tag.startswith('v'):
        raise ValueError('Invalid GitHub release tag.')
    version = tag[1:]
    version_tuple(version)
    if version_tuple(version) <= version_tuple(VERSION):
        return None
    name = f'VECTOR-PDF-{version}-win-x64.zip'
    base = f'https://github.com/{REPOSITORY}/releases/download/{tag}/'
    assets = data.get('assets')
    if not isinstance(assets, list):
        raise ValueError('Invalid GitHub release assets.')
    urls = {}
    for asset in assets:
        if not isinstance(asset, dict):
            raise ValueError('Invalid GitHub release assets.')
        asset_name = asset.get('name')
        asset_url = asset.get('browser_download_url')
        if not isinstance(asset_name, str) or not isinstance(asset_url, str):
            raise ValueError('Invalid GitHub release assets.')
        if asset_name in urls:
            raise ValueError('Duplicate GitHub release asset.')
        urls[asset_name] = asset_url
    names = (name, name + '.sha256', 'manifest.json', 'manifest.json.sig')
    for asset_name in names:
        if urls.get(asset_name) != base + asset_name:
            raise ValueError(
                'The release does not offer a verified Windows archive.')
        trusted(urls[asset_name])
    return Release(version, *(urls[asset] for asset in names))


def _open(url, attempts=3):
    last = None
    for attempt in range(attempts):
        try:
            return fetch(url)
        except ValueError:
            raise
        except (OSError, TimeoutError) as error:
            last = error
        if attempt + 1 < attempts:
            time.sleep(0.4 * (2 ** attempt))
    raise ValueError('Unable to download update data.') from last


def _read_url(url, limit, description):
    try:
        with _open(url) as response:
            data = response.read(limit + 1)
    except (OSError, TimeoutError) as error:
        raise ValueError(f'Unable to download {description}.') from error
    if len(data) > limit:
        raise ValueError(f'The {description} exceeds its size limit.')
    return data


def check():
    """Return the latest complete Release, or None when already current."""
    raw = _read_url(f'https://api.github.com/repos/{REPOSITORY}/releases/latest',
                    1024**2, 'release information')
    try:
        data = json.loads(raw.decode('utf-8'),
                          object_pairs_hook=_unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError('Invalid response from the update server.') from error
    return release_assets(data)



def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key.')
        result[key] = value
    return result


def canonical_json(value):
    """Canonical manifest bytes: sorted keys, compact separators, UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False).encode('utf-8')


def load_manifest(raw):
    """Parse canonical manifest JSON without trusting its contents yet."""
    if not isinstance(raw, (bytes, bytearray)):
        raise ValueError('Invalid update manifest.')
    raw = bytes(raw)
    if not raw or len(raw) > MAX_MANIFEST:
        raise ValueError('Invalid update manifest.')
    try:
        manifest = json.loads(raw.decode('utf-8'),
                              object_pairs_hook=_unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError('Invalid update manifest JSON.') from error
    if canonical_json(manifest) != raw:
        raise ValueError('The update manifest is not canonical JSON.')
    return manifest, raw


def _safe_parts(value):
    """Validate one archive/manifest path and return its components."""
    if not isinstance(value, str) or not value or len(value) > MAX_PATH_LENGTH:
        raise ValueError('An update entry has an unsafe path.')
    if '\\' in value or '\x00' in value:
        raise ValueError('An update entry has an unsafe path.')
    parts = value.split('/')
    for part in parts:
        if not part or part in ('.', '..'):
            raise ValueError('An update entry has an unsafe path.')
        if part.endswith((' ', '.')):
            raise ValueError('An update entry has an unsafe path.')
        if any(ord(char) < 32 or char in _INVALID_WINDOWS_CHARS
               for char in part):
            raise ValueError('An update entry has an unsafe path.')
        if part.split('.')[0].strip().upper() in _RESERVED_DOS:
            raise ValueError('An update entry has an unsafe path.')
    return parts


def validate_manifest(manifest, version=None):
    """Validate shape, paths, caps and the mandatory component set."""
    if not isinstance(manifest, dict):
        raise ValueError('Invalid update manifest.')
    if type(manifest.get('format')) is not int or manifest['format'] != 1:
        raise ValueError('Unsupported update manifest format.')
    declared = manifest.get('version')
    version_tuple(declared)
    if version is not None and declared != version:
        raise ValueError(
            'The update manifest version does not match the release version.')
    archive_digest = manifest.get('archive_sha256')
    if not isinstance(archive_digest, str) or \
            not _SHA256_RE.fullmatch(archive_digest):
        raise ValueError('Invalid archive SHA-256 in the update manifest.')
    entries = manifest.get('files')
    if not isinstance(entries, list) or not entries:
        raise ValueError('The update manifest has no files.')
    if len(entries) > MAX_ENTRIES:
        raise ValueError('The update manifest exceeds the entry limit.')
    files = {}
    folded = set()
    total = 0
    for entry in entries:
        if not isinstance(entry, dict) or \
                set(entry) != {'path', 'size', 'sha256'}:
            raise ValueError('Invalid file entry in the update manifest.')
        path = entry['path']
        size = entry['size']
        digest = entry['sha256']
        if not isinstance(path, str):
            raise ValueError('An update entry has an unsafe path.')
        parts = _safe_parts(path)
        normalized = '/'.join(parts)
        if normalized in ('manifest.json', 'manifest.json.sig'):
            raise ValueError('The update manifest cannot sign itself.')
        if type(size) is not int or size < 0 or size > MAX_EXPANDED:
            raise ValueError('Invalid file size in the update manifest.')
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise ValueError('Invalid file SHA-256 in the update manifest.')
        key = normalized.casefold()
        if key in folded:
            raise ValueError('The update manifest contains duplicate paths.')
        folded.add(key)
        files[normalized] = {'path': normalized, 'size': size,
                             'sha256': digest}
        total += size
        if total > MAX_EXPANDED:
            raise ValueError('The update exceeds the unpacking limit.')
    required = manifest.get('required')
    if not isinstance(required, list) or not required:
        raise ValueError('The update manifest has no required components.')
    if len(required) > MAX_ENTRIES:
        raise ValueError('The update manifest exceeds the entry limit.')
    for item in required:
        if not isinstance(item, str) or not item or len(item) > MAX_PATH_LENGTH:
            raise ValueError('Invalid required component in the update manifest.')
        is_prefix = item.endswith('/**') or item.endswith('/')
        base_item = item[:-3] if item.endswith('/**') else \
            item[:-1] if item.endswith('/') else item
        _safe_parts(base_item)
        if is_prefix:
            if not any(path.startswith(base_item + '/') for path in files):
                raise ValueError(
                    'The update manifest lists a missing required component.')
        elif base_item not in files:
            raise ValueError(
                'The update manifest lists a missing required component.')
    for name in REQUIRED_FILES:
        if name not in files:
            raise ValueError(
                'The update manifest is missing a required component.')
    for prefix in REQUIRED_PREFIXES:
        if not any(path.startswith(prefix) for path in files):
            raise ValueError(
                'The update manifest is missing a required component.')
    return manifest


def verify_signature(message, signature):
    """Verify the detached signature with the pinned public key.

    The placeholder/test key shipped in the repository is rejected: it is
    deliberately useless for authenticating a real release.
    """
    key = PUBLIC_KEY
    if isinstance(key, str):
        try:
            key = bytes.fromhex(key)
        except ValueError:
            key = b''
    elif isinstance(key, (bytes, bytearray)):
        key = bytes(key)
    else:
        key = b''
    if not key or key == update_key.PLACEHOLDER_PUBLIC_KEY:
        raise ValueError(
            'Update signing is not configured; install a production update key.')
    if not isinstance(signature, (bytes, bytearray)) or len(signature) != 64:
        raise ValueError('The update signature is invalid.')
    if not update_key.verify(key, message, bytes(signature)):
        raise ValueError('The update signature is invalid.')


def _read_checksum(release):
    data = _read_url(release.checksum, 4096, 'update checksum')
    try:
        text = data.decode('ascii')
    except UnicodeDecodeError as error:
        raise ValueError('Invalid SHA-256 checksum.') from error
    parts = text.split()
    if not parts:
        raise ValueError('Empty SHA-256 checksum.')
    digest = parts[0].lower()
    if not _SHA256_RE.fullmatch(digest):
        raise ValueError('Invalid SHA-256 checksum.')
    return digest


def _report(progress, fraction, message):
    if progress is None:
        return
    value = max(0.0, min(1.0, fraction))
    try:
        try:
            progress(value, message)
        except TypeError:
            progress(value)
    except Exception:
        pass


def _download_archive(url, path, expected, progress=None):
    digest = sha256()
    received = 0
    try:
        with _open(url) as response, Path(path).open('wb') as target:
            while data := response.read(256 * 1024):
                received += len(data)
                if received > MAX_DOWNLOAD:
                    raise ValueError(
                        'The update exceeds the download limit.')
                target.write(data)
                digest.update(data)
                _report(progress, 0.45 + 0.45 * received / MAX_DOWNLOAD,
                        'Downloading update')
    except (OSError, TimeoutError) as error:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError('Unable to download the update.') from error
    actual = digest.hexdigest()
    if actual != expected:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError('The update checksum does not match.')
    return actual


def download_release(release, directory, progress=None):
    """Download and verify the manifest, signature, checksum and archive."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    _report(progress, 0.02, 'Checking update checksum')
    expected = _read_checksum(release)
    _report(progress, 0.05, 'Verifying update manifest')
    manifest_bytes = _read_url(release.manifest, MAX_MANIFEST,
                               'update manifest')
    signature = _read_url(release.signature, 4096, 'update signature')
    manifest, canonical = load_manifest(manifest_bytes)
    # Signature first: no manifest field is used before this succeeds.
    verify_signature(canonical, signature)
    validate_manifest(manifest, release.version)
    if manifest['archive_sha256'] != expected:
        raise ValueError(
            'The update checksum does not match its signed manifest.')
    archive = directory / 'update.zip'
    _report(progress, 0.1, 'Downloading update')
    _download_archive(release.archive, archive, expected, progress)
    _report(progress, 0.92, 'Update downloaded')
    return manifest, canonical, bytes(signature), archive


def verified_file(release, path):
    """Backwards-compatible checksum-only download helper."""
    digest = _read_checksum(release)
    _download_archive(release.archive, path, digest)
    return digest


def _precheck_archive(archive_path):
    """Reject absurd archives before zipfile materializes the directory."""
    archive_path = Path(archive_path)
    try:
        size = archive_path.stat().st_size
    except OSError as error:
        raise ValueError('Invalid update archive.') from error
    if size <= 0 or size > MAX_DOWNLOAD:
        raise ValueError('Invalid update archive size.')
    try:
        with archive_path.open('rb') as stream:
            stream.seek(max(0, size - 65557))
            tail = stream.read()
    except OSError as error:
        raise ValueError('Invalid update archive.') from error
    index = tail.rfind(b'PK\x05\x06')
    if index < 0 or len(tail) - index < 22:
        raise ValueError('Invalid update archive.')
    try:
        entries, directory_size = struct.unpack_from('<HI', tail, index + 10)
    except struct.error as error:
        raise ValueError('Invalid update archive.') from error
    if entries == 0xFFFF or directory_size == 0xFFFFFFFF:
        raise ValueError('The update archive is too large.')
    if entries > MAX_ENTRIES or directory_size > MAX_CENTRAL_DIRECTORY:
        raise ValueError('The update exceeds the unpacking limit.')
    return entries


def _entry_parts(entry):
    name = entry.filename
    original = getattr(entry, 'orig_filename', name)
    if '\\' in name or '\\' in original or '\x00' in name:
        raise ValueError('An update entry has an unsafe path.')
    stripped = name[:-1] if name.endswith('/') else name
    parts = _safe_parts(stripped)
    if not parts or parts[0] != 'Vector PDF':
        raise ValueError('An update entry has an unsafe path.')
    if stat.S_ISLNK(entry.external_attr >> 16):
        raise ValueError('An update entry has an unsafe path.')
    return parts[1:], entry.is_dir()


def unpack(archive_path, destination, version, manifest=None):
    """Extract a verified archive and check every path, size and hash."""
    destination = Path(destination)
    _precheck_archive(archive_path)
    if manifest is None:
        # Keep the path-safety contract testable without a manifest: scan the
        # central directory, then fail closed because hashes are unavailable.
        with ZipFile(archive_path) as archive:
            seen = set()
            for entry in archive.infolist():
                relative_parts, _ = _entry_parts(entry)
                normalized = '/'.join(relative_parts).casefold()
                if normalized in seen:
                    raise ValueError('Duplicate update entry.')
                seen.add(normalized)
        raise ValueError('The update manifest is required.')
    validate_manifest(manifest, version)
    expected = {entry['path']: entry for entry in manifest['files']}
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise ValueError('The update staging directory must be empty.')
    destination.mkdir(parents=True, exist_ok=True)
    written = set()
    seen = set()
    try:
        with ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ENTRIES:
                raise ValueError('The update exceeds the unpacking limit.')
            total = sum(entry.file_size for entry in entries)
            if total > MAX_EXPANDED:
                raise ValueError('The update exceeds the unpacking limit.')
            for entry in entries:
                relative_parts, is_directory = _entry_parts(entry)
                relative = '/'.join(relative_parts)
                normalized = relative.casefold()
                if normalized in seen:
                    raise ValueError('Duplicate update entry.')
                seen.add(normalized)
                if is_directory:
                    destination.joinpath(*relative_parts).mkdir(
                        parents=True, exist_ok=True)
                    continue
                if relative not in expected:
                    raise ValueError(
                        'The update archive does not match its manifest.')
                declared = expected[relative]
                if entry.file_size != declared['size']:
                    raise ValueError(
                        'The update archive does not match its manifest.')
                target = destination.joinpath(*relative_parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = sha256()
                written_size = 0
                with archive.open(entry) as source, target.open('xb') as sink:
                    while data := source.read(256 * 1024):
                        written_size += len(data)
                        if written_size > declared['size']:
                            raise ValueError(
                                'The update archive does not match its manifest.')
                        sink.write(data)
                        digest.update(data)
                if written_size != declared['size'] or \
                        digest.hexdigest() != declared['sha256']:
                    raise ValueError(
                        'The update archive does not match its manifest.')
                written.add(relative)
    except (BadZipFile, RuntimeError, NotImplementedError, EOFError) as error:
        raise ValueError('Invalid update archive.') from error
    if written != set(expected):
        raise ValueError('The update archive does not match its manifest.')
    return manifest



def _file_sha256(path):
    digest = sha256()
    with Path(path).open('rb') as stream:
        while data := stream.read(1024 * 1024):
            digest.update(data)
    return digest.hexdigest()


def check_installed(directory, version, verify_hashes=True,
                    verify_manifest_signature=False):
    """Require the complete component set and (by default) every hash."""
    directory = Path(directory)
    version_tuple(version)
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError('Incomplete portable update.')
    manifest_path = directory / 'manifest.json'
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError('Incomplete portable update: manifest.json is missing.')
    try:
        raw = manifest_path.read_bytes()
    except OSError as error:
        raise ValueError('Invalid installed update manifest.') from error
    manifest, _ = load_manifest(raw)
    validate_manifest(manifest, version)
    if verify_manifest_signature:
        signature_path = directory / 'manifest.json.sig'
        if not signature_path.is_file() or signature_path.is_symlink():
            raise ValueError(
                'Incomplete portable update: manifest.json.sig is missing.')
        try:
            signature = signature_path.read_bytes()
        except OSError as error:
            raise ValueError('Invalid installed update signature.') from error
        verify_signature(raw, signature)
    declared_file = directory / 'vector_pdf' / 'version.py'
    if not declared_file.is_file():
        raise ValueError('Incomplete portable update.')
    try:
        declared = declared_file.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError('Incomplete portable update.') from error
    if not re.search(r"^VERSION = ['\"]" + re.escape(version) + r"['\"]$",
                     declared, re.M):
        raise ValueError(
            'The package version does not match its GitHub tag.')
    files = {entry['path']: entry for entry in manifest['files']}
    for name in REQUIRED_FILES:
        path = directory.joinpath(*name.split('/'))
        if not path.is_file() or path.is_symlink():
            raise ValueError(f'Incomplete portable update: {name} is missing.')
    for prefix in REQUIRED_PREFIXES:
        if not any(path.startswith(prefix) for path in files):
            raise ValueError(
                f'Incomplete portable update: {prefix} is missing.')
    if verify_hashes:
        for entry in files.values():
            path = directory.joinpath(*entry['path'].split('/'))
            if not path.is_file() or path.is_symlink():
                raise ValueError(
                    f"Incomplete portable update: {entry['path']} is missing.")
            try:
                if path.stat().st_size != entry['size'] or \
                        _file_sha256(path) != entry['sha256']:
                    raise ValueError(
                        'The installed update has been modified.')
            except OSError as error:
                raise ValueError('The installed update is unreadable.') from error
    return manifest


@contextmanager
def update_lock(base, timeout=30.0):
    """Windows-safe O_EXCL lock with stale-lock recovery."""
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    path = base / 'update.lock'
    deadline = time.monotonic() + timeout
    descriptor = None
    while True:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                                 0o644)
            os.write(descriptor, f'{os.getpid()} {time.time():.0f}'.encode())
            break
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except FileNotFoundError:
                continue
            except OSError:
                age = 0
            if age > LOCK_STALE:
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise ValueError('Another update is already in progress.')
            time.sleep(0.1)
    try:
        yield
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            path.unlink()
        except OSError:
            pass


def _read_pointer(path):
    path = Path(path)
    try:
        text = path.read_text(encoding='ascii')
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError('Invalid update pointer.') from error
    text = text.strip()
    if not text:
        return None
    if len(text) > MAX_POINTER:
        raise ValueError('Invalid update pointer.')
    return text


def _pointer_safe(path):
    try:
        return _read_pointer(path)
    except ValueError:
        return None


def _write_pointer(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = mkstemp(dir=path.parent, prefix=path.name + '.',
                                    suffix='.tmp')
    try:
        with os.fdopen(descriptor, 'w', encoding='ascii', newline='') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass


def _valid_install(base, version):
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        return False
    try:
        check_installed(Path(base) / 'versions' / version, version)
        return True
    except (OSError, ValueError):
        return False


def _older_than(path, seconds):
    try:
        return time.time() - Path(path).stat().st_mtime > seconds
    except OSError:
        return False


def _remove_tree(path):
    path = Path(path)
    try:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _clean_incoming(base, max_age=INCOMING_RETENTION):
    removed = []
    for item in Path(base).glob('incoming-*'):
        if _older_than(item, max_age):
            _remove_tree(item)
            removed.append(item.name)
    return removed


def cleanup(base=None):
    """Retain active + previous versions; remove stale incoming/broken data."""
    base = Path(base) if base is not None else root()
    removed = []
    if not base.is_dir():
        return removed
    active = _pointer_safe(base / 'active')
    previous = _pointer_safe(base / 'previous')
    keep = {name for name in (active, previous)
            if name and name != 'base' and _VERSION_RE.fullmatch(name)}
    removed.extend(_clean_incoming(base))
    versions = base / 'versions'
    if versions.is_dir():
        for item in list(versions.iterdir()):
            name = item.name
            if name in keep:
                continue
            if _VERSION_RE.fullmatch(name):
                _remove_tree(item)
                removed.append(item.name)
            elif name.startswith('broken-'):
                if _older_than(item, BROKEN_RETENTION):
                    _remove_tree(item)
                    removed.append(name)
    for item in base.glob('*.tmp'):
        if _older_than(item, INCOMING_RETENTION):
            _remove_tree(item)
            removed.append(item.name)
    if removed:
        log('Cleanup removed: ' + ', '.join(sorted(removed)))
    return removed


def health_check(directory, timeout=60):
    """Import the staged runtime before activation; never launch the GUI."""
    directory = Path(directory)
    python = directory / 'runtime' / 'python.exe'
    if not python.is_file():
        raise ValueError('The update has no staged Python runtime.')
    script = ("import PySide6; import vector_pdf.ui, vector_pdf.writer, "
              "vector_pdf.fonts; print('ok')")
    environment = os.environ.copy()
    environment.pop('PYTHONPATH', None)
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    try:
        completed = subprocess.run(
            [str(python), '-c', script], cwd=str(directory),
            capture_output=True, text=True, timeout=timeout,
            env=environment)
    except subprocess.TimeoutExpired as error:
        raise ValueError('The update timed out during its health check.') from error
    except OSError as error:
        raise ValueError('The update could not run its health check.') from error
    if completed.returncode != 0 or 'ok' not in (completed.stdout or ''):
        detail = (completed.stderr or completed.stdout or '').strip()
        raise ValueError(
            'The update failed its health check.' +
            (f' {detail[:300]}' if detail else ''))
    return True


def _broken_path(versions, version):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    candidate = versions / f'broken-{version}-{stamp}'
    counter = 0
    while candidate.exists():
        counter += 1
        candidate = versions / f'broken-{version}-{stamp}-{counter}'
    return candidate


def _activate(base, version):
    current = _pointer_safe(base / 'active')
    existing = _pointer_safe(base / 'previous')
    if current and current != version and _valid_install(base, current):
        previous = current
    elif existing and existing != version and _valid_install(base, existing):
        previous = existing
    else:
        previous = 'base'
    _write_pointer(base / 'previous', previous)
    _write_pointer(base / 'active', version)
    return previous


def _prepare(base):
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    (base / 'versions').mkdir(parents=True, exist_ok=True)
    active_path = base / 'active'
    active = _pointer_safe(active_path)
    if active_path.exists() and (active is None or
                                 not _VERSION_RE.fullmatch(active)):
        try:
            active_path.unlink()
        except OSError:
            pass
        log('Removed an invalid active update pointer.')
    elif active and not (base / 'versions' / active).is_dir():
        try:
            active_path.unlink()
        except OSError:
            pass
        log('Removed a stale active update pointer.')
    _clean_incoming(base, 0)


def arm(release, base=None, progress=None):
    """Download, verify, health-check and activate an update."""
    base = Path(base) if base is not None else root()
    version_tuple(release.version)
    if version_tuple(release.version) <= version_tuple(VERSION):
        raise ValueError('The update is not newer than the current version.')
    try:
        with update_lock(base):
            _prepare(base)
            versions = base / 'versions'
            destination = versions / release.version
            if destination.exists():
                try:
                    check_installed(destination, release.version,
                                    verify_manifest_signature=True)
                except (OSError, ValueError) as error:
                    broken = _broken_path(versions, release.version)
                    log(f'Renaming incomplete {release.version} to '
                        f'{broken.name}: {error}')
                    try:
                        destination.rename(broken)
                    except OSError:
                        _remove_tree(destination)
            if not destination.exists():
                with TemporaryDirectory(dir=base, prefix='incoming-') as temp:
                    incoming = Path(temp)
                    manifest, canonical, signature, archive = \
                        download_release(release, incoming, progress)
                    _report(progress, 0.94, 'Unpacking update')
                    stage = incoming / 'app'
                    stage.mkdir()
                    unpack(archive, stage, release.version, manifest)
                    _report(progress, 0.96, 'Health-checking update')
                    health_check(stage)
                    (stage / 'manifest.json').write_bytes(canonical)
                    (stage / 'manifest.json.sig').write_bytes(signature)
                    check_installed(stage, release.version,
                                    verify_manifest_signature=True)
                    try:
                        stage.rename(destination)
                    except OSError:
                        if not destination.exists():
                            raise
                        check_installed(destination, release.version,
                                        verify_manifest_signature=True)
            check_installed(destination, release.version,
                            verify_manifest_signature=True)
            previous = _activate(base, release.version)
            log(f'Activated update {release.version}; previous={previous}.')
            cleanup(base)
            _report(progress, 1.0, 'Update ready')
            return release.version
    except (OSError, ValueError) as error:
        log(f'Update {getattr(release, "version", "?")} failed: {error}')
        raise


def rollback(reason, base=None):
    """Return to the previous valid version, or disable the active update."""
    base = Path(base) if base is not None else root()
    previous = _pointer_safe(base / 'previous')
    target = None
    if previous and previous != 'base' and _valid_install(base, previous):
        target = previous
    try:
        if target:
            _write_pointer(base / 'active', target)
            _write_pointer(base / 'previous', 'base')
            log(f'Rolled back to {target}: {reason}')
        else:
            try:
                (base / 'active').unlink(missing_ok=True)
            except OSError:
                pass
            log(f'Disabled the active update; the portable version starts: '
                f'{reason}')
    except OSError as error:
        log(f'Rollback could not update the active pointer: {error}')
    return target


def reset_update(base=None):
    """Remove update pointers so the portable base application starts."""
    base = Path(base) if base is not None else root()
    try:
        with update_lock(base, timeout=5):
            (base / 'active').unlink(missing_ok=True)
            (base / 'previous').unlink(missing_ok=True)
            for item in (base / 'versions').glob('broken-*'):
                _remove_tree(item)
            _clean_incoming(base, 0)
        log('Update state reset by request.')
    except (OSError, ValueError) as error:
        log(f'Update reset was incomplete: {error}')
        try:
            (base / 'active').unlink(missing_ok=True)
        except OSError:
            pass
    return False


@dataclass(frozen=True)
class LaunchOptions:
    safe_mode: bool
    reset_update: bool
    ready_token: str | None
    paths: tuple[str, ...]


def parse_launch_arguments(argv=None):
    """Strip updater control switches and leave file arguments untouched."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    safe_mode = (os.environ.get('VECTORPDF_SAFE_MODE', '').casefold()
                 in {'1', 'true', 'yes', 'on'})
    reset = False
    token = None
    paths = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == '--safe-mode':
            safe_mode = True
        elif argument == '--reset-update':
            reset = True
        elif argument == '--ready-token':
            index += 1
            if index >= len(arguments) or not arguments[index]:
                raise ValueError('The readiness token path is missing.')
            token = arguments[index]
        elif argument.startswith('--ready-token='):
            token = argument.split('=', 1)[1]
            if not token:
                raise ValueError('The readiness token path is missing.')
        else:
            paths.append(argument)
        index += 1
    return LaunchOptions(safe_mode, reset, token, tuple(paths))


def record_ready(token=None, version=VERSION):
    """Write the one-time readiness token after the new window is shown."""
    if not token:
        return
    try:
        _write_pointer(Path(token),
                       f'version={version}\npid={os.getpid()}\n')
    except OSError:
        log('Could not write the update readiness token.')


def _token_ready(token_path, version, pid):
    try:
        text = Path(token_path).read_text(encoding='utf-8')[:4096]
    except (OSError, UnicodeDecodeError):
        return None
    fields = {}
    stripped = text.strip()
    if stripped.startswith('{'):
        try:
            document = json.loads(stripped)
        except json.JSONDecodeError:
            return False
        if isinstance(document, dict):
            fields = {str(key): str(value)
                      for key, value in document.items()}
    else:
        for line in text.splitlines():
            if '=' in line:
                key, value = line.split('=', 1)
                fields[key.strip()] = value.strip()
    if fields.get('version') != version:
        return False
    reported = fields.get('pid')
    return reported in (None, str(pid))


def dispatch(argv=None, base=None, ready_timeout=READY_TIMEOUT):
    """Hand off to the active update before Qt loads.

    Returns True only after the child writes a matching readiness token.  On
    any failure the child is terminated and ``rollback`` removes or reverts the
    active pointer so the portable application can start.
    """
    if sys.platform != 'win32' or 'LOCALAPPDATA' not in os.environ:
        return False
    base = Path(base) if base is not None else root()
    try:
        options = parse_launch_arguments(sys.argv[1:] if argv is None else argv)
    except ValueError as error:
        log(f'Ignoring invalid launch arguments: {error}')
        return False
    if options.safe_mode:
        log('Safe mode requested; automatic updates are disabled.')
        return False
    if options.reset_update:
        return reset_update(base)
    try:
        cleanup(base)
        active = _read_pointer(base / 'active')
        if not active:
            return False
        if not _VERSION_RE.fullmatch(active):
            raise ValueError('The active update pointer is invalid.')
        if version_tuple(active) <= version_tuple(VERSION):
            return False
        directory = base / 'versions' / active
        # Re-verify every hash and the installed signature immediately before
        # handing off.
        check_installed(directory, active, verify_hashes=True,
                        verify_manifest_signature=True)
        runtime = directory / 'runtime' / 'pythonw.exe'
        if not runtime.is_file():
            raise ValueError('The update runtime is missing.')
        token_path = base / f'ready-{uuid4().hex}.token'
        try:
            token_path.unlink(missing_ok=True)
        except OSError:
            pass
        command = [str(runtime), '-m', 'vector_pdf', *options.paths,
                   '--ready-token', str(token_path)]
        if sum(len(part) + 3 for part in command) > 30_000:
            raise ValueError('The command line for the update is too long.')
        log(f'Starting update {active}.')
        child = subprocess.Popen(command)
    except (OSError, ValueError) as error:
        rollback(f'Could not launch the update: {error}', base)
        return False
    ready = False
    detail = ''
    try:
        try:
            limit = max(0.1, float(ready_timeout))
        except (TypeError, ValueError):
            limit = READY_TIMEOUT
        deadline = time.monotonic() + limit
        while True:
            if token_path.exists():
                if _token_ready(token_path, active, child.pid):
                    ready = True
                else:
                    detail = 'the readiness token did not match'
                break
            code = child.poll()
            if code is not None:
                detail = (f'the updated application exited during startup '
                          f'(code {code})')
                break
            if time.monotonic() >= deadline:
                detail = 'timed out waiting for the updated application'
                break
            time.sleep(0.05)
    except OSError as error:
        detail = f'readiness check failed: {error}'
    finally:
        try:
            token_path.unlink(missing_ok=True)
        except OSError:
            pass
    if ready:
        log(f'Update {active} confirmed startup (pid {child.pid}).')
        return True
    try:
        child.terminate()
    except Exception:
        pass
    try:
        child.wait(timeout=5)
    except Exception:
        try:
            child.kill()
        except Exception:
            pass
    rollback(f'Update {active} failed to start: {detail}', base)
    return False
