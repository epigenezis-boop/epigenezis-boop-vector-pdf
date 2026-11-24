from io import BytesIO
from zipfile import ZipFile

import pytest

from vector_pdf import updater


def release_data(version='0.3.0'):
    name = f'VECTOR-PDF-{version}-win-x64.zip'
    base = f'https://github.com/{updater.REPOSITORY}/releases/download/v{version}/'
    return {'tag_name': 'v'+version, 'assets': [
        {'name': part, 'browser_download_url': base+part} for part in (name, name+'.sha256')
    ]}


def test_only_new_stable_releases_from_the_project():
    assert updater.release_assets(release_data()).version == '0.3.0'
    assert updater.release_assets(release_data('0.1.0')) is None
    assert updater.release_assets(dict(release_data(), prerelease=True)) is None
    data = release_data()
    data['assets'][0]['browser_download_url'] = 'https://elsewhere.example/update.zip'
    with pytest.raises(ValueError, match='verified'):
        updater.release_assets(data)


def test_checksum_safe_unpack_atomic_activation(tmp_path, monkeypatch):
    from hashlib import sha256
    container = BytesIO()
    with ZipFile(container, 'w') as zipfile:
        for name in updater.REQUIRED:
            zipfile.writestr('Vector PDF/'+name,
                "VERSION = '0.3.0'\n" if name.endswith('version.py') else b'data')
    payload = container.getvalue()
    release = updater.release_assets(release_data())
    monkeypatch.setattr(updater, 'fetch', lambda url: BytesIO(
        sha256(payload).hexdigest().encode() if url.endswith('.sha256') else payload))
    assert updater.arm(release, tmp_path) == '0.3.0'
    assert (tmp_path/'active').read_text() == '0.3.0'
    updater.check_installed(tmp_path/'versions'/'0.3.0', '0.3.0')

    monkeypatch.setattr(updater, 'fetch', lambda url: BytesIO(
        b'0'*64 if url.endswith('.sha256') else payload))
    with pytest.raises(ValueError, match='checksum does not match'):
        updater.arm(updater.release_assets(release_data('0.4.0')), tmp_path)
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
