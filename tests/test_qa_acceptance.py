"""Independent QA acceptance checks for updater, UI state and recovery.

The UI runs on Qt's offscreen platform.  Dialogues are replaced with bounded
stubs so no test can block on a native window.  Baseline expectations are
recorded in ``QA_BASELINE.md``.
"""
from __future__ import annotations

from dataclasses import replace
from io import BytesIO
import inspect
import json
import os
from pathlib import Path
import shutil
import time
from time import monotonic
import tempfile

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
from vector_pdf.document import (
    Document, JOURNAL, LIVE, LocalEditor, WORKSPACE_PREFIX,
    cleanup_workspaces, scan_recovery,
)
from vector_pdf.fonts import fallback
from vector_pdf.geometry import Sheet
from vector_pdf.object_index import index_page
from vector_pdf.render import Tile, TileCache
from vector_pdf.ui import MainWindow, configure_fonts
from vector_pdf.version import VERSION
from vector_pdf.writer import Edit, file_stamp

from vector_pdf import ui as ui_module
from test_updater_hardening import OTHER_SEED, PUBLIC, install, payload_for



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
    monkeypatch.setenv('VECTORPDF_NO_RECOVERY', '1')
    (tmp_path / 'localappdata').mkdir()
    (tmp_path / 'tmp').mkdir()
    window = MainWindow()
    window.settings = QSettings(str(tmp_path / 'settings.ini'),
                                QSettings.Format.IniFormat)
    errors = []
    window.error = lambda error, title=None: errors.append(error)
    window.check_updates = lambda *args, **kwargs: None
    window.auto_update.setChecked(False)
    # The integrated UI routes every confirmation through instance methods.
    # Replace both with bounded Python callables so an unexpected dialogue can
    # never block an offscreen run.
    window.message_calls = []
    window.ask_calls = []
    window.ask = lambda title, text, buttons, default: (
        window.ask_calls.append((title, text, buttons, default)) or default)
    window.message = lambda text, title=None, icon=None: (
        window.message_calls.append(str(text)))
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


class AskRecorder:
    def __init__(self, result=QMessageBox.StandardButton.Cancel):
        self.calls = []
        self.result = result

    def __call__(self, title, text, buttons, default):
        self.calls.append((title, text, buttons, default))
        return self.result


class MessageRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, text, title=None, icon=None):
        self.calls.append(str(text))


def _record_ask(window, monkeypatch, result=QMessageBox.StandardButton.Cancel):
    recorder = AskRecorder(result)
    monkeypatch.setattr(window, 'ask', recorder)
    return recorder


def _record_message(window, monkeypatch):
    recorder = MessageRecorder()
    monkeypatch.setattr(window, 'message', recorder)
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


def test_signed_manifest_bad_signature_is_rejected(tmp_path, monkeypatch):
    release, payloads = payload_for('9.9.9', signer_seed=OTHER_SEED)
    monkeypatch.setattr(updater, 'PUBLIC_KEY', PUBLIC)
    monkeypatch.setattr(updater, 'fetch',
                        lambda url: BytesIO(payloads[url]))
    monkeypatch.setattr(updater, 'health_check',
                        lambda directory, timeout=60: True)
    with pytest.raises(ValueError, match='signature is invalid'):
        updater.arm(release, tmp_path)
    assert not (tmp_path / 'active').exists()


def test_partial_version_directory_is_repaired(tmp_path, monkeypatch):
    """A half-populated versions/<v> must not permanently block that version."""
    base = tmp_path / 'updates'
    partial = base / 'versions' / '9.9.9'
    partial.mkdir(parents=True)
    (partial / 'VectorPDF.exe').write_bytes(b'partial')
    release, payloads = payload_for('9.9.9')
    assert install(monkeypatch, base, release, payloads) == '9.9.9'
    installed = base / 'versions' / '9.9.9'
    assert (installed / 'VectorPDF.exe').read_bytes() == b'MZ launcher'
    assert (installed / 'vector_pdf' / 'assets' / 'icon.png').is_file()
    assert any(item.name.startswith('broken-9.9.9-')
               for item in (base / 'versions').iterdir()), \
        'partial version directory was not quarantined'
    assert (base / 'active').read_text(encoding='ascii') == '9.9.9'


def test_dispatch_rejects_child_that_never_becomes_ready(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'localappdata'))
    base = updater.root()
    release, payloads = payload_for('9.9.9')
    assert install(monkeypatch, base, release, payloads) == '9.9.9'
    assert (base / 'active').read_text(encoding='ascii') == '9.9.9'

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

    monkeypatch.setattr(updater.subprocess, 'Popen',
                        lambda *args, **kwargs: DeadProcess())
    assert updater.dispatch(argv=[], base=base, ready_timeout=0.2) is False, \
        'dispatch accepted a dead child process'
    active = base / 'active'
    assert not active.exists() or \
        active.read_text(encoding='ascii').strip() != '9.9.9', \
        'failed dispatch left the broken version active'


def test_ui_main_exposes_ready_token_handshake():
    parameters = inspect.signature(ui_module.main).parameters
    if 'ready_token' not in parameters:
        pytest.skip('ui.main(ready_token=...) is absent on this tree')
    assert parameters['ready_token'].default is None


def test_bad_checksum_does_not_change_active_version(tmp_path, monkeypatch):
    base = tmp_path / 'updates'
    good_release, good_payloads = payload_for('9.9.9')
    assert install(monkeypatch, base, good_release, good_payloads) == '9.9.9'
    assert (base / 'active').read_text(encoding='ascii') == '9.9.9'

    bad_release, bad_payloads = payload_for(
        '9.9.10', checksum_override=b'0' * 64)
    monkeypatch.setattr(updater, 'fetch',
                        lambda url: BytesIO(bad_payloads[url]))
    with pytest.raises(ValueError, match='checksum'):
        updater.arm(bad_release, base)
    assert (base / 'active').read_text(encoding='ascii') == '9.9.9'


def test_source_change_can_still_export_from_snapshot(tmp_path):
    """P0 #9: an immutable basis must keep edits exportable after source change."""
    source = _pdf(tmp_path / 'source.pdf', first='Original')
    document = Document(source)
    try:
        if not all(hasattr(document, name)
                   for name in ('ensure_basis', 'basis', 'source_stamp')):
            pytest.skip('Document snapshot API is absent on this tree')
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
    tab.set_mode('text')
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
    asks = _record_ask(window, monkeypatch, QMessageBox.StandardButton.No)
    tab.save()
    _wait(qapp, lambda: (tmp_path / 'report.docx.pdf').is_file() and not tab.busy,
          'append save did not finish', 15)
    assert collision.read_bytes() == b'do not overwrite me', \
        'a non-.pdf name silently overwrote an existing PDF'
    assert (tmp_path / 'report.docx.pdf').is_file(), \
        '.pdf was not appended to a name with another extension'
    assert not asks.calls, 'unexpected confirmation for a non-existing appended target'
    assert not errors, errors


def test_save_as_confirms_existing_final_pdf(qapp, window, monkeypatch, tmp_path):
    window, errors = window
    source = _pdf(tmp_path / 'save-source.pdf')
    tab = _open(window, qapp, source)
    existing = tmp_path / 'report.docx.pdf'
    existing.write_bytes(b'keep this revision')
    requested = tmp_path / 'report.docx'
    monkeypatch.setattr(QFileDialog, 'getSaveFileName',
                        staticmethod(lambda *args, **kwargs: (str(requested), 'PDF')))
    asks = _record_ask(window, monkeypatch, QMessageBox.StandardButton.No)
    tab.save()
    _wait(qapp, lambda: bool(asks.calls), 'overwrite confirmation missing', 5)
    assert asks.calls, 'existing final PDF was not confirmed'
    assert existing.read_bytes() == b'keep this revision'


def test_close_modified_tab_offers_save_discard_cancel(qapp, window, monkeypatch, tmp_path):
    window, errors = window
    source = _pdf(tmp_path / 'close-source.pdf')
    tab = _open(window, qapp, source)
    tab.document.commit({(0, -1): Edit(0, -1, 'changed', 50, 100, 12, 0,
                                      family='Sans')})
    assert tab.document.modified
    asks = _record_ask(window, monkeypatch, QMessageBox.StandardButton.Cancel)
    assert window.confirm(tab) == 'cancel'
    assert asks.calls
    flags = asks.calls[-1][2]
    for expected in (QMessageBox.StandardButton.Save,
                     QMessageBox.StandardButton.Discard,
                     QMessageBox.StandardButton.Cancel):
        assert flags & expected, f'close dialogue lacks {expected}'
    window.close_tab(window.tabs.indexOf(tab))
    assert window.tabs.count() == 1, 'Cancel still closed the modified tab'


def test_uncommitted_draft_is_seen_by_confirm(qapp, window, monkeypatch, tmp_path):
    window, errors = window
    source = _pdf(tmp_path / 'draft-source.pdf')
    tab = _open(window, qapp, source)
    _select_original(tab)
    _set_widget_text(tab.text, 'UNCOMMITTED DRAFT')
    assert not tab.document.modified
    asks = _record_ask(window, monkeypatch, QMessageBox.StandardButton.Cancel)
    assert window.confirm(tab) == 'cancel', 'confirm ignored an uncommitted draft'
    assert asks.calls


def test_uncommitted_draft_survives_page_switch(qapp, window, monkeypatch, tmp_path):
    window, errors = window
    source = _pdf(tmp_path / 'draft-source.pdf', pages=2)
    tab = _open(window, qapp, source)
    _select_original(tab)
    _set_widget_text(tab.text, 'UNCOMMITTED DRAFT')
    asks = _record_ask(window, monkeypatch, QMessageBox.StandardButton.Cancel)
    tab.page(1)
    assert asks.calls, 'page switch did not ask about the uncommitted draft'
    assert tab.current == 0
    assert tab.selected is not None
    assert _widget_text(tab.text) == 'UNCOMMITTED DRAFT'


def test_uncommitted_draft_survives_view_mode(qapp, window, monkeypatch, tmp_path):
    window, errors = window
    source = _pdf(tmp_path / 'draft-source.pdf')
    tab = _open(window, qapp, source)
    tab.set_mode('text')
    _select_original(tab)
    _set_widget_text(tab.text, 'UNCOMMITTED DRAFT')
    asks = _record_ask(window, monkeypatch, QMessageBox.StandardButton.Cancel)
    tab.set_mode('view')
    assert asks.calls, 'View mode did not ask about the uncommitted draft'
    assert tab.view.mode != 'view'
    assert _widget_text(tab.text) == 'UNCOMMITTED DRAFT'


def test_uncommitted_draft_blocks_tab_close(qapp, window, monkeypatch, tmp_path):
    window, errors = window
    source = _pdf(tmp_path / 'draft-source.pdf')
    tab = _open(window, qapp, source)
    _select_original(tab)
    _set_widget_text(tab.text, 'UNCOMMITTED DRAFT')
    asks = _record_ask(window, monkeypatch, QMessageBox.StandardButton.Cancel)
    window.close_tab(window.tabs.indexOf(tab))
    assert asks.calls, 'tab closed with an uncommitted draft without a prompt'
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
    asks = _record_ask(window, monkeypatch, QMessageBox.StandardButton.Yes)
    messages = _record_message(window, monkeypatch)
    tab_a.save()
    _wait(qapp, lambda: bool(messages.calls) or bool(errors),
          'save-over-open-tab produced no friendly message', 5)
    assert second.read_bytes() == before, 'destination open in another tab was overwritten'
    assert messages.calls or errors, 'no user-facing rejection for save over an open tab'
    assert not any(isinstance(error, OSError) for error in errors), (
        f'raw platform error was exposed instead of a friendly rejection: {errors!r}')
    assert not asks.calls, 'a save-over-open-tab should be rejected before overwrite confirmation'


# ---------------------------------------------------------------------------
# 12. Journals, recovery and temp cleanup.


def test_journal_exists_after_successful_edit(qapp, window, monkeypatch, tmp_path):
    window, errors = window
    source = _pdf(tmp_path / 'journal-source.pdf')
    tab = _open(window, qapp, source)
    _select_original(tab)
    _set_widget_text(tab.text, 'Journal edit')
    tab.apply()
    _wait(qapp, lambda: tab.document.cursor == 1 and not tab.busy, 'edit failed')
    journal = Path(tab.document.workspace.name) / JOURNAL
    _wait(qapp, lambda: journal.is_file(), 'journal was not written', 5)
    assert journal.is_file(), 'no recovery journal after a successful edit'
    payload = json.loads(journal.read_text(encoding='utf-8'))
    assert Path(payload['source']) == source
    assert Path(payload['version']).is_file()
    assert payload['version_string'] == VERSION
    assert not errors, errors


def test_recovery_is_offered_after_simulated_crash(qapp, window, monkeypatch, tmp_path):
    window, errors = window
    root = tmp_path / 'tmp'
    monkeypatch.setattr(tempfile, 'tempdir', str(root))
    source = _pdf(tmp_path / 'recovery-source.pdf')
    document = Document(source)
    workspace = Path(document.workspace.name)
    try:
        assert workspace.parent == root
        basis = document.ensure_basis()
        _, version = document.new_version()
        shutil.copyfile(basis, version)
        document.write_journal(version)
        (workspace / LIVE).write_text(json.dumps({'pid': 999_999_999}),
                                      encoding='ascii')
        candidates = [candidate for candidate in scan_recovery(root)
                      if candidate.workspace == workspace]
        assert len(candidates) == 1, 'journal was not offered as recoverable'
        assert candidates[0].version == version
        assert candidates[0].source == source

        window._recovery_started = False
        asks = _record_ask(window, monkeypatch, QMessageBox.StandardButton.Discard)
        window.start_recovery()
        _wait(qapp, lambda: bool(asks.calls), 'recovery prompt was not shown', 15)
        assert asks.calls
        _wait(qapp, lambda: not workspace.exists(),
              'Discard did not clean the recovered workspace', 10)
        assert not workspace.exists()
    finally:
        if workspace.exists():
            document.cleanup()


def test_stale_temp_workspace_is_cleaned(qapp, window, monkeypatch, tmp_path):
    window, errors = window
    root = tmp_path / 'tmp'
    stale = root / f'{WORKSPACE_PREFIX}stale-qa'
    stale.mkdir()
    (stale / 'old.pdf').write_bytes(b'stale')
    (stale / LIVE).write_text(json.dumps({'pid': 999_999_999}),
                              encoding='ascii')
    old = time.time() - 8 * 24 * 60 * 60
    os.utime(stale, (old, old))
    assert cleanup_workspaces(root) >= 1, 'stale workspace was not removed'
    assert not stale.exists()


def test_clean_close_removes_workspace(qapp, window, monkeypatch, tmp_path):
    window, errors = window
    source = _pdf(tmp_path / 'clean-close.pdf')
    tab = _open(window, qapp, source)
    workspace = Path(tab.document.workspace.name)
    assert workspace.exists()
    window.close_tab(window.tabs.indexOf(tab))
    _wait(qapp, lambda: not workspace.exists(), 'workspace was not removed', 10)
    assert not workspace.exists()
