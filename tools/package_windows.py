"""Construct an offline Windows package from Python's embeddable x64 ZIP and wheels."""
from pathlib import Path
import argparse
import shutil
import tempfile
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


QT_COMPONENTS = {
    'PySide6/QtCore.pyd', 'PySide6/QtGui.pyd', 'PySide6/QtWidgets.pyd',
    'PySide6/Qt6Core.dll', 'PySide6/Qt6Gui.dll', 'PySide6/Qt6Widgets.dll',
    'PySide6/pyside6.abi3.dll', 'PySide6/plugins/platforms/qwindows.dll',
}


def include(member):
    if '.data/' in member or member.startswith(('PySide6/include/', 'PySide6/typesystems/')):
        return False
    if member.startswith('PySide6/') and member.endswith('.exe'):
        return False
    if member.startswith('PySide6/') and member.endswith(('.pyd', '.dll')):
        return member in QT_COMPONENTS or Path(member).name.startswith(('msvcp140', 'vcruntime140', 'concrt140'))
    return True


def package(python_zip, wheels, launcher, destination):
    with tempfile.TemporaryDirectory(prefix='vector-release-') as temp:
        bundle = Path(temp) / 'Vector PDF'
        runtime = bundle / 'runtime'
        site = runtime / 'Lib' / 'site-packages'
        site.mkdir(parents=True)
        with ZipFile(python_zip) as archive:
            archive.extractall(runtime)
        (runtime / 'python312._pth').write_text(
            'python312.zip\n.\nLib/site-packages\n..\nimport site\n', encoding='ascii')
        for wheel in sorted(wheels.glob('*.whl')):
            with ZipFile(wheel) as archive:
                for member in archive.namelist():
                    if include(member):
                        archive.extract(member, site)
        shutil.copy2(launcher, bundle / 'VectorPDF.exe')
        (runtime / 'qt.conf').write_text('[Paths]\nPlugins = Lib/site-packages/PySide6/plugins\n',
                                        encoding='ascii')
        shutil.copytree(ROOT / 'vector_pdf', bundle / 'vector_pdf',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for name in ('README.md', 'ARCHITECTURE.md', 'BENCHMARKS.md', 'TEST_RESULTS.md',
                     'KNOWN_LIMITATIONS.md', 'THIRD_PARTY.md', 'LICENSE'):
            shutil.copy2(ROOT / name, bundle / name)
        shutil.copytree(ROOT / 'licenses', bundle / 'licenses')
        required = [
            'runtime/python312.dll', 'runtime/python.exe', 'VectorPDF.exe',
            'runtime/Lib/site-packages/PySide6/plugins/platforms/qwindows.dll',
            'runtime/Lib/site-packages/PySide6/Qt6Core.dll',
        ]
        for name in required:
            if not (bundle / name).is_file():
                raise RuntimeError(f'Missing Windows runtime component: {name}')
        result = Path(shutil.make_archive(str(destination.with_suffix('')), 'zip', bundle.parent, bundle.name))
        with ZipFile(result) as archive:
            if bad := archive.testzip():
                raise RuntimeError(f'ZIP verification failed: {bad}')
        return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('python_zip', type=Path)
    parser.add_argument('wheels', type=Path)
    parser.add_argument('launcher', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    print(package(args.python_zip, args.wheels, args.launcher, args.output))
