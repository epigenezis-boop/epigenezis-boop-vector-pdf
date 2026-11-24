"""Failure-injection and tamper tests for the signed update chain."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from zipfile import ZipFile

import pytest

from vector_pdf import update_key, updater

ROOT = Path(__file__).resolve().parents[1]
TEST_SEED = sha256(b'VECTOR PDF updater test key v1').digest()
OTHER_SEED = sha256(b'VECTOR PDF updater other test key v1').digest()
PUBLIC = update_key.public_key(TEST_SEED)


@pytest.fixture(autouse=True)
def pinned_application_version(monkeypatch):
    """Keep these update scenarios valid after the product version is bumped."""
    monkeypatch.setattr(updater, 'VERSION', '0.2.1')


def release_data(version='0.3.0'):
    name = f'VECTOR-PDF-{version}-win-x64.zip'
    base = f'https://github.com/{updater.REPOSITORY}/releases/download/v{version}/'
    names = (name, name + '.sha256', 'manifest.json', 'manifest.json.sig')
    return {
        'tag_name': 'v' + version,
        'draft': False,
        'prerelease': False,
        'assets': [{'name': part, 'browser_download_url': base + part}
                   for part in names],
    }


def bundle_files(version='0.3.0', omit=(), extra=None):
    files = {
        'VectorPDF.exe': b'MZ launcher',
        'runtime/pythonw.exe': b'pythonw',
        'runtime/python.exe': b'python',
        'runtime/python312.dll': b'python312.dll',
        'runtime/python312.zip': b'python312.zip',
        'runtime/python312._pth':
            b'python312.zip\n.\nLib/site-packages\n..\nimport site\n',
        'runtime/qt.conf': b'[Paths]\nPlugins = Lib/site-packages/PySide6/plugins\n',
        'runtime/Lib/site-packages/PySide6/Qt6Core.dll': b'core',
        'runtime/Lib/site-packages/PySide6/Qt6Gui.dll': b'gui',
        'runtime/Lib/site-packages/PySide6/Qt6Widgets.dll': b'widgets',
        'runtime/Lib/site-packages/PySide6/plugins/platforms/qwindows.dll': b'qwindows',
        'runtime/Lib/site-packages/PySide6/plugins/platforms/qoffscreen.dll': b'qoffscreen',
        'vector_pdf/__main__.py': b'',
        'vector_pdf/ui.py': b'',
        'vector_pdf/writer.py': b'',
        'vector_pdf/fonts.py': b'',
        'vector_pdf/version.py': f"VERSION = '{version}'\n".encode(),
        'vector_pdf/assets/icon.png': b'png',
        'licenses/LICENSE': b'MIT',
    }
    for name in omit:
        files.pop(name, None)
    if extra:
        files.update(extra)
    return files


def zip_bytes(files, root='Vector PDF'):
    buffer = BytesIO()
    with ZipFile(buffer, 'w') as archive:
        for path in sorted(files):
            archive.writestr(f'{root}/{path}', files[path])
    return buffer.getvalue()


def make_manifest(files, version, archive_digest='0' * 64, required=None):
    entries = [
        {'path': path, 'size': len(data),
         'sha256': sha256(data).hexdigest()}
        for path, data in sorted(files.items())
    ]
    if required is None:
        required = list(updater.REQUIRED_FILES)
        required += [prefix + '**' for prefix in updater.REQUIRED_PREFIXES]
    return {
        'format': 1,
        'version': version,
        'archive_sha256': archive_digest,
        'files': entries,
        'required': required,
    }


def payload_for(version='0.3.0', files=None, archive_files=None,
                manifest_files=None, archive_digest=None, manifest_edit=None,
                signature_edit=None, signer_seed=TEST_SEED,
                checksum_override=None, raw_edit=None):
    files = dict(files if files is not None else bundle_files(version))
    archive_files = dict(files if archive_files is None else archive_files)
    manifest_files = dict(files if manifest_files is None else manifest_files)
    archive = zip_bytes(archive_files)
    digest = sha256(archive).hexdigest()
    manifest = make_manifest(manifest_files, version,
                             digest if archive_digest is None else archive_digest)
    if manifest_edit:
        manifest_edit(manifest)
    raw = updater.canonical_json(manifest)
    if raw_edit:
        raw = raw_edit(raw, manifest)
    signature = update_key.sign(signer_seed, raw)
    if signature_edit:
        signature = signature_edit(signature)
    release = updater.release_assets(release_data(version))
    checksum = checksum_override
    if checksum is None:
        checksum = (digest + '  archive.zip\n').encode()
    payloads = {
        release.checksum: checksum,
        release.manifest: raw,
        release.signature: signature,
        release.archive: archive,
    }
    return release, payloads


def install(monkeypatch, base, release, payloads, health=None):
    monkeypatch.setattr(updater, 'PUBLIC_KEY', PUBLIC)
    monkeypatch.setattr(updater, 'LOG_PATH', Path(base) / 'update.log')
    monkeypatch.setattr(updater, 'fetch',
                        lambda url: BytesIO(payloads[url]))
    if health is None:
        monkeypatch.setattr(updater, 'health_check',
                            lambda directory, timeout=60: True)
    else:
        monkeypatch.setattr(updater, 'health_check', health)
    return updater.arm(release, base)


def log_text(base):
    return (Path(base) / 'update.log').read_text(encoding='utf-8')


# ---------------------------------------------------------------------------
# Ed25519 and canonical manifest
# ---------------------------------------------------------------------------

RFC_VECTORS = (
    (
        '9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60',
        'd75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a',
        '',
        'e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b',
    ),
    (
        '4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb',
        '3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c',
        '72',
        '92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00',
    ),
    (
        'c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7',
        'fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025',
        'af82',
        '6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a',
    ),
)


@pytest.mark.parametrize('seed_hex,public_hex,message_hex,signature_hex',
                         RFC_VECTORS)
def test_ed25519_rfc8032_vectors(seed_hex, public_hex, message_hex,
                                 signature_hex):
    seed = bytes.fromhex(seed_hex)
    public = bytes.fromhex(public_hex)
    message = bytes.fromhex(message_hex)
    signature = bytes.fromhex(signature_hex)
    assert update_key.public_key(seed) == public
    assert update_key.sign(seed, message) == signature
    assert update_key.verify(public, message, signature)
    assert not update_key.verify(public, message + b'!', signature)
    assert not update_key.verify(public, message,
                                 bytes([signature[0] ^ 1]) + signature[1:])
    assert not update_key.verify(public, message, signature[:63])
    assert not update_key.verify(b'', message, signature)
    too_large = signature[:32] + (
        update_key._L).to_bytes(32, 'little')
    assert not update_key.verify(public, message, too_large)


def test_canonical_json_and_placeholder_key_fails_closed(monkeypatch):
    value = {'b': 2, 'a': 1}
    assert updater.canonical_json(value) == b'{"a":1,"b":2}'
    monkeypatch.setattr(updater, 'PUBLIC_KEY', update_key.PLACEHOLDER_PUBLIC_KEY)
    with pytest.raises(ValueError, match='not configured'):
        updater.verify_signature(b'{}', b'\0' * 64)
    monkeypatch.setattr(updater, 'PUBLIC_KEY', PUBLIC)
    signature = update_key.sign(TEST_SEED, b'{}')
    updater.verify_signature(b'{}', signature)
    with pytest.raises(ValueError, match='signature is invalid'):
        updater.verify_signature(b'{"a":1}', signature)


# ---------------------------------------------------------------------------
# Release discovery and network normalization
# ---------------------------------------------------------------------------

def test_release_requires_four_exact_assets():
    assert updater.release_assets(release_data()).version == '0.3.0'
    for asset in ('VECTOR-PDF-0.3.0-win-x64.zip',
                  'VECTOR-PDF-0.3.0-win-x64.zip.sha256',
                  'manifest.json', 'manifest.json.sig'):
        data = deepcopy(release_data())
        data['assets'] = [item for item in data['assets']
                          if item['name'] != asset]
        with pytest.raises(ValueError, match='verified'):
            updater.release_assets(data)
        data = deepcopy(release_data())
        for item in data['assets']:
            if item['name'] == asset:
                item['browser_download_url'] = 'https://elsewhere.example/x'
        with pytest.raises(ValueError, match='verified'):
            updater.release_assets(data)


def test_release_ignores_old_and_prerelease_versions():
    assert updater.release_assets(release_data('0.1.0')) is None
    assert updater.release_assets(
        dict(release_data(), prerelease=True)) is None
    assert updater.release_assets(dict(release_data(), draft=True)) is None


def test_malformed_release_json_and_tag():
    with pytest.raises(ValueError):
        updater.release_assets([])
    with pytest.raises(ValueError):
        updater.release_assets({'tag_name': 'bad', 'assets': []})
    with pytest.raises(ValueError):
        updater.release_assets({'tag_name': 'v0.3.0', 'assets': 'nope'})


@pytest.mark.parametrize('url', [
    'http://github.com/x',
    'https://github.com.evil.example/x',
    'https://evil.example/x',
    'https://github.com:444/x',
    'https://user@github.com/x',
    'https://github.com/x#fragment',
])
def test_trusted_rejects_bad_origins(url):
    with pytest.raises(ValueError, match='origin'):
        updater.trusted(url)


def test_trusted_accepts_case_normalized_default_port():
    assert updater.trusted('https://GitHub.com:443/x')
    assert updater.trusted('https://API.GITHUB.COM/x')
    assert updater.trusted('https://github-releases.githubusercontent.com/x')


def test_check_normalizes_empty_and_malformed_responses(monkeypatch):
    monkeypatch.setattr(updater, 'fetch', lambda url: BytesIO(b''))
    with pytest.raises(ValueError):
        updater.check()
    monkeypatch.setattr(updater, 'fetch', lambda url: BytesIO(b'{not json'))
    with pytest.raises(ValueError):
        updater.check()
    monkeypatch.setattr(updater, 'fetch',
                        lambda url: BytesIO(json.dumps(
                            release_data()).encode()))
    assert updater.check().version == '0.3.0'


# ---------------------------------------------------------------------------
# Full signed install
# ---------------------------------------------------------------------------

def test_arm_installs_signed_release(tmp_path, monkeypatch):
    release, payloads = payload_for()
    assert install(monkeypatch, tmp_path, release, payloads) == '0.3.0'
    assert (tmp_path / 'active').read_text(encoding='ascii') == '0.3.0'
    assert (tmp_path / 'previous').read_text(encoding='ascii') == 'base'
    installed = tmp_path / 'versions' / '0.3.0'
    assert (installed / 'manifest.json').is_file()
    assert (installed / 'manifest.json.sig').stat().st_size == 64
    updater.check_installed(installed, '0.3.0')
    updater.check_installed(installed, '0.3.0',
                           verify_manifest_signature=True)
    assert not list(tmp_path.glob('incoming-*'))
    assert 'Activated update 0.3.0' in log_text(tmp_path)


def test_arm_rejects_tampered_manifest_signature(tmp_path, monkeypatch):
    release, payloads = payload_for()
    manifest = json.loads(payloads[release.manifest])
    manifest['files'][0]['size'] += 1
    payloads[release.manifest] = updater.canonical_json(manifest)
    with pytest.raises(ValueError, match='signature is invalid'):
        install(monkeypatch, tmp_path, release, payloads)
    assert not (tmp_path / 'active').exists()


def test_arm_rejects_tampered_archive(tmp_path, monkeypatch):
    release, payloads = payload_for()
    payloads[release.archive] += b'changed'
    with pytest.raises(ValueError, match='checksum does not match'):
        install(monkeypatch, tmp_path, release, payloads)
    assert not (tmp_path / 'active').exists()


def test_arm_rejects_empty_checksum_without_index_error(tmp_path, monkeypatch):
    release, payloads = payload_for(checksum_override=b'')
    with pytest.raises(ValueError, match='Empty SHA-256'):
        install(monkeypatch, tmp_path, release, payloads)


def test_arm_rejects_bad_checksum(tmp_path, monkeypatch):
    release, payloads = payload_for(checksum_override=b'0' * 64)
    with pytest.raises(ValueError, match='checksum does not match'):
        install(monkeypatch, tmp_path, release, payloads)


def test_arm_rejects_malformed_manifest(tmp_path, monkeypatch):
    release, payloads = payload_for()
    payloads[release.manifest] = b'{not json'
    with pytest.raises(ValueError, match='manifest JSON'):
        install(monkeypatch, tmp_path, release, payloads)


def test_arm_rejects_noncanonical_manifest(tmp_path, monkeypatch):
    release, payloads = payload_for()
    payloads[release.manifest] = payloads[release.manifest].replace(
        b'","', b'", "')
    with pytest.raises(ValueError, match='canonical'):
        install(monkeypatch, tmp_path, release, payloads)


def test_arm_rejects_manifest_version_mismatch(tmp_path, monkeypatch):
    release, payloads = payload_for(
        manifest_edit=lambda manifest: manifest.__setitem__('version', '9.9.9'))
    with pytest.raises(ValueError, match='version does not match'):
        install(monkeypatch, tmp_path, release, payloads)


def test_arm_rejects_manifest_archive_mismatch(tmp_path, monkeypatch):
    release, payloads = payload_for(archive_digest='0' * 64)
    with pytest.raises(ValueError, match='signed manifest'):
        install(monkeypatch, tmp_path, release, payloads)


def test_signature_is_checked_before_manifest_contents(tmp_path, monkeypatch):
    files = bundle_files(omit=('runtime/python312.zip',))
    release, payloads = payload_for(files=files, signer_seed=OTHER_SEED)
    with pytest.raises(ValueError, match='signature is invalid'):
        install(monkeypatch, tmp_path, release, payloads)


@pytest.mark.parametrize('missing', [
    'runtime/python312.zip',
    'runtime/Lib/site-packages/PySide6/Qt6Widgets.dll',
    'runtime/Lib/site-packages/PySide6/plugins/platforms/qwindows.dll',
    'vector_pdf/ui.py',
    'vector_pdf/assets/icon.png',
    'licenses/LICENSE',
])
def test_manifest_requires_complete_component_set(tmp_path, monkeypatch,
                                                  missing):
    files = bundle_files(omit=(missing,))
    release, payloads = payload_for(files=files)
    with pytest.raises(ValueError, match='required component'):
        install(monkeypatch, tmp_path, release, payloads)


# ---------------------------------------------------------------------------
# Archive file-set validation
# ---------------------------------------------------------------------------

def test_unpack_rejects_extra_and_missing_files(tmp_path):
    files = bundle_files()
    version = '0.3.0'
    archive = zip_bytes(files)
    digest = sha256(archive).hexdigest()
    manifest = make_manifest(files, version, digest)

    extra = tmp_path / 'extra.zip'
    extra.write_bytes(zip_bytes({**files, 'vector_pdf/evil.py': b'evil'}))
    with pytest.raises(ValueError, match='does not match its manifest'):
        updater.unpack(extra, tmp_path / 'extra', version, manifest)

    missing = tmp_path / 'missing.zip'
    missing_files = dict(files)
    missing_files.pop('vector_pdf/writer.py')
    missing.write_bytes(zip_bytes(missing_files))
    with pytest.raises(ValueError, match='does not match its manifest'):
        updater.unpack(missing, tmp_path / 'missing', version, manifest)


def test_unpack_rejects_size_and_hash_mismatch(tmp_path):
    files = bundle_files()
    version = '0.3.0'
    archive = tmp_path / 'update.zip'
    archive.write_bytes(zip_bytes(files))
    digest = sha256(archive.read_bytes()).hexdigest()
    target = 'runtime/qt.conf'
    original = files[target]

    declared = dict(files)
    declared[target] = b'x' * (len(original) + 1)
    manifest = make_manifest(declared, version, digest)
    with pytest.raises(ValueError, match='does not match its manifest'):
        updater.unpack(archive, tmp_path / 'size', version, manifest)

    same_size = dict(files)
    same_size[target] = b'X' * len(original)
    manifest = make_manifest(same_size, version, digest)
    with pytest.raises(ValueError, match='does not match its manifest'):
        updater.unpack(archive, tmp_path / 'hash', version, manifest)


@pytest.mark.parametrize('bad_path', [
    '../escape',
    'vector_pdf/../escape',
    'vector_pdf/ui.py:stream',
    '/absolute',
    'vector_pdf/CON.txt',
    'vector_pdf/CONIN$.txt',
    'vector_pdf/CONOUT$.txt',
    'vector_pdf/CLOCK$.txt',
    'vector_pdf/COM0.txt',
    'vector_pdf/COM¹.txt',
    'vector_pdf/LPT¹.txt',
    'vector_pdf/trailing.',
    'vector_pdf/trailing ',
    'vector_pdf\\ui.py',
    'vector_pdf/\x00.py',
])
def test_manifest_rejects_unsafe_paths(bad_path):
    files = bundle_files(extra={bad_path: b'bad'})
    manifest = make_manifest(files, '0.3.0', '0' * 64)
    with pytest.raises(ValueError, match='unsafe path'):
        updater.validate_manifest(manifest, '0.3.0')


def test_manifest_rejects_duplicate_casefold_paths():
    files = bundle_files(extra={'vector_pdf/UI.py': b'duplicate'})
    manifest = make_manifest(files, '0.3.0', '0' * 64)
    with pytest.raises(ValueError, match='duplicate'):
        updater.validate_manifest(manifest, '0.3.0')


def test_unpack_rejects_unsafe_archive_paths(tmp_path):
    path = tmp_path / 'unsafe.zip'
    with ZipFile(path, 'w') as archive:
        archive.writestr('Vector PDF/../escape', b'')
        archive.writestr('Vector PDF/runtime/setup.exe:stream', b'')
        archive.writestr('Vector PDF/runtime/CON.txt', b'')
        archive.writestr('another-root/VectorPDF.exe', b'')
    with pytest.raises(ValueError, match='unsafe|manifest'):
        updater.unpack(path, tmp_path / 'out', '0.3.0')


def test_unpack_rejects_symlink_entry(tmp_path):
    from zipfile import ZipInfo

    path = tmp_path / 'symlink.zip'
    with ZipFile(path, 'w') as archive:
        info = ZipInfo('Vector PDF/vector_pdf/link.py')
        info.external_attr = 0o120777 << 16
        archive.writestr(info, b'target')
    with pytest.raises(ValueError, match='unsafe'):
        updater.unpack(path, tmp_path / 'out', '0.3.0')


# ---------------------------------------------------------------------------
# Installed-version checks
# ---------------------------------------------------------------------------

def test_check_installed_requires_manifest_and_hashes(tmp_path, monkeypatch):
    release, payloads = payload_for()
    install(monkeypatch, tmp_path, release, payloads)
    installed = tmp_path / 'versions' / '0.3.0'
    updater.check_installed(installed, '0.3.0')

    with pytest.raises(ValueError, match='manifest.json is missing'):
        updater.check_installed(tmp_path, '0.3.0')

    cases = tmp_path / 'cases'
    cases.mkdir()

    def case(name):
        target = cases / name
        shutil.copytree(installed, target)
        return target

    missing_manifest = case('missing-manifest')
    (missing_manifest / 'manifest.json').unlink()
    with pytest.raises(ValueError, match='manifest.json is missing'):
        updater.check_installed(missing_manifest, '0.3.0')

    tampered = case('tampered')
    with (tampered / 'vector_pdf/ui.py').open('ab') as stream:
        stream.write(b'tampered')
    with pytest.raises(ValueError, match='modified'):
        updater.check_installed(tampered, '0.3.0')

    missing_license = case('missing-license')
    (missing_license / 'licenses/LICENSE').unlink()
    with pytest.raises(ValueError, match='missing'):
        updater.check_installed(missing_license, '0.3.0')

    wrong_version = case('wrong-version')
    (wrong_version / 'vector_pdf/version.py').write_text(
        "VERSION = '9.9.9'\n", encoding='utf-8')
    with pytest.raises(ValueError, match='modified|version does not match'):
        updater.check_installed(wrong_version, '0.3.0')


def test_check_installed_requires_full_component_set(tmp_path, monkeypatch):
    release, payloads = payload_for()
    install(monkeypatch, tmp_path, release, payloads)
    installed = tmp_path / 'versions' / '0.3.0'
    (installed / 'runtime/python312.zip').unlink()
    with pytest.raises(ValueError, match='missing'):
        updater.check_installed(installed, '0.3.0')


def test_partial_existing_version_is_renamed_and_reinstalled(tmp_path,
                                                             monkeypatch):
    partial = tmp_path / 'versions' / '0.3.0'
    partial.mkdir(parents=True)
    (partial / 'VectorPDF.exe').write_bytes(b'partial')
    release, payloads = payload_for()
    install(monkeypatch, tmp_path, release, payloads)
    assert (tmp_path / 'versions' / '0.3.0' / 'manifest.json').is_file()
    assert list((tmp_path / 'versions').glob('broken-0.3.0-*'))
    updater.check_installed(tmp_path / 'versions' / '0.3.0', '0.3.0')


def test_interrupted_incoming_is_cleaned(tmp_path, monkeypatch):
    stale = tmp_path / 'incoming-stale'
    (stale / 'app').mkdir(parents=True)
    release, payloads = payload_for()
    install(monkeypatch, tmp_path, release, payloads)
    assert not (tmp_path / 'incoming-stale').exists()
    assert not list(tmp_path.glob('incoming-*'))

    old = tmp_path / 'incoming-old'
    old.mkdir()
    stamp = time.time() - updater.INCOMING_RETENTION - 60
    os.utime(old, (stamp, stamp))
    updater.cleanup(tmp_path)
    assert not old.exists()


def test_retention_keeps_active_and_previous(tmp_path):
    versions = tmp_path / 'versions'
    for version in ('0.2.0', '0.3.0', '0.4.0'):
        (versions / version).mkdir(parents=True)
    (tmp_path / 'active').write_text('0.4.0', encoding='ascii')
    (tmp_path / 'previous').write_text('0.3.0', encoding='ascii')
    updater.cleanup(tmp_path)
    assert (versions / '0.3.0').is_dir()
    assert (versions / '0.4.0').is_dir()
    assert not (versions / '0.2.0').exists()


def test_fresh_broken_version_is_retained(tmp_path):
    broken = tmp_path / 'versions' / 'broken-0.3.0-20260101T000000Z'
    broken.mkdir(parents=True)
    (tmp_path / 'active').write_text('0.4.0', encoding='ascii')
    updater.cleanup(tmp_path)
    assert broken.is_dir()
    old = time.time() - updater.BROKEN_RETENTION - 60
    os.utime(broken, (old, old))
    updater.cleanup(tmp_path)
    assert not broken.exists()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def test_health_check_runs_staged_runtime(tmp_path, monkeypatch):
    (tmp_path / 'runtime').mkdir()
    (tmp_path / 'runtime/python.exe').write_bytes(b'')
    calls = {}

    class Completed:
        returncode = 0
        stdout = 'ok\n'
        stderr = ''

    def run(command, **kwargs):
        calls['command'] = command
        calls.update(kwargs)
        return Completed()

    monkeypatch.setattr(updater.subprocess, 'run', run)
    assert updater.health_check(tmp_path, timeout=7)
    assert calls['command'][0].endswith('python.exe')
    assert calls['command'][1] == '-c'
    assert 'import PySide6' in calls['command'][2]
    assert calls['cwd'] == str(tmp_path)
    assert calls['timeout'] == 7
    assert calls['env']['PYTHONDONTWRITEBYTECODE'] == '1'

    class Failed:
        returncode = 1
        stdout = ''
        stderr = 'ImportError: PySide6 missing'

    monkeypatch.setattr(updater.subprocess, 'run', lambda *a, **k: Failed())
    with pytest.raises(ValueError, match='failed its health check'):
        updater.health_check(tmp_path)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('python.exe', 1)

    monkeypatch.setattr(updater.subprocess, 'run', timeout)
    with pytest.raises(ValueError, match='timed out'):
        updater.health_check(tmp_path)


def test_health_check_failure_prevents_activation(tmp_path, monkeypatch):
    release, payloads = payload_for('0.3.0')
    install(monkeypatch, tmp_path, release, payloads)
    assert (tmp_path / 'active').read_text(encoding='ascii') == '0.3.0'

    release4, payload4 = payload_for('0.4.0')

    def broken(directory, timeout=60):
        raise ValueError('The update failed its health check.')

    with pytest.raises(ValueError, match='health check'):
        install(monkeypatch, tmp_path, release4, payload4, health=broken)
    assert (tmp_path / 'active').read_text(encoding='ascii') == '0.3.0'
    assert not (tmp_path / 'versions' / '0.4.0').exists()
    assert 'failed its health check' in log_text(tmp_path)


# ---------------------------------------------------------------------------
# Dispatch, readiness handshake, rollback, safe mode
# ---------------------------------------------------------------------------

class TimeoutChild:
    pid = 4321

    def __init__(self, command):
        self.command = command
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


class ReadyChild:
    pid = 4322

    def __init__(self, command):
        self.command = command
        self.terminated = False
        token = Path(command[command.index('--ready-token') + 1])
        token.write_text(f'version=0.3.0\npid={self.pid}\n',
                         encoding='utf-8')

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def _dispatch_env(monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setattr(updater, 'LOG_PATH', tmp_path / 'update.log')
    monkeypatch.delenv('VECTORPDF_SAFE_MODE', raising=False)


def test_dispatch_timeout_rolls_back_to_base(tmp_path, monkeypatch):
    release, payloads = payload_for()
    install(monkeypatch, tmp_path, release, payloads)
    _dispatch_env(monkeypatch, tmp_path)
    child = []

    def popen(command):
        child.append(TimeoutChild(command))
        return child[-1]

    monkeypatch.setattr(updater.subprocess, 'Popen', popen)
    assert updater.dispatch(argv=[], base=tmp_path,
                            ready_timeout=0.15) is False
    assert not (tmp_path / 'active').exists()
    assert (tmp_path / 'previous').read_text(encoding='ascii') == 'base'
    assert child[0].terminated
    assert 'failed to start' in log_text(tmp_path)


def test_dispatch_readiness_token_success(tmp_path, monkeypatch):
    release, payloads = payload_for()
    install(monkeypatch, tmp_path, release, payloads)
    _dispatch_env(monkeypatch, tmp_path)
    commands = []

    def popen(command):
        commands.append(command)
        return ReadyChild(command)

    monkeypatch.setattr(updater.subprocess, 'Popen', popen)
    assert updater.dispatch(argv=['drawing.pdf'], base=tmp_path,
                            ready_timeout=2) is True
    assert (tmp_path / 'active').read_text(encoding='ascii') == '0.3.0'
    assert 'drawing.pdf' in commands[0]
    assert list(tmp_path.glob('ready-*.token')) == []


def test_dispatch_reverifies_hashes_before_launch(tmp_path, monkeypatch):
    release, payloads = payload_for()
    install(monkeypatch, tmp_path, release, payloads)
    _dispatch_env(monkeypatch, tmp_path)
    installed = tmp_path / 'versions' / '0.3.0'
    with (installed / 'vector_pdf/ui.py').open('ab') as stream:
        stream.write(b'tampered')
    launched = []

    def popen(command):
        launched.append(command)
        return TimeoutChild(command)

    monkeypatch.setattr(updater.subprocess, 'Popen', popen)
    assert updater.dispatch(argv=[], base=tmp_path,
                            ready_timeout=0.2) is False
    assert launched == []
    assert not (tmp_path / 'active').exists()


def test_dispatch_rejects_rehashed_manifest_without_valid_signature(
        tmp_path, monkeypatch):
    release, payloads = payload_for()
    install(monkeypatch, tmp_path, release, payloads)
    _dispatch_env(monkeypatch, tmp_path)
    installed = tmp_path / 'versions' / '0.3.0'
    data = b'evil'
    (installed / 'vector_pdf/ui.py').write_bytes(data)
    manifest_path = installed / 'manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    for entry in manifest['files']:
        if entry['path'] == 'vector_pdf/ui.py':
            entry['size'] = len(data)
            entry['sha256'] = sha256(data).hexdigest()
    manifest_path.write_bytes(updater.canonical_json(manifest))
    launched = []

    def popen(command):
        launched.append(command)
        return TimeoutChild(command)

    monkeypatch.setattr(updater.subprocess, 'Popen', popen)
    assert updater.dispatch(argv=[], base=tmp_path,
                            ready_timeout=0.2) is False
    assert launched == []
    assert not (tmp_path / 'active').exists()
    assert 'signature is invalid' in log_text(tmp_path)


def test_dispatch_failure_rolls_back_to_previous_version(tmp_path, monkeypatch):
    release3, payload3 = payload_for('0.3.0')
    install(monkeypatch, tmp_path, release3, payload3)
    release4, payload4 = payload_for('0.4.0')
    install(monkeypatch, tmp_path, release4, payload4)
    assert (tmp_path / 'active').read_text(encoding='ascii') == '0.4.0'
    assert (tmp_path / 'previous').read_text(encoding='ascii') == '0.3.0'
    _dispatch_env(monkeypatch, tmp_path)
    monkeypatch.setattr(updater.subprocess, 'Popen',
                        lambda command: TimeoutChild(command))
    assert updater.dispatch(argv=[], base=tmp_path,
                            ready_timeout=0.15) is False
    assert (tmp_path / 'active').read_text(encoding='ascii') == '0.3.0'
    assert (tmp_path / 'previous').read_text(encoding='ascii') == 'base'


def test_dispatch_ignores_downgrade(tmp_path, monkeypatch):
    _dispatch_env(monkeypatch, tmp_path)
    (tmp_path / 'versions' / '0.2.0').mkdir(parents=True)
    (tmp_path / 'active').write_text('0.2.0', encoding='ascii')

    def popen(command):
        raise AssertionError('must not launch a downgrade')

    monkeypatch.setattr(updater.subprocess, 'Popen', popen)
    assert updater.dispatch(argv=[], base=tmp_path) is False
    assert (tmp_path / 'active').read_text(encoding='ascii') == '0.2.0'


def test_safe_mode_and_reset_update(tmp_path, monkeypatch):
    release, payloads = payload_for()
    install(monkeypatch, tmp_path, release, payloads)
    _dispatch_env(monkeypatch, tmp_path)
    monkeypatch.setenv('VECTORPDF_SAFE_MODE', '1')

    def popen(command):
        raise AssertionError('safe mode must not launch the update')

    monkeypatch.setattr(updater.subprocess, 'Popen', popen)
    assert updater.dispatch(argv=[], base=tmp_path) is False
    assert (tmp_path / 'active').read_text(encoding='ascii') == '0.3.0'
    monkeypatch.delenv('VECTORPDF_SAFE_MODE')
    assert updater.dispatch(argv=['--safe-mode'], base=tmp_path) is False
    assert (tmp_path / 'active').read_text(encoding='ascii') == '0.3.0'
    assert updater.dispatch(argv=['--reset-update'], base=tmp_path) is False
    assert not (tmp_path / 'active').exists()
    assert not (tmp_path / 'previous').exists()


def test_parse_launch_arguments():
    options = updater.parse_launch_arguments(
        ['a.pdf', '--safe-mode', '--ready-token', 'C:\\tmp\\token',
         'b.pdf'])
    assert options.safe_mode
    assert options.ready_token == 'C:\\tmp\\token'
    assert options.paths == ('a.pdf', 'b.pdf')
    options = updater.parse_launch_arguments(
        ['--ready-token=C:\\tmp\\token', '--reset-update'])
    assert options.reset_update
    assert options.ready_token == 'C:\\tmp\\token'


def test_readiness_token_formats(tmp_path):
    token = tmp_path / 'ready.token'
    token.write_text('version=0.3.0\npid=123\n', encoding='utf-8')
    assert updater._token_ready(token, '0.3.0', 123) is True
    assert updater._token_ready(token, '0.3.0', 124) is False
    token.write_text('{"version": "0.3.0", "pid": 123}', encoding='utf-8')
    assert updater._token_ready(token, '0.3.0', 123) is True
    token.write_text('version=9.9.9\npid=123\n', encoding='utf-8')
    assert updater._token_ready(token, '0.3.0', 123) is False


# ---------------------------------------------------------------------------
# Packaging helpers
# ---------------------------------------------------------------------------

def _load_packager():
    specification = importlib.util.spec_from_file_location(
        'package_windows_under_test', ROOT / 'tools/package_windows.py')
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _load_tool(name):
    specification = importlib.util.spec_from_file_location(
        f'{name}_under_test', ROOT / 'tools' / f'{name}.py')
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_update_keygen_replaces_only_the_public_block():
    module = _load_tool('update_keygen')
    source = (ROOT / 'vector_pdf/update_key.py').read_text(encoding='utf-8')
    updated = module.replace_public_key(source, '11' * 32, 'test-key-id')
    assert "KEY_ID = 'test-key-id'" in updated
    assert "PUBLIC_KEY_HEX = '" + '11' * 32 + "'" in updated
    assert 'IS_PLACEHOLDER = False' in updated
    assert 'def verify(' in updated
    compile(updated, 'update_key.py', 'exec')


def test_package_manifest_and_deterministic_archive(tmp_path):
    module = _load_packager()
    bundle = tmp_path / 'bundle'
    (bundle / 'vector_pdf/assets').mkdir(parents=True)
    (bundle / 'licenses').mkdir()
    (bundle / 'VectorPDF.exe').write_bytes(b'MZ')
    (bundle / 'vector_pdf/ui.py').write_bytes(b'')
    (bundle / 'vector_pdf/assets/icon.png').write_bytes(b'png')
    (bundle / 'licenses/LICENSE').write_bytes(b'MIT')
    manifest = module.build_manifest(bundle, '0.3.0')
    assert manifest['format'] == 1
    paths = {entry['path'] for entry in manifest['files']}
    assert 'VectorPDF.exe' in paths
    assert 'vector_pdf/ui.py' in paths
    first = module.write_archive(tmp_path / 'first.zip', bundle)
    second = module.write_archive(tmp_path / 'second.zip', bundle)
    assert first.read_bytes() == second.read_bytes()
