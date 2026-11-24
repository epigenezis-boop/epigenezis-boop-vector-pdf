from hashlib import sha256
from io import BytesIO
from zipfile import ZipFile

import pytest

from vector_pdf import update_key, updater

TEST_SEED = sha256(b'VECTOR PDF legacy updater test key v1').digest()
PUBLIC = update_key.public_key(TEST_SEED)


@pytest.fixture(autouse=True)
def pinned_application_version(monkeypatch):
    monkeypatch.setattr(updater, 'VERSION', '0.2.1')


def release_data(version='0.3.0'):
    name = f'VECTOR-PDF-{version}-win-x64.zip'
    base = f'https://github.com/{updater.REPOSITORY}/releases/download/v{version}/'
    return {'tag_name': 'v'+version, 'assets': [
        {'name': part, 'browser_download_url': base+part}
        for part in (name, name+'.sha256', 'manifest.json',
                     'manifest.json.sig')
    ]}


def bundle(version='0.3.0'):
    return {
        'VectorPDF.exe': b'MZ',
        'runtime/pythonw.exe': b'pythonw',
        'runtime/python.exe': b'python',
        'runtime/python312.dll': b'dll',
        'runtime/python312.zip': b'zip',
        'runtime/python312._pth': b'python312.zip\n.\n..\n',
        'runtime/qt.conf': b'[Paths]\n',
        'runtime/Lib/site-packages/PySide6/Qt6Core.dll': b'core',
        'runtime/Lib/site-packages/PySide6/Qt6Gui.dll': b'gui',
        'runtime/Lib/site-packages/PySide6/Qt6Widgets.dll': b'widgets',
        'runtime/Lib/site-packages/PySide6/plugins/platforms/qwindows.dll': b'win',
        'vector_pdf/__main__.py': b'',
        'vector_pdf/ui.py': b'',
        'vector_pdf/writer.py': b'',
        'vector_pdf/fonts.py': b'',
        'vector_pdf/version.py': f"VERSION = '{version}'\n".encode(),
        'vector_pdf/assets/icon.png': b'png',
        'licenses/LICENSE': b'MIT',
    }


def signed_payload(version='0.3.0'):
    files = bundle(version)
    buffer = BytesIO()
    with ZipFile(buffer, 'w') as archive:
        for path, data in sorted(files.items()):
            archive.writestr('Vector PDF/'+path, data)
    payload = buffer.getvalue()
    digest = sha256(payload).hexdigest()
    manifest = {
        'format': 1,
        'version': version,
        'archive_sha256': digest,
        'files': [{'path': path, 'size': len(data),
                   'sha256': sha256(data).hexdigest()}
                  for path, data in sorted(files.items())],
        'required': list(updater.REQUIRED_FILES) +
                    [prefix + '**' for prefix in updater.REQUIRED_PREFIXES],
    }
    raw = updater.canonical_json(manifest)
    signature = update_key.sign(TEST_SEED, raw)
    release = updater.release_assets(release_data(version))
    return release, {
        release.checksum: (digest+'  archive.zip\n').encode(),
        release.manifest: raw,
        release.signature: signature,
        release.archive: payload,
    }


def test_only_new_stable_releases_from_the_project():
    assert updater.release_assets(release_data()).version == '0.3.0'
    assert updater.release_assets(release_data('0.1.0')) is None
    assert updater.release_assets(dict(release_data(), prerelease=True)) is None
    data = release_data()
    data['assets'] = [asset for asset in data['assets']
                      if asset['name'] != 'manifest.json']
    with pytest.raises(ValueError, match='verified'):
        updater.release_assets(data)
    data = release_data()
    data['assets'][0]['browser_download_url'] = 'https://elsewhere.example/update.zip'
    with pytest.raises(ValueError, match='verified'):
        updater.release_assets(data)


def test_checksum_safe_unpack_atomic_activation(tmp_path, monkeypatch):
    release, payloads = signed_payload()
    monkeypatch.setattr(updater, 'PUBLIC_KEY', PUBLIC)
    monkeypatch.setattr(updater, 'LOG_PATH', tmp_path/'update.log')
    monkeypatch.setattr(updater, 'health_check', lambda directory, timeout=60: True)
    monkeypatch.setattr(updater, 'fetch',
                        lambda url: BytesIO(payloads[url]))
    assert updater.arm(release, tmp_path) == '0.3.0'
    assert (tmp_path/'active').read_text() == '0.3.0'
    updater.check_installed(tmp_path/'versions'/'0.3.0', '0.3.0')

    release4, payload4 = signed_payload('0.4.0')
    payload4[release4.checksum] = b'0'*64
    monkeypatch.setattr(updater, 'fetch',
                        lambda url: BytesIO(payload4[url]))
    with pytest.raises(ValueError, match='checksum does not match'):
        updater.arm(release4, tmp_path)
    assert (tmp_path/'active').read_text() == '0.3.0'


@pytest.mark.parametrize('entry', [
    'Vector PDF/../somewhere', 'Vector PDF/runtime/setup.exe:stream',
    'Vector PDF/runtime/CON.txt', 'another-root/VectorPDF.exe',
])
def test_update_rejects_unsafe_paths(tmp_path, entry):
    path = tmp_path/'inputs.zip'
    with ZipFile(path, 'w') as archive:
        archive.writestr(entry, b'')
    with pytest.raises(ValueError, match='unsafe'):
        updater.unpack(path, tmp_path/'output', '0.3.0')
