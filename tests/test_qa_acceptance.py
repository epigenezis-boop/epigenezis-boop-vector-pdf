"""Independent QA acceptance checks for updater, UI state and recovery.

The UI runs on Qt's offscreen platform.  Dialogues are replaced with bounded
stubs so no test can block on a native window.  Baseline expectations are
recorded in ``QA_BASELINE.md``.
"""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import inspect
import os
from pathlib import Path
from time import monotonic
from zipfile import ZipFile

import pikepdf as q
import pypdfium2 as pdfium
import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

pytest.importorskip('PySide6.QtWidgets')

from PySide6.QtCore import QPointF, QSettings
from PySide6.QtGui import QColor, QFontDatabase, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QMessageBox,
)

from vector_pdf import updater
from vector_pdf.canvas import Canvas
from vector_pdf.document import Document, LocalEditor
from vector_pdf.fonts import fallback
from vector_pdf.geometry import Sheet
from vector_pdf.object_index import index_page
from vector_pdf.render import Tile, TileCache
from vector_pdf.ui import MainWindow, configure_fonts
from vector_pdf.writer import Edit, file_stamp

from vector_pdf import ui as ui_module


# ---------------------------------------------------------------------------
# Small synthetic PDF used by the acceptance tests.


def _pdf(path: Path, pages: int = 1, first: str = 'Original') -> Path:
    with q.Pdf.new() as pdf:
        font, metrics, _ = fallback('Sans', 'Original Replacement Page Two', pdf)
        for number in range(pages):
            page = pdf.add_blank_page(page_size=(400, 300))
            page.Resources = q.Dictionary(Font=q.Dictionary(F0=font))
            words = first if number == 0 else 'Page Two'
            code = metrics.encode(words)
            page.Contents = pdf.make_stream(
                f'BT /F0 12 Tf 1 0 0 1 50 {240 if number == 0 else 200} Tm '
                f'<{code.hex()}> Tj ET\n'.encode())
        pdf.save(path)
    return path


def _signed_pdf(path: Path) -> Path:
    with q.Pdf.open(_pdf(path)) as pdf:
        pdf.Root.MySig = pdf.make_indirect(q.Dictionary(
            Type=q.Name.Sig, ByteRange=q.Array([0, 10, 20, 30]),
            Contents=q.String(b'not-a-real-signature')))
        pdf.save(tmp := path.with_name(path.stem + '-signed.pdf'))
    path.unlink()
    tmp.replace(path)
    return path


# ---------------------------------------------------------------------------
# Qt harness


@pytest.fixture(scope='session')
def qapp(tmp_path_factory):
    directory = tmp_path_factory.mktemp('qt-settings')
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat,
                      QSettings.Scope.UserScope, str(directory))
    app = QApplication.instance() or QApplication([])
    app.setStyle('Fusion')
    configure_fonts(app)
    return app


def _wait(app, condition, description: str, seconds: float = 12.0):
    limit = monotonic() + seconds
    while monotonic() < limit:
        app.processEvents()
        if condition():
            return
        QTest.qWait(25)
    assert condition(), description


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'localappdata'))
    monkeypatch.setenv('TMP', str(tmp_path / 'tmp'))
    monkeypatch.setenv('TEMP', str(tmp_path / 'tmp'))
    (tmp_path / 'localappdata').mkdir()
    (tmp_path / 'tmp').mkdir()
    window = MainWindow()
    window.settings = QSettings(str(tmp_path / 'settings.ini'),
                                QSettings.Format.IniFormat)
    errors = []
    window.error = errors.append
    window.check_updates = lambda *args, **kwargs: None
    window.auto_update.setChecked(False)
    # Never let an unexpected native modal dialogue hang an offscreen run.
    for name in ('warning', 'information', 'critical'):
        monkeypatch.setattr(QMessageBox, name,
                            staticmethod(lambda *args, **kwargs:
                                         QMessageBox.StandardButton.Ok))
    window.show()
    qapp.processEvents()
    try:
        yield window, errors
    finally:
        for index in reversed(range(window.tabs.count())):
            tab = window.tabs.widget(index)
            if tab is not None:
                tab.close_workspace()
            window.tabs.removeTab(index)
        window.close()
        window.editor.executor.shutdown(wait=True, cancel_futures=True)
        window.render.shutdown()
        qapp.processEvents()


def _open(window, app, path: Path):
    window.open_file(path)
    tab = window.current_tab()
    _wait(app, lambda: tab.permission_checked and tab.sheets and 0 in tab.sheet_loaded,
          'document/sheet did not load', 15)
    return tab


def _widget_text(widget) -> str:
    if hasattr(widget, 'toPlainText'):
        return widget.toPlainText()
    return widget.text()


def _set_widget_text(widget, text: str):
    if hasattr(widget, 'setPlainText'):
        widget.setPlainText(text)
    else:
        widget.setText(text)


class DialogRecorder:
    def __init__(self, result=QMessageBox.StandardButton.Cancel):
        self.calls = []
        self.result = result

    def question(self, parent, title, text, standard=None, default=None):
        self.calls.append(standard)
        return self.result

    def warning(self, parent, title, text,
                buttons=QMessageBox.StandardButton.Ok, default=None):
        self.calls.append(buttons)
        return self.result

    def exec(self, box):
        self.calls.append(box.standardButtons())
        return int(self.result.value if hasattr(self.result, 'value') else self.result)


def _record_dialogs(monkeypatch, result=QMessageBox.StandardButton.Cancel):
    recorder = DialogRecorder(result)
    monkeypatch.setattr(QMessageBox, 'question', staticmethod(recorder.question))
    monkeypatch.setattr(QMessageBox, 'warning', staticmethod(recorder.warning))

    def exec_(box):
        return recorder.exec(box)

    monkeypatch.setattr(QMessageBox, 'exec', exec_)
    return recorder


def _select_original(tab, text: str = 'Original'):
    run = next(run for run in index_page(tab.document.source, 0).runs
               if run.text == text)
    tab.load_edit(Edit.from_run(0, run))
    return run


# ---------------------------------------------------------------------------
# 10. Updater hardening.


def _five_file_install(directory: Path, version: str = '9.9.9') -> Path:
    for name in updater.REQUIRED:
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if name.endswith('version.py'):
            target.write_text(f"VERSION = '{version}'\n", encoding='utf-8')
        else:
            target.write_bytes(b'stub')
    return directory


def test_five_file_update_stub_is_rejected(tmp_path):
    install = _five_file_install(tmp_path / '9.9.9')
    with pytest.raises(ValueError):
        updater.check_installed(install, '9.9.9')


def test_complete_runtime_without_manifest_is_not_trusted(tmp_path):
    install = tmp_path / '9.9.9'
    names = [
        *updater.REQUIRED,
        'runtime/python312.zip', 'runtime/python312._pth', 'runtime/qt.conf',
        'runtime/Lib/site-packages/PySide6/__init__.py',
        'vector_pdf/ui.py', 'vector_pdf/writer.py', 'vector_pdf/fonts.py',
        'vector_pdf/assets/osifont.ttf', 'licenses/LICENSE-Qt.txt',
    ]
    for name in names:
        target = install / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if name.endswith('version.py'):
            target.write_text("VERSION = '9.9.9'\n", encoding='utf-8')
        else:
            target.write_bytes(b'stub')
    with pytest.raises(ValueError):
        updater.check_installed(install, '9.9.9')


def test_manifest_with_bad_signature_is_rejected(tmp_path):
    if not any(hasattr(updater, name) for name in
               ('verify_manifest', 'check_manifest', 'manifest_signature')):
        pytest.skip('0.3 signed-manifest API is absent on the 0.2.1 baseline')
    # The API shape is intentionally not guessed; presence is the QA gate.
    pytest.fail('signed-manifest API appeared; extend this test with real key material')


def test_partial_version_directory_is_repaired(tmp_path, monkeypatch):
    """A half-populated versions/<v> must not permanently block that version."""
    base = tmp_path / 'updates'
    partial = base / 'versions' / '0.3.0'
    partial.mkdir(parents=True)
    (partial / 'VectorPDF.exe').write_bytes(b'partial')
    payload = BytesIO()
    with ZipFile(payload, 'w') as archive:
        for name in updater.REQUIRED:
            archive.writestr('Vector PDF/' + name,
                             "VERSION = '0.3.0'\n" if name.endswith('version.py')
                             else b'repaired')
        archive.writestr('Vector PDF/repaired.marker', b'present')
    data = payload.getvalue()

    def fetch(url):
        return BytesIO(sha256(data).hexdigest().encode()
                       if url.endswith('.sha256') else data)

    monkeypatch.setattr(updater, 'fetch', fetch)
    monkeypatch.setattr(updater, 'check_installed', lambda *args, **kwargs: None)
    release = updater.Release('0.3.0',
                              f'https://github.com/{updater.REPOSITORY}/releases/download/v0.3.0/x.zip',
                              f'https://github.com/{updater.REPOSITORY}/releases/download/v0.3.0/x.zip.sha256')
    assert updater.arm(release, base) == '0.3.0'
    assert (base / 'versions' / '0.3.0' / 'repaired.marker').is_file(), \
        'partial version directory was trusted instead of repaired'


def test_dispatch_rejects_child_that_never_becomes_ready(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'localappdata'))
    base = updater.root()
    install = _five_file_install(base / 'versions' / '9.9.9')
    (base / 'active').write_text('9.9.9', encoding='ascii')

    class DeadProcess:
        pid = 424242
        returncode = 1

        def wait(self, timeout=None):
            return 1

        def poll(self):
            return 1

        def communicate(self, timeout=None):
            return b'', b''

        def terminate(self):
            pass

        def kill(self):
            pass

    monkeypatch.setattr(updater.subprocess, 'Popen', lambda *args, **kwargs: DeadProcess())
    for name in ('HEALTH_TIMEOUT', 'READY_TIMEOUT', 'DISPATCH_TIMEOUT'):
        if hasattr(updater, name):
            monkeypatch.setattr(updater, name, 1)

    assert updater.dispatch() is False, 'dispatch accepted a dead child process'
    active = base / 'active'
    assert not active.exists() or active.read_text(encoding='ascii').strip() != '9.9.9', \
        'failed dispatch left the broken version active'


def test_ui_main_exposes_ready_token_handshake():
    parameters = inspect.signature(ui_module.main).parameters
    if 'ready_token' not in parameters:
        pytest.skip('ui.main(ready_token=...) is absent on the 0.2.1 baseline')
    assert parameters['ready_token'].default is None


def test_bad_checksum_does_not_change_active_version(tmp_path, monkeypatch):
    base = tmp_path / 'updates'
    payload = BytesIO()
    with ZipFile(payload, 'w') as archive:
        for name in updater.REQUIRED:
            archive.writestr('Vector PDF/' + name,
                             "VERSION = '0.3.0'\n" if name.endswith('version.py')
                             else b'data')
    data = payload.getvalue()

    def good(url):
        return BytesIO(sha256(data).hexdigest().encode()
                       if url.endswith('.sha256') else data)

    monkeypatch.setattr(updater, 'fetch', good)
    release = updater.Release(
        '0.3.0',
        f'https://github.com/{updater.REPOSITORY}/releases/download/v0.3.0/x.zip',
        f'https://github.com/{updater.REPOSITORY}/releases/download/v0.3.0/x.zip.sha256')
    assert updater.arm(release, base) == '0.3.0'
    assert (base / 'active').read_text(encoding='ascii') == '0.3.0'

    monkeypatch.setattr(updater, 'fetch', lambda url: BytesIO(
        b'0' * 64 if url.endswith('.sha256') else data))
    with pytest.raises(ValueError, match='checksum'):
        updater.arm(updater.Release(
            '0.4.0',
            f'https://github.com/{updater.REPOSITORY}/releases/download/v0.4.0/x.zip',
            f'https://github.com/{updater.REPOSITORY}/releases/download/v0.4.0/x.zip.sha256'), base)
    assert (base / 'active').read_text(encoding='ascii') == '0.3.0'


def test_source_change_can_still_export_from_snapshot(tmp_path):
    """P0 #9: an immutable basis must keep edits exportable after source change."""
    source = _pdf(tmp_path / 'source.pdf', first='Original')
    document = Document(source)
    try:
        if not all(hasattr(document, name)
                   for name in ('ensure_basis', 'basis', 'source_stamp')):
            pytest.skip('Document snapshot API is absent on the 0.2.1 baseline')
        original_stamp = file_stamp(source)
        document.ensure_basis()
        basis = document.basis() if callable(document.basis) else document.basis
        assert Path(basis).is_file()
        assert document.source_stamp == original_stamp

        replacement = _pdf(tmp_path / 'replacement.pdf', first='Replacement')
        source.write_bytes(replacement.read_bytes())
        assert file_stamp(source) != original_stamp

        index = LocalEditor.index(document, 0)
        run = next(run for run in index.runs if run.text == 'Original')
        edit = replace(Edit.from_run(0, run), text='Snapshot')
        destination = tmp_path / 'snapshot-export.pdf'
        LocalEditor.save(document, destination, {(0, run.id): edit})
        assert destination.is_file()
        assert 'Snapshot' in pdfium_text(destination)
        assert 'Replacement' not in pdfium_text(destination)
    finally:
        document.workspace.cleanup()


def pdfium_text(path: Path) -> str:
    with pdfium.PdfDocument(path) as document:
        page = document[0]
        textpage = page.get_textpage()
        try:
            return textpage.get_text_bounded()
        finally:
            textpage.close()
            page.close()


# ---------------------------------------------------------------------------
# 11. UI state, save safety, preview and rendering.


def test_busy_page_switch_is_ignored(qapp, window, tmp_path):
    window, errors = window
    source = _pdf(tmp_path / 'two-pages.pdf', pages=2)
    tab = _open(window, qapp, source)
    assert len(tab.sheets) == 2
    tab.busy = True
    try:
        tab.page(1)
    finally:
        tab.busy = False
    assert tab.current == 0, 'page changed while an async save was busy'


def test_add_before_real_sheet_is_blocked(qapp, window, tmp_path):
    window, errors = window
    source = _pdf(tmp_path / 'add-source.pdf', pages=2)
    tab = _open(window, qapp, source)
    tab.page(1)  # placeholder sheet, render.sheet still queued
    window.change_mode('text')
    window.add()
    assert tab.selected is None or 1 in tab.sheet_loaded, \
        'text was added using placeholder sheet geometry'


def test_long_text_is_not_silently_truncated(qapp, window, tmp_path):
    window, errors = window
    source = _pdf(tmp_path / 'long-source.pdf')
    tab = _open(window, qapp, source)
    long_text = 'a' * 2000
    tab.load_edit(Edit(0, -91, long_text, 100, 100, 12, 0, family='Sans'))
    assert len(_widget_text(tab.text)) == 2000, \
        'long text was silently truncated by the editor field'


@pytest.mark.parametrize('size', [0.1, 4500.0])
def test_extreme_size_is_not_silently_clamped(qapp, window, tmp_path, size):
    window, errors = window
    source = _pdf(tmp_path / 'size-source.pdf')
    tab = _open(window, qapp, source)
    tab.load_edit(Edit(0, -92, 'size', 100, 100, size, 0, family='Sans'))
    assert tab.size.value() == pytest.approx(size), \
        f'size {size} pt was silently clamped to {tab.size.value()}'


def test_save_as_appends_pdf_instead_of_overwriting_other_pdf(qapp, window, monkeypatch, tmp_path):
    window, errors = window
    source = _pdf(tmp_path / 'save-source.pdf')
    tab = _open(window, qapp, source)
    collision = tmp_path / 'report.pdf'
    collision.write_bytes(b'do not overwrite me')
    requested = tmp_path / 'report.docx'
    monkeypatch.setattr(QFileDialog, 'getSaveFileName',
                        staticmethod(lambda *args, **kwargs: (str(requested), 'PDF')))
    prompts = []
    monkeypatch.setattr(QMessageBox, 'question',
                        staticmethod(lambda *args, **kwargs: prompts.append(args) or
                                     QMessageBox.StandardButton.Cancel))
    tab.save()
    _wait(qapp, lambda: not tab.busy, 'save did not finish')
    assert collision.read_bytes() == b'do not overwrite me', \
        'a non-.pdf name silently overwrote an existing PDF'
    assert (tmp_path / 'report.docx.pdf').is_file(), \
        '.pdf was not appended to a name with another extension'
    assert not prompts, 'unexpected confirmation for a non-existing appended target'


def test_save_as_confirms_existing_final_pdf(qapp, window, monkeypatch, tmp_path):
    window, errors = window
    source = _pdf(tmp_path / 'save-source.pdf')
    tab = _open(window, qapp, source)
    existing = tmp_path / 'report.docx.pdf'
    existing.write_bytes(b'keep this revision')
    requested = tmp_path / 'report.docx'
    monkeypatch.setattr(QFileDialog, 'getSaveFileName',
                        staticmethod(lambda *args, **kwargs: (str(requested), 'PDF')))
    dialogs = _record_dialogs(monkeypatch)
    tab.save()
    _wait(qapp, lambda: not tab.busy and bool(dialogs.calls),
          'overwrite confirmation missing', 5)
    assert dialogs.calls, 'existing final PDF was not confirmed'
    assert existing.read_bytes() == b'keep this revision'


def test_close_modified_tab_offers_save_discard_cancel(qapp, window, monkeypatch, tmp_path):
    window, errors = window
    source = _pdf(tmp_path / 'close-source.pdf')
    tab = _open(window, qapp, source)
    tab.document.commit({(0, -1): Edit(0, -1, 'changed', 50, 100, 12, 0,
                                      family='Sans')})
    dialogs = _record_dialogs(monkeypatch)
    window.close_tab(window.tabs.indexOf(tab))
    assert dialogs.calls, 'closing a modified tab did not ask about unsaved edits'
    flags = dialogs.calls[-1]
    for expected in (QMessageBox.StandardButton.Save,
                     QMessageBox.StandardButton.Discard,
                     QMessageBox.StandardButton.Cancel):
        assert flags & expected, f'close dialogue lacks {expected}'
    assert window.tabs.count() == 1, 'Cancel still closed the modified tab'


def test_uncommitted_draft_is_seen_by_confirm(qapp, window, monkeypatch, tmp_path):
    window, errors = window
    source = _pdf(tmp_path / 'draft-source.pdf')
    tab = _open(window, qapp, source)
    _select_original(tab)
    _set_widget_text(tab.text, 'UNCOMMITTED DRAFT')
    assert not tab.document.modified
    buttons = []
    monkeypatch.setattr(QMessageBox, 'question',
                        staticmethod(lambda *args, **kwargs: buttons.append(args) or
                                     QMessageBox.StandardButton.Cancel))
    assert window.confirm(tab) is False, 'confirm ignored an uncommitted draft'
    assert buttons


def test_uncommitted_draft_survives_page_switch(qapp, window, monkeypatch, tmp_path):
    window, errors = window
    source = _pdf(tmp_path / 'draft-source.pdf', pages=2)
    tab = _open(window, qapp, source)
    _select_original(tab)
    _set_widget_text(tab.text, 'UNCOMMITTED DRAFT')
    monkeypatch.setattr(QMessageBox, 'question',
                        staticmethod(lambda *args, **kwargs:
                                     QMessageBox.StandardButton.Cancel))
    tab.page(1)
    assert tab.current == 0 or (tab.selected is not None and
                                _widget_text(tab.text) == 'UNCOMMITTED DRAFT'), \
        'page switch discarded the uncommitted draft'


def test_uncommitted_draft_survives_view_mode(qapp, window, monkeypatch, tmp_path):
    window, errors = window
    source = _pdf(tmp_path / 'draft-source.pdf')
    tab = _open(window, qapp, source)
    _select_original(tab)
    _set_widget_text(tab.text, 'UNCOMMITTED DRAFT')
    monkeypatch.setattr(QMessageBox, 'question',
                        staticmethod(lambda *args, **kwargs:
                                     QMessageBox.StandardButton.Cancel))
    window.change_mode('view')
    assert (tab.selected is not None and
            _widget_text(tab.text) == 'UNCOMMITTED DRAFT') or tab.view.mode != 'view', \
        'switching to View discarded the uncommitted draft'


def test_uncommitted_draft_blocks_tab_close(qapp, window, monkeypatch, tmp_path):
    window, errors = window
    source = _pdf(tmp_path / 'draft-source.pdf')
    tab = _open(window, qapp, source)
    _select_original(tab)
    _set_widget_text(tab.text, 'UNCOMMITTED DRAFT')
    prompts = []
    monkeypatch.setattr(QMessageBox, 'question',
                        staticmethod(lambda *args, **kwargs: prompts.append(args) or
                                     QMessageBox.StandardButton.Cancel))
    window.close_tab(window.tabs.indexOf(tab))
    assert prompts, 'tab closed with an uncommitted draft without a prompt'
    assert window.tabs.count() == 1


def test_tiles_are_painted_low_resolution_first(qapp):
    canvas = Canvas()
    canvas.resize(96, 96)
    canvas.sheet = Sheet(96, 96, (0, 0, 96, 96), 0)
    canvas.page = 0
    canvas.zoom = 1.0
    canvas.rotation = 0
    canvas.pan = QPointF(0, 0)
    canvas.cache = TileCache()
    low = QImage(64, 64, QImage.Format.Format_RGB32)
    low.fill(QColor('blue'))
    high = QImage(256, 256, QImage.Format.Format_RGB32)
    high.fill(QColor('red'))
    canvas.cache.put(Tile(0, .5, 0, 0), low)
    canvas.cache.put(Tile(0, 4.0, 0, 0), high)
    image = QImage(96, 96, QImage.Format.Format_RGB32)
    image.fill(QColor('white'))
    canvas.render(image)
    color = QColor(image.pixelColor(8, 8))
    canvas.deleteLater()
    assert color.red() > 200 and color.blue() < 60, (
        f'low-resolution tile was painted over the high-resolution tile: {color.name()}')


def test_dejavu_preview_fonts_are_registered(qapp):
    families = set(QFontDatabase.families())
    for family in ('DejaVu Sans', 'DejaVu Serif', 'DejaVu Sans Mono'):
        assert family in families, f'{family} is not registered for preview'


def test_read_only_pdf_can_save_a_copy(qapp, window, monkeypatch, tmp_path):
    window, errors = window
    source = _signed_pdf(tmp_path / 'signed.pdf')
    tab = _open(window, qapp, source)
    _wait(qapp, lambda: tab.permission_checked and tab.read_only,
          'signature permission was not detected')
    assert window.actions['save'].isEnabled(), \
        'Save copy is disabled for a read-only/signed PDF'
    destination = tmp_path / 'signed-copy.pdf'
    monkeypatch.setattr(QFileDialog, 'getSaveFileName',
                        staticmethod(lambda *args, **kwargs: (str(destination), 'PDF')))
    window.actions['save'].trigger()
    _wait(qapp, lambda: destination.exists() and not tab.busy,
          'read-only copy was not written', 15)
    assert not errors, errors
    assert destination.read_bytes() == source.read_bytes(), \
        'read-only copy differs from the original bytes'


def test_saving_over_pdf_open_in_another_tab_is_rejected(qapp, window, monkeypatch, tmp_path):
    window, errors = window
    first = _pdf(tmp_path / 'first.pdf')
    second = _pdf(tmp_path / 'second.pdf')
    tab_a = _open(window, qapp, first)
    window.open_file(second)
    _wait(qapp, lambda: window.tabs.count() == 2 and
          window.tabs.widget(1).sheets, 'second tab did not open')
    window.tabs.setCurrentIndex(window.tabs.indexOf(tab_a))
    before = second.read_bytes()
    monkeypatch.setattr(QFileDialog, 'getSaveFileName',
                        staticmethod(lambda *args, **kwargs: (str(second), 'PDF')))
    monkeypatch.setattr(QMessageBox, 'question',
                        staticmethod(lambda *args, **kwargs:
                                     QMessageBox.StandardButton.Yes))
    tab_a.save()
    _wait(qapp, lambda: not tab_a.busy and (errors or second.read_bytes() != before),
          'save-over-open-tab did not finish', 15)
    assert second.read_bytes() == before, 'destination open in another tab was overwritten'
    assert errors, 'no user-facing error for save over an open tab'
    assert isinstance(errors[-1], ValueError), (
        f'raw platform error instead of a friendly rejection: {errors[-1]!r}')


# ---------------------------------------------------------------------------
# 12. Journals, recovery and temp cleanup.
#
# The 0.2.1 baseline has no journal/recovery API.  These probes skip with a
# clear reason and are recorded as NOT RUN in QA_BASELINE.md.


def _journal_candidates(document):
    workspace = Path(document.workspace.name)
    roots = [workspace]
    local = os.environ.get('LOCALAPPDATA')
    if local:
        roots.append(Path(local) / 'VectorPDF')
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob('*'):
            if path.is_file() and path.suffix.lower() in ('.json', '.journal'):
                return path
    return None


def test_journal_exists_after_successful_edit(qapp, window, monkeypatch, tmp_path):
    window, errors = window
    source = _pdf(tmp_path / 'journal-source.pdf')
    tab = _open(window, qapp, source)
    run = _select_original(tab)
    _set_widget_text(tab.text, 'Journal edit')
    monkeypatch.setattr(QMessageBox, 'question',
                        staticmethod(lambda *args, **kwargs:
                                     QMessageBox.StandardButton.Cancel))
    tab.apply()
    _wait(qapp, lambda: tab.document.cursor == 1 and not tab.busy, 'edit failed')
    journal = _journal_candidates(tab.document)
    if journal is None:
        pytest.skip('journal/autosave file absent on the 0.2.1 baseline')
    assert journal.is_file()


def test_recovery_is_offered_after_simulated_crash(qapp, window, monkeypatch, tmp_path):
    window, errors = window
    if not any(hasattr(MainWindow, name) for name in
               ('offer_recovery', 'recover', 'recoveries', 'recovery')):
        pytest.skip('recovery API is absent on the 0.2.1 baseline')
    pytest.fail('recovery API appeared; extend this test with a crash fixture')


def test_stale_temp_workspace_is_cleaned_on_startup(qapp, window, monkeypatch, tmp_path):
    window, errors = window
    if not any(hasattr(MainWindow, name) for name in
               ('cleanup_temp', 'cleanup_temporary', 'cleanup_stale')):
        pytest.skip('startup temp-cleanup API is absent on the 0.2.1 baseline')
    pytest.fail('cleanup API appeared; extend this test with a stale workspace fixture')


def test_clean_close_removes_workspace(qapp, window, monkeypatch, tmp_path):
    window, errors = window
    source = _pdf(tmp_path / 'clean-close.pdf')
    tab = _open(window, qapp, source)
    workspace = Path(tab.document.workspace.name)
    assert workspace.exists()
    monkeypatch.setattr(QMessageBox, 'question',
                        staticmethod(lambda *args, **kwargs:
                                     QMessageBox.StandardButton.Yes))
    window.close_tab(window.tabs.indexOf(tab))
    _wait(qapp, lambda: not workspace.exists(), 'workspace was not removed', 10)
    assert not workspace.exists()
