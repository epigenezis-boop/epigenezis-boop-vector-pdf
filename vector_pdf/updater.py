"""Windows updates from the project's GitHub Releases, installed side by side."""
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import ZipFile

from .version import REPOSITORY, VERSION

MAX_DOWNLOAD = 200 * 1024**2
MAX_EXPANDED = 800 * 1024**2
HOSTS = {'github.com', 'api.github.com', 'release-assets.githubusercontent.com',
         'objects.githubusercontent.com'}
REQUIRED = ('VectorPDF.exe', 'runtime/pythonw.exe', 'runtime/python312.dll',
            'vector_pdf/__main__.py', 'vector_pdf/version.py')


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:0|[1-9][0-9]{0,5})(?:\.(?:0|[1-9][0-9]{0,5})){2}', value):
        raise ValueError('Invalid release version.')
    return tuple(map(int, value.split('.')))


def root():
    return Path(os.environ['LOCALAPPDATA']) / 'VectorPDF' / 'updates'


def trusted(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc not in HOSTS:
        raise ValueError('Unexpected download origin.')
    return url


class GitHubRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, trusted(newurl))


def fetch(url):
    return build_opener(GitHubRedirects()).open(
        Request(trusted(url), headers={'User-Agent': f'VECTOR-PDF/{VERSION}'}), timeout=25)


@dataclass(frozen=True)
class Release:
    version: str
    archive: str
    checksum: str


def release_assets(data):
    if data.get('draft') or data.get('prerelease'):
        return None
    tag = data['tag_name']
    if not isinstance(tag, str) or not tag.startswith('v'):
        raise ValueError('Invalid GitHub release tag.')
    version = tag[1:]
    if version_tuple(version) <= version_tuple(VERSION):
        return None
    name = f'VECTOR-PDF-{version}-win-x64.zip'
    base = f'https://github.com/{REPOSITORY}/releases/download/{tag}/'
    urls = {asset['name']: asset['browser_download_url'] for asset in data['assets']}
    if urls.get(name) != base+name or urls.get(name+'.sha256') != base+name+'.sha256':
        raise ValueError('The release does not offer a verified Windows archive.')
    return Release(version, urls[name], urls[name+'.sha256'])


def check():
    with fetch(f'https://api.github.com/repos/{REPOSITORY}/releases/latest') as response:
        return release_assets(json.loads(response.read(1024**2)))


def verified_file(release, path):
    with fetch(release.checksum) as response:
        digest = response.read(2048).decode('ascii').split()[0].lower()
    if not re.fullmatch('[0-9a-f]{64}', digest):
        raise ValueError('Invalid SHA-256 checksum.')
    computed = sha256()
    received = 0
    with fetch(release.archive) as response, path.open('wb') as target:
        while data := response.read(256*1024):
            received += len(data)
            if received > MAX_DOWNLOAD:
                raise ValueError('The update exceeds the download limit.')
            target.write(data)
            computed.update(data)
    if computed.hexdigest() != digest:
        raise ValueError('The update checksum does not match.')


def unpack(archive_path, destination, version):
    with ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if len(entries) > 20_000 or sum(entry.file_size for entry in entries) > MAX_EXPANDED:
            raise ValueError('The update exceeds the unpacking limit.')
        seen = set()
        for entry in entries:
            parts = entry.filename.removesuffix('/').split('/')
            original = getattr(entry, 'orig_filename', entry.filename)
            if (not parts or parts[0] != 'Vector PDF' or '\\' in original or '\x00' in original or
                    any(not part or part in {'.', '..'} or ':' in part or
                        part.endswith((' ', '.')) or part.split('.')[0].upper() in
                        {'CON', 'PRN', 'AUX', 'NUL', *('COM'+str(i) for i in range(1,10)),
                         *('LPT'+str(i) for i in range(1,10))} for part in parts) or
                    stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError('An update entry has an unsafe path.')
            normalized = '/'.join(parts).casefold()
            if normalized in seen:
                raise ValueError('Duplicate update entry.')
            seen.add(normalized)
            target = destination.joinpath(*parts[1:])
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as src, target.open('xb') as dst:
                    shutil.copyfileobj(src, dst, 256*1024)
    check_installed(destination, version)


def check_installed(directory, version):
    if not all((directory / name).is_file() for name in REQUIRED):
        raise ValueError('Incomplete portable update.')
    declared = (directory / 'vector_pdf' / 'version.py').read_text(encoding='utf-8')
    if not re.search(r"^VERSION = ['\"]"+re.escape(version)+r"['\"]$", declared, re.M):
        raise ValueError('The package version does not match its GitHub tag.')


def arm(release, base=None):
    base = root() if base is None else base
    version_tuple(release.version)
    versions = base / 'versions'
    versions.mkdir(parents=True, exist_ok=True)
    dest = versions / release.version
    if not dest.exists():
        with TemporaryDirectory(dir=base, prefix='incoming-') as temp:
            directory = Path(temp)
            archive = directory / 'update.zip'
            verified_file(release, archive)
            stage = directory / 'app'
            stage.mkdir()
            unpack(archive, stage, release.version)
            try:
                stage.rename(dest)
            except OSError:
                if not dest.exists():
                    raise
                check_installed(dest, release.version)
    check_installed(dest, release.version)
    with NamedTemporaryFile('w', encoding='ascii', dir=base, delete=False) as file:
        pointer = Path(file.name)
        file.write(release.version)
    try:
        pointer.replace(base / 'active')
    finally:
        pointer.unlink(missing_ok=True)
    return release.version


def dispatch():
    """Hand a later launch off to the installed version before loading Qt."""
    if sys.platform != 'win32' or 'LOCALAPPDATA' not in os.environ:
        return False
    base = root()
    try:
        desired = (base / 'active').read_text(encoding='ascii').strip()
        if version_tuple(desired) <= version_tuple(VERSION):
            return False
        directory = base / 'versions' / desired
        check_installed(directory, desired)
        runtime = directory / 'runtime/pythonw.exe'
        subprocess.Popen([str(runtime), '-m', 'vector_pdf', *sys.argv[1:]])
        return True
    except (OSError, ValueError):
        return False
