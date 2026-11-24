from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import uuid4

import pikepdf as q

from .object_index import index_page
from .writer import Edit, file_stamp, validate, write_copy


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
        self.workspace = TemporaryDirectory(prefix='vector-pdf-')
        self.indexes = {}
        self.states = [{}]
        self.cursor = 0
        self.next_id = -1
        self.serial = 0
        self.exported = {}

    @property
    def edits(self):
        return self.states[self.cursor]

    @property
    def modified(self):
        return self.edits != self.exported

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


class LocalEditor:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='qpdf')

    @staticmethod
    def check(document):
        with q.Pdf.open(document.source, attempt_recovery=False) as pdf:
            validate(pdf)

    @staticmethod
    def index(document, page):
        if file_stamp(document.source) != document.stamp:
            raise ValueError('The source file changed on disk.')
        return index_page(document.source, page)

    @staticmethod
    def save(document, target, state):
        start = perf_counter()
        write_copy(document.source, target, state.values(), document.stamp)
        return (perf_counter()-start)*1000
