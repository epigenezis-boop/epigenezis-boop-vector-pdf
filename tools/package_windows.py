"""Construct an offline Windows package and its canonical signed-manifest input.

The bundle contains the application, tools, tests, requirements, documentation,
licenses and an optional SBOM.  The ZIP is written deterministically; the
manifest sidecar lists every packaged file with SHA-256 and the archive digest.
Signing happens separately (``tools/sign_manifest.py``) so the private key never
enters the build tree.
"""
from __future__ import annotations

from pathlib import Path
import argparse
from hashlib import sha256
import shutil
import sys
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vector_pdf import updater  # noqa: E402
from vector_pdf.version import VERSION  # noqa: E402


QT_COMPONENTS = {
    'PySide6/QtCore.pyd', 'PySide6/QtGui.pyd', 'PySide6/QtWidgets.pyd',
    'PySide6/QtTest.pyd',
    'PySide6/Qt6Core.dll', 'PySide6/Qt6Gui.dll', 'PySide6/Qt6Widgets.dll',
    'PySide6/Qt6Test.dll',
    'PySide6/pyside6.abi3.dll',
    'PySide6/plugins/platforms/qwindows.dll',
    'PySide6/plugins/platforms/qoffscreen.dll',
}

DOCUMENTS = (
    'README.md', 'ARCHITECTURE.md', 'BENCHMARKS.md', 'TEST_RESULTS.md',
    'KNOWN_LIMITATIONS.md', 'THIRD_PARTY.md', 'THIRD_PARTY_LICENSES.txt',
    'LICENSE', 'CHANGELOG.md', 'SECURITY.md', 'AUDIT_MATRIX.md',
    'QA_REPORT.md', 'QA_BASELINE.md', 'requirements.lock',
)

IGNORE = shutil.ignore_patterns(
    '__pycache__', '*.pyc', '*.pyo', '.pytest_cache', '.mypy_cache')


def include(member):
    if '__pycache__' in member or member.endswith(('.pyc', '.pyo')):
        return False
    if '.data/' in member or member.startswith(
            ('PySide6/include/', 'PySide6/typesystems/')):
        return False
    if member.startswith('PySide6/') and member.endswith('.exe'):
        return False
    if member.startswith('PySide6/') and member.endswith(('.pyd', '.dll')):
        return member in QT_COMPONENTS or Path(member).name.startswith(
            ('msvcp140', 'vcruntime140', 'concrt140'))
    return True


def file_sha256(path):
    digest = sha256()
    with Path(path).open('rb') as stream:
        while data := stream.read(1024 * 1024):
            digest.update(data)
    return digest.hexdigest()


def build_manifest(bundle, version):
    """Manifest fields except the archive digest, which is added after zipping."""
    files = []
    for path in sorted(Path(bundle).rglob('*')):
        if not path.is_file():
            continue
        relative = path.relative_to(bundle).as_posix()
        if relative in ('manifest.json', 'manifest.json.sig'):
            continue
        files.append({'path': relative, 'size': path.stat().st_size,
                      'sha256': file_sha256(path)})
    required = list(updater.REQUIRED_FILES)
    required.extend(prefix + '**' for prefix in updater.REQUIRED_PREFIXES)
    return {
        'format': 1,
        'version': version,
        'archive_sha256': '0' * 64,
        'files': files,
        'required': required,
    }


def write_archive(destination, bundle):
    """Write a deterministic ZIP (fixed timestamps, sorted entries)."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, 'w', ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(Path(bundle).rglob('*')):
            if not path.is_file():
                continue
            relative = path.relative_to(bundle).as_posix()
            if relative in ('manifest.json', 'manifest.json.sig'):
                continue
            info = ZipInfo('Vector PDF/' + relative,
                           date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            with path.open('rb') as source, archive.open(info, 'w') as sink:
                shutil.copyfileobj(source, sink, 256 * 1024)
    with ZipFile(destination) as archive:
        if bad := archive.testzip():
            raise RuntimeError(f'ZIP verification failed: {bad}')
    return destination


def _copy_source(bundle):
    shutil.copytree(ROOT / 'vector_pdf', bundle / 'vector_pdf', ignore=IGNORE)
    for name in DOCUMENTS:
        source = ROOT / name
        if source.is_file():
            shutil.copy2(source, bundle / name)
    for source in sorted(ROOT.glob('requirements*.txt')):
        shutil.copy2(source, bundle / source.name)
    for name in ('licenses', 'tests', 'tools'):
        source = ROOT / name
        if not source.is_dir():
            raise RuntimeError(f'Missing release source directory: {name}')
        shutil.copytree(source, bundle / name, ignore=IGNORE)
    workflows = ROOT / '.github'
    if workflows.is_dir():
        shutil.copytree(workflows, bundle / '.github', ignore=IGNORE)


def package(python_zip, wheels, launcher, destination, manifest=None,
            sbom=None, version=None):
    version = version or VERSION
    destination = Path(destination)
    with tempfile.TemporaryDirectory(prefix='vector-release-') as temp:
        bundle = Path(temp) / 'Vector PDF'
        runtime = bundle / 'runtime'
        site = runtime / 'Lib' / 'site-packages'
        site.mkdir(parents=True)
        with ZipFile(python_zip) as archive:
            archive.extractall(runtime)
        (runtime / 'python312._pth').write_text(
            'python312.zip\n.\nLib/site-packages\n..\nimport site\n',
            encoding='ascii')
        for wheel in sorted(Path(wheels).glob('*.whl')):
            with ZipFile(wheel) as archive:
                for member in archive.namelist():
                    if include(member):
                        archive.extract(member, site)
        shutil.copy2(launcher, bundle / 'VectorPDF.exe')
        (runtime / 'qt.conf').write_text(
            '[Paths]\nPlugins = Lib/site-packages/PySide6/plugins\n',
            encoding='ascii')
        _copy_source(bundle)
        if sbom is not None:
            sbom_path = Path(sbom)
            if not sbom_path.is_file():
                raise RuntimeError(f'Missing SBOM: {sbom_path}')
            shutil.copy2(sbom_path, bundle / 'sbom.json')
        for name in updater.REQUIRED_FILES:
            if not (bundle / name).is_file():
                raise RuntimeError(
                    f'Missing Windows runtime component: {name}')
        for prefix in updater.REQUIRED_PREFIXES:
            if not any(path.is_file() for path in
                       bundle.glob(prefix + '**/*')):
                raise RuntimeError(f'Missing packaged component: {prefix}')
        manifest_data = build_manifest(bundle, version)
        result = write_archive(destination, bundle)
        manifest_data['archive_sha256'] = file_sha256(result)
        updater.validate_manifest(manifest_data, version)
        manifest_path = Path(manifest) if manifest else \
            destination.parent / 'manifest.json'
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_bytes(updater.canonical_json(manifest_data))
        return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('python_zip', type=Path)
    parser.add_argument('wheels', type=Path)
    parser.add_argument('launcher', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--manifest', type=Path,
                        help='manifest sidecar (default: <output dir>/manifest.json)')
    parser.add_argument('--sbom', type=Path,
                        help='CycloneDX SBOM to include in the bundle')
    parser.add_argument('--version', default=VERSION)
    args = parser.parse_args()
    print(package(args.python_zip, args.wheels, args.launcher, args.output,
                  args.manifest, args.sbom, args.version))
