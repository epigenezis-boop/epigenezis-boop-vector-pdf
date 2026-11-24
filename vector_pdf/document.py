from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from inspect import signature
import json
import logging
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory, gettempdir, mkstemp
from threading import Lock
from time import perf_counter, time
from uuid import uuid4

import pikepdf as q

from .object_index import index_page
from .version import VERSION
from .writer import Edit, file_stamp, validate, write_copy

LOG = logging.getLogger(__name__)

WORKSPACE_PREFIX = 'vector-pdf-'
JOURNAL = 'journal.json'
LIVE = '.live'
STALE_AGE = 7*24*60*60
INVALID_AGE = 60*60


@dataclass
class Metrics:
    opened: float = 0
    index: float = 0
    objects: int = 0
    area: int = 0
    render: float = 0
    edit: float = 0
    saved: float = 0
    invalidated: int = 0
    ui: float = 0


@dataclass
class Document:
    source: Path
    key: str = field(default_factory=lambda: uuid4().hex)
    metrics: Metrics = field(default_factory=Metrics)

    def __post_init__(self):
        self.original_key = self.key
        self.stamp = None
        self.workspace = TemporaryDirectory(prefix=WORKSPACE_PREFIX)
        self.indexes = {}
        self.states = [{}]
        self.cursor = 0
        self.next_id = -1
        self.serial = 0
        self.exported = {}
        self.export_name = None
        self.basis_path = None
        self.source_stamp = None
        self.snapshot_stamp = None
        self.disposed = False
        self._basis_lock = Lock()
        self._write_live()

    @property
    def edits(self):
        return self.states[self.cursor]

    @property
    def modified(self):
        return self.edits != self.exported

    @property
    def basis(self):
        return self.basis_path or Path(self.workspace.name) / 'basis.pdf'

    def ensure_basis(self):
        """Copy the source into the private workspace once, before indexing or saving.

        All later reads use the snapshot.  That keeps a changed, renamed or removed
        source from making already-made edits impossible to export.
        """
        with self._basis_lock:
            if self.disposed:
                raise ValueError('The document is closed.')
            if self.basis_path is not None and self.basis_path.exists():
                return self.basis_path
            source = Path(self.source)
            workspace = Path(self.workspace.name)
            workspace.mkdir(parents=True, exist_ok=True)
            target = workspace / 'basis.pdf'
            for _attempt in range(2):
                fd, temporary = mkstemp(prefix='.basis-', suffix='.pdf', dir=workspace)
                os.close(fd)
                try:
                    before = file_stamp(source)
                    shutil.copyfile(source, temporary)
                    try:
                        after = file_stamp(source)
                    except OSError:
                        # The source vanished while it was being copied.  The copy
                        # itself is complete, so keep it as the immutable basis.
                        after = before
                    os.replace(temporary, target)
                    self.source_stamp = after
                    self.basis_path = target
                    try:
                        self.snapshot_stamp = file_stamp(target)
                    except OSError:
                        self.snapshot_stamp = None
                    self.stamp = self.source_stamp
                    if before == after:
                        return target
                except OSError:
                    if target.is_file() and self.basis_path is not None:
                        return target
                    raise
                finally:
                    Path(temporary).unlink(missing_ok=True)
            # The source kept changing while it was copied.  The snapshot is still a
            # complete, consistent revision and remains usable for export.
            if target.is_file():
                return target
            raise OSError('The source PDF could not be read.')

    def source_changed(self):
        if self.source_stamp is None:
            return False
        try:
            return file_stamp(self.source) != self.source_stamp
        except OSError:
            return True

    def get(self, page, run):
        return self.edits.get((page, run.id)) or Edit.from_run(page, run)

    def commit(self, edits):
        self.states[self.cursor+1:] = [edits]
        self.cursor += 1

    def allocate(self):
        key = self.next_id
        self.next_id -= 1
        return key

    def new_version(self):
        self.serial += 1
        return self.original_key + ':' + str(self.serial), Path(self.workspace.name) / f'{self.serial}.pdf'

    def write_journal(self, version_path=None):
        """Atomically record the newest recoverable revision of this document."""
        try:
            workspace = Path(self.workspace.name)
            version = Path(version_path) if version_path is not None else workspace / f'{self.serial}.pdf'
            payload = {
                'source': str(self.source),
                'source_stamp': list(self.source_stamp) if self.source_stamp else None,
                'version': str(version),
                'version_path': str(version),
                'serial': self.serial,
                'timestamp': time(),
                'version_string': VERSION,
                'app_version': VERSION,
            }
            fd, temporary = mkstemp(prefix='.journal-', suffix='.json', dir=workspace)
            os.close(fd)
            try:
                with open(temporary, 'w', encoding='utf-8') as handle:
                    json.dump(payload, handle, ensure_ascii=False)
                os.replace(temporary, workspace / JOURNAL)
            finally:
                Path(temporary).unlink(missing_ok=True)
        except (OSError, TypeError, ValueError):
            LOG.debug('Could not write a recovery journal for %s', self.source, exc_info=True)

    def cleanup(self):
        self.disposed = True
        workspace = Path(self.workspace.name)
        try:
            (workspace / JOURNAL).unlink(missing_ok=True)
            (workspace / LIVE).unlink(missing_ok=True)
        except OSError:
            LOG.debug('Could not remove the recovery journal for %s', self.source, exc_info=True)
        try:
            self.workspace.cleanup()
        except Exception:
            LOG.debug('Could not remove the workspace for %s', self.source, exc_info=True)
        finally:
            self.basis_path = None

    def _write_live(self):
        try:
            marker = Path(self.workspace.name) / LIVE
            marker.write_text(json.dumps({'pid': os.getpid(), 'created': time()}), encoding='ascii')
        except OSError:
            LOG.debug('Could not mark the workspace for %s as live', self.source, exc_info=True)


class LocalEditor:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='qpdf')

    @staticmethod
    def check(document):
        with q.Pdf.open(document.ensure_basis(), attempt_recovery=False) as pdf:
            validate(pdf)

    @staticmethod
    def index(document, page):
        return index_page(document.ensure_basis(), page)

    @staticmethod
    def save(document, target, state):
        start = perf_counter()
        source = Path(document.source)
        target = Path(target)
        if same_path(target, source) or same_path(target, document.basis):
            raise ValueError('Choose a separate destination PDF. The original source is never overwritten.')
        basis = document.ensure_basis()
        edits = list(state.values()) if isinstance(state, dict) else list(state)
        if not edits:
            atomic_copy(basis, target)
        else:
            parameters = signature(write_copy).parameters
            options = {}
            if 'original_source' in parameters:
                options['original_source'] = source
            if 'verify' in parameters:
                # Per-edit workspace versions need to stay responsive: the light
                # path still checks structure, geometry, signatures and untouched
                # bytes, while PDFium itself validates the attached version.  The
                # final user-visible Save copy gets the deeper text/QPDF checks.
                try:
                    temporary = target.parent.resolve() == Path(document.workspace.name).resolve()
                except OSError:
                    temporary = False
                options['verify'] = 'light' if temporary else 'auto'
            if 'balanced_pages' in parameters:
                options['balanced_pages'] = {
                    number for number, index in document.indexes.items()
                    if getattr(index, 'balanced', False)
                }
            write_copy(basis, target, edits, **options)
        try:
            if target.parent.resolve() == Path(document.workspace.name).resolve():
                document.write_journal(target)
        except OSError:
            LOG.debug('Could not journal a version', exc_info=True)
        return (perf_counter()-start)*1000


def same_path(left, right):
    left, right = Path(left), Path(right)
    try:
        if left.resolve() == right.resolve():
            return True
    except OSError:
        pass
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return False


def atomic_copy(source, target):
    target = Path(target)
    fd, temporary = mkstemp(suffix='.pdf', prefix='.vector-', dir=target.parent)
    os.close(fd)
    try:
        shutil.copyfile(source, temporary)
        with open(temporary, 'rb+') as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


@dataclass(frozen=True)
class Recovery:
    source: Path
    version: Path
    workspace: Path
    serial: int
    timestamp: float
    version_string: str


def _temp_root(root=None):
    return Path(root) if root is not None else Path(gettempdir())


def _live_pid(workspace):
    try:
        value = json.loads((Path(workspace) / LIVE).read_text(encoding='ascii'))
        return int(value['pid'])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _pid_alive(pid):
    if pid is None:
        return False
    if pid == os.getpid():
        return True
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:
        if os.name == 'nt':
            return True
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def workspace_is_live(workspace):
    return _pid_alive(_live_pid(workspace))


def _journal_record(workspace):
    workspace = Path(workspace)
    try:
        payload = json.loads((workspace / JOURNAL).read_text(encoding='utf-8'))
        version = Path(str(payload.get('version') or payload['version_path']))
        if version.parent.resolve() != workspace.resolve() or not version.is_file():
            return None
        return Recovery(
            source=Path(str(payload.get('source', ''))),
            version=version,
            workspace=workspace,
            serial=int(payload.get('serial', 0)),
            timestamp=float(payload.get('timestamp', 0)),
            version_string=str(payload.get('version_string') or payload.get('app_version') or ''),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def scan_recovery(root=None):
    """Return recoverable versions, newest first.  Journals are never opened here."""
    root = _temp_root(root)
    if not root.is_dir():
        return []
    candidates = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    for workspace in entries:
        try:
            if (not workspace.name.startswith(WORKSPACE_PREFIX) or not workspace.is_dir()
                    or workspace.is_symlink() or workspace_is_live(workspace)):
                continue
            recovery = _journal_record(workspace)
            if recovery is not None:
                candidates.append(recovery)
        except OSError:
            continue
    candidates.sort(key=lambda item: item.timestamp, reverse=True)
    return candidates


def cleanup_workspaces(root=None, max_age=STALE_AGE, invalid_age=INVALID_AGE):
    """Remove stale private workspaces without touching a live one."""
    root = _temp_root(root)
    if not root.is_dir():
        return 0
    removed = 0
    now = time()
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    for workspace in entries:
        try:
            if (not workspace.name.startswith(WORKSPACE_PREFIX) or not workspace.is_dir()
                    or workspace.is_symlink()):
                continue
            age = now-workspace.stat().st_mtime
            if workspace_is_live(workspace):
                continue
            recovery = _journal_record(workspace)
            # A workspace from this version carries a .live marker.  Without the
            # marker we cannot tell an old-version live workspace from an abandoned
            # one, so only the full stale age is safe to remove.
            marked = (workspace / LIVE).exists()
            if age > max_age or (recovery is None and marked and age > invalid_age):
                shutil.rmtree(workspace, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def cleanup_temp_files(root=None, max_age=STALE_AGE):
    """Remove abandoned writer scratch files; never touches user files."""
    root = _temp_root(root)
    if not root.is_dir():
        return 0
    removed = 0
    now = time()
    try:
        entries = list(root.glob('.vector-*'))
    except OSError:
        return 0
    for path in entries:
        try:
            if (path.is_file() and not path.is_symlink() and path.suffix.lower() == '.pdf'
                    and now-path.stat().st_mtime > max_age):
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed
