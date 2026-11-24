import inspect
import math
import os
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('VECTORPDF_NO_RECOVERY', '1')

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QImage, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QLabel, QListWidgetItem, QToolButton

import pytest

from fixtures import drawing
from vector_pdf import ui as ui_module
from vector_pdf.canvas import Canvas
from vector_pdf.document import Document
from vector_pdf.geometry import Sheet, point
from vector_pdf.object_index import index_page
from vector_pdf.render import Tile, TileCache
from vector_pdf.ui import (
    MainWindow, MAX_TEXT_LENGTH, Workspace, about_text, pdf_target, write_ready_token,
)
from vector_pdf.version import VERSION
from vector_pdf.writer import Edit, file_stamp


@pytest.fixture(scope='session')
def app():
    app = QApplication.instance() or QApplication([])
    ui_module.configure_fonts(app)
    return app


class FakeSettings:
    store = {}

    def __init__(self, *args):
        self.values = FakeSettings.store

    def value(self, key, default=None, type=None):
        value = self.values.get(key, default)
        if type is bool:
            return bool(value)
        return value

    def setValue(self, key, value):
        self.values[key] = value


class Task:
    def __init__(self, value=None, error=None):
        self.value, self.error = value, error

    def result(self):
        if self.error is not None:
            raise self.error
        return self.value

    def cancelled(self):
        return False


@pytest.fixture
def window(app, monkeypatch):
    FakeSettings.store = {}
    monkeypatch.setattr(ui_module, 'QSettings', FakeSettings)
    monkeypatch.setenv('VECTORPDF_NO_RECOVERY', '1')
    window = MainWindow()
    window.show()
    app.processEvents()
    yield window
    window._allow_close = True
    window.close()
    app.processEvents()
    window.editor.executor.shutdown(wait=True, cancel_futures=True)
    window.render.shutdown()
    app.processEvents()


def make_tab(window, tmp_path, name='drawing.pdf'):
    source = drawing(tmp_path / name)
    document = Document(source)
    document.stamp = file_stamp(source)
    tab = Workspace(window, document)
    tab.sheets = [Sheet(595, 842, (0, 0, 595, 842), 0), Sheet(595, 842, (0, 0, 595, 842), 0)]
    tab.sheet_loaded = {0, 1}
    tab.permission_checked = True
    tab.read_only = False
    tab.pages.blockSignals(True)
    tab.pages.addItem(QListWidgetItem('1'))
    tab.pages.addItem(QListWidgetItem('2'))
    tab.pages.blockSignals(False)
    tab.current = 0
    tab.view.resize(640, 480)
    tab.view.set_sheet(0, tab.sheets[0])
    window.tabs.addTab(tab, source.name)
    window.tabs.setCurrentIndex(window.tabs.count()-1)
    window.current_changed()
    return tab


def new_edit(text='draft text', ident=-50, size=12.):
    return Edit(0, ident, text, 100., 120., size, 0., family='Sans')


def test_busy_page_switch_is_ignored_and_pages_disabled(window, tmp_path):
    tab = make_tab(window, tmp_path)
    tab.set_busy(True)
    assert not tab.pages.isEnabled()
    tab.page(1)
    assert tab.current == 0
    tab.set_busy(False)
    assert tab.pages.isEnabled()
    tab.page(1)
    assert tab.current == 1


def test_add_before_sheet_is_blocked(window, tmp_path):
    tab = make_tab(window, tmp_path)
    tab.current = 1
    tab.sheet_loaded = {0}
    tab.view.set_sheet(1, tab.sheets[1])
    tab.add()
    assert tab.selected is None
    assert 'Подождите' in tab.note.text() or 'Wait' in tab.note.text()
    tab.sheet_loaded.add(1)
    tab.matches.addItem(QListWidgetItem('stale match'))
    tab.results[999] = new_edit('stale')
    tab.active_area = (0, 0, 10, 10)
    tab.add()
    assert tab.selected is not None
    assert tab.text.maxLength() == MAX_TEXT_LENGTH
    assert tab.matches.count() == 0
    assert not tab.results
    assert tab.active_area is None
    tab.clear_selection()


def test_long_text_and_unusual_size_are_preserved(window, tmp_path):
    tab = make_tab(window, tmp_path)
    original = 'a' * 2000
    assert tab.load_edit(new_edit(original, size=.1))
    assert len(tab.text.text()) == 2000
    assert tab.text.maxLength() >= 2000
    assert tab.size.value() == pytest.approx(.1, abs=.0001)
    assert tab.size.minimum() <= .1

    tab.size.setValue(4500.)
    fields = tab.read_fields()
    assert fields.size == pytest.approx(4500.)
    assert fields.text == original
    assert tab.size.maximum() >= 4500.

    tab.family.setCurrentIndex(tab.family.findData('Mono'))
    tab.edit_color = (.2, .3, .4)
    fields = tab.read_fields()
    assert fields.text == original
    assert fields.size == pytest.approx(4500.)
    tab.preview_move((5., 3.))
    assert tab.view.preview is not None
    assert tab.view.preview.text == original
    assert tab.view.preview.size == pytest.approx(4500.)
    tab.preview_move(None)
    assert tab.read_fields().text == original

    assert tab.load_edit(new_edit('tiny', size=4500.))
    assert tab.size.value() == pytest.approx(4500.)
    assert tab.size.maximum() >= 4500.
    tab.clear_selection()


def test_save_suffix_and_last_directory(window, tmp_path, monkeypatch):
    assert pdf_target(tmp_path / 'collision.docx') == tmp_path / 'collision.docx.pdf'
    assert pdf_target(tmp_path / 'report.v2') == tmp_path / 'report.v2.pdf'
    assert pdf_target(tmp_path / 'report.pdf') == tmp_path / 'report.pdf'

    tab = make_tab(window, tmp_path)
    collision = tmp_path / 'collision.pdf'
    collision.write_bytes(b'user file')
    selected = tmp_path / 'collision.docx'
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', staticmethod(lambda *args: (str(selected), 'PDF')))
    monkeypatch.setattr(window, 'ask', lambda *args: ui_module.QMessageBox.StandardButton.No)

    def immediate(executor, function, done, *args):
        done(Task(function(*args)))
        return Task()

    monkeypatch.setattr(tab.bridge, 'submit', immediate)
    assert tab.save() is True
    target = tmp_path / 'collision.docx.pdf'
    assert target.is_file()
    assert collision.read_bytes() == b'user file'
    assert tab.document.export_name == target.name
    assert tab.document.modified is False
    assert window.settings.value('last-directory') == str(target.parent)
    assert tab.busy is False


def test_existing_target_is_confirmed_for_the_actual_file(window, tmp_path, monkeypatch):
    tab = make_tab(window, tmp_path)
    target = tmp_path / 'copy.pdf'
    target.write_bytes(b'sentinel')
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', staticmethod(lambda *args: (str(target), 'PDF')))
    monkeypatch.setattr(window, 'ask', lambda *args: ui_module.QMessageBox.StandardButton.No)

    def immediate(executor, function, done, *args):
        done(Task(function(*args)))
        return Task()

    monkeypatch.setattr(tab.bridge, 'submit', immediate)
    assert tab.save() is False
    assert target.read_bytes() == b'sentinel'
    assert tab.busy is False


def test_close_flow_save_discard_cancel(window, tmp_path, monkeypatch):
    tab = make_tab(window, tmp_path)
    tab.document.commit({(0, -1): Edit(0, -1, 'committed', 50, 50, 12, 0, family='Sans')})
    assert tab.document.modified

    messages = []
    monkeypatch.setattr(window, 'message', lambda *args, **kwargs: messages.append(args))
    tab.set_busy(True)
    window.close_tab(0)
    assert window.tabs.count() == 1
    assert messages
    tab.set_busy(False)

    monkeypatch.setattr(window, 'ask', lambda *args: ui_module.QMessageBox.StandardButton.Cancel)
    window.close_tab(0)
    assert window.tabs.count() == 1

    callbacks = []
    monkeypatch.setattr(window, 'ask', lambda *args: ui_module.QMessageBox.StandardButton.Save)
    monkeypatch.setattr(tab, 'save', lambda after=None: callbacks.append(after) or True)
    window.close_tab(0)
    assert window.tabs.count() == 1
    assert callbacks
    callbacks[0](False)
    assert window.tabs.count() == 1
    callbacks[0](True)
    assert window.tabs.count() == 0

    discard = make_tab(window, tmp_path, 'discard.pdf')
    discard.document.commit({(0, -2): Edit(0, -2, 'committed', 50, 50, 12, 0, family='Sans')})
    monkeypatch.setattr(window, 'ask', lambda *args: ui_module.QMessageBox.StandardButton.Discard)
    window.close_tab(window.tabs.indexOf(discard))
    assert window.tabs.count() == 0


def test_draft_is_not_lost_on_page_mode_or_close(window, tmp_path, monkeypatch):
    tab = make_tab(window, tmp_path)
    tab.set_mode('text')
    assert tab.load_edit(new_edit('initial text'))
    tab.text.setText('UNCOMMITTED FIELD CHANGE')
    assert tab.has_draft()

    monkeypatch.setattr(window, 'ask', lambda *args: ui_module.QMessageBox.StandardButton.Cancel)
    tab.page(1)
    assert tab.current == 0
    assert tab.text.text() == 'UNCOMMITTED FIELD CHANGE'
    tab.set_mode('view')
    assert tab.view.mode == 'text'
    assert tab.text.text() == 'UNCOMMITTED FIELD CHANGE'
    window.change_mode('view')
    assert tab.view.mode == 'text'
    assert window.actions['text'].isChecked()
    assert not window.actions['view'].isChecked()
    assert tab.text.text() == 'UNCOMMITTED FIELD CHANGE'
    window.close_tab(0)
    assert window.tabs.count() == 1
    assert tab.text.text() == 'UNCOMMITTED FIELD CHANGE'

    monkeypatch.setattr(window, 'ask', lambda *args: ui_module.QMessageBox.StandardButton.Discard)
    tab.page(1)
    assert tab.current == 1
    assert tab.selected is None
    tab.clear_selection()


def test_saved_commits_the_uncommitted_field_draft(window, tmp_path):
    tab = make_tab(window, tmp_path)
    assert tab.load_edit(new_edit('initial'))
    tab.text.setText('DRAFT SAVED')
    state, draft = tab.save_state()
    key = draft.page, draft.id
    assert state[key].text == 'DRAFT SAVED'
    tab.save_token = 7
    target = tmp_path / 'copy.pdf'
    tab.saved(target, state, draft, 7, None, Task(1.5))
    assert tab.document.edits[key].text == 'DRAFT SAVED'
    assert tab.document.exported[key].text == 'DRAFT SAVED'
    assert not tab.has_draft()
    assert not tab.document.modified
    assert tab.document.export_name == target.name
    tab.clear_selection()


def test_selection_from_another_page_is_not_loaded(window, tmp_path, monkeypatch):
    tab = make_tab(window, tmp_path)
    tab.current = 1
    tab.view.set_sheet(1, tab.sheets[1])
    path = Path(tab.document.workspace.name) / '1.pdf'
    path.write_bytes(tab.document.source.read_bytes())
    old = tab.document.key
    selection = Edit(0, -1, 'wrong page', 100, 100, 12, 0, family='Sans')
    monkeypatch.setattr(tab.bridge, 'submit', lambda *args, **kwargs: None)
    tab.render_ready(Task(None), old + ':1', path, {}, None, {}, selection, 1.0, tab.job_token)
    assert tab.selected is None
    assert tab.document.key == old + ':1'
    assert 'другому листу' in tab.note.text() or 'another sheet' in tab.note.text()


def test_update_actions_busy_and_read_only_save(window, tmp_path):
    tab = make_tab(window, tmp_path)
    window.update_actions()
    assert window.actions['save'].isEnabled()
    assert window.actions['text'].isEnabled()
    assert window.actions['add'].isEnabled()
    tab.set_busy(True)
    for key in ('save', 'text', 'region', 'add', 'undo', 'redo'):
        assert not window.actions[key].isEnabled(), key
    tab.set_busy(False)

    tab.read_only = True
    window.update_actions()
    assert window.actions['save'].isEnabled()
    for key in ('text', 'region', 'add'):
        assert not window.actions[key].isEnabled(), key

    tab.document.commit({(0, -3): Edit(0, -3, 'x', 40, 40, 10, 0, family='Sans')})
    window.update_actions()
    assert not window.actions['save'].isEnabled()
    tab.read_only = False


def test_read_only_tab_can_save_an_unchanged_copy(window, tmp_path, monkeypatch):
    tab = make_tab(window, tmp_path)
    tab.read_only = True
    tab.permission_checked = True
    window.update_actions()
    assert window.actions['save'].isEnabled()
    assert not window.actions['text'].isEnabled()
    target = tmp_path / 'read-only-copy.pdf'
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', staticmethod(lambda *args: (str(target), 'PDF')))

    def immediate(executor, function, done, *args):
        done(Task(function(*args)))
        return Task()

    monkeypatch.setattr(tab.bridge, 'submit', immediate)
    assert tab.save() is True
    assert target.is_file()
    assert target.read_bytes() == tab.document.source.read_bytes()
    assert tab.read_only is True
    assert tab.busy is False


def test_destination_open_in_another_tab_is_rejected(window, tmp_path, monkeypatch):
    first = make_tab(window, tmp_path, 'first.pdf')
    second = make_tab(window, tmp_path, 'second.pdf')
    original = second.document.source.read_bytes()
    window.tabs.setCurrentIndex(window.tabs.indexOf(first))
    monkeypatch.setattr(QFileDialog, 'getSaveFileName',
                        staticmethod(lambda *args: (str(second.document.source), 'PDF')))
    messages = []
    monkeypatch.setattr(window, 'message', lambda *args, **kwargs: messages.append(args))
    assert first.save() is False
    assert messages
    assert first.busy is False
    assert second.document.source.read_bytes() == original


def test_tab_mode_sync_and_read_only(window, tmp_path):
    tab = make_tab(window, tmp_path)
    window.mode = 'text'
    window.sync_mode(tab)
    assert tab.view.mode == 'text'
    assert window.actions['text'].isChecked()
    tab.read_only = True
    window.sync_mode(tab)
    assert tab.view.mode == 'view'
    assert window.actions['view'].isChecked()
    tab.read_only = False


def test_tab_switch_keeps_a_draft_when_cancelled(window, tmp_path, monkeypatch):
    first = make_tab(window, tmp_path, 'draft-tab.pdf')
    second = make_tab(window, tmp_path, 'other-tab.pdf')
    window.tabs.setCurrentIndex(window.tabs.indexOf(first))
    first.set_mode('text')
    first.load_edit(new_edit('initial tab text'))
    first.text.setText('TAB DRAFT')
    assert first.has_draft()
    monkeypatch.setattr(window, 'ask', lambda *args: ui_module.QMessageBox.StandardButton.Cancel)
    window.tabs.setCurrentIndex(window.tabs.indexOf(second))
    assert window.current_tab() is first
    assert first.text.text() == 'TAB DRAFT'
    assert first.has_draft()
    first.clear_selection()


def test_about_versions_plain_text_and_ready_token(window, tmp_path, monkeypatch):
    assert QApplication.instance().applicationVersion() == VERSION
    assert window.status_version.text() == 'v' + VERSION
    text = about_text()
    for token in (VERSION, 'Qt:', 'PySide6:', 'PDFium:', 'QPDF:', 'pikepdf:', 'MIT'):
        assert token in text
    assert window.minimumWidth() <= 800
    assert window.minimumHeight() <= 480
    tab = make_tab(window, tmp_path)
    assert tab.note.textFormat() == Qt.TextFormat.PlainText
    assert tab.font_notice.textFormat() == Qt.TextFormat.PlainText
    assert window.status_sheet.textFormat() == Qt.TextFormat.PlainText
    assert tab.right_scroll.widget() is tab.right
    assert 'QWidget#drawing' in window.empty.styleSheet()
    assert window.auto_update.isChecked() is False

    signature = inspect.signature(ui_module.main)
    assert signature.parameters['ready_token'].default is None
    token = tmp_path / 'ready.txt'
    write_ready_token(token)
    payload = token.read_text(encoding='utf-8')
    assert f'version={VERSION}' in payload
    assert 'pid=' in payload

    monkeypatch.setenv('VECTORPDF_SAFE_MODE', '1')
    assert ui_module.safe_mode()
    monkeypatch.setenv('VECTORPDF_SAFE_MODE', '0')
    assert not ui_module.safe_mode()


def test_per_page_zoom_and_rotation_are_remembered(window, tmp_path):
    tab = make_tab(window, tmp_path)
    tab.view.zoom = 2.
    tab.view.rotation = 90
    tab.page(1)
    assert tab.current == 1
    tab.view.zoom = 1.5
    tab.view.rotation = 180
    tab.page(0)
    assert tab.current == 0
    assert tab.view.zoom == pytest.approx(2.)
    assert tab.view.rotation == 90
    tab.page(1)
    assert tab.view.zoom == pytest.approx(1.5)
    assert tab.view.rotation == 180


def test_new_label_auto_applies_and_can_be_edited_and_deleted(window, app, tmp_path, monkeypatch):
    tab = make_tab(window, tmp_path)
    canvas = tab.view
    tab.document.indexes[0] = index_page(tab.document.source, 0)

    def unexpected_dialog(*args):
        pytest.fail(f'Unexpected dialog: {args}')

    def wait_until(condition):
        deadline = monotonic()+5
        while monotonic() < deadline:
            app.processEvents()
            if condition():
                return
            sleep(.01)
        assert condition()

    monkeypatch.setattr(window, 'ask', unexpected_dialog)
    monkeypatch.setattr(window, 'error', unexpected_dialog)
    window.activateWindow()
    app.processEvents()
    window.add()
    QTest.keyClicks(tab.text, 'Label')
    key = tab.selected.page, tab.selected.id
    tab.family.setFocus()
    app.processEvents()
    assert key not in tab.document.edits
    canvas.setFocus()
    wait_until(lambda: key in tab.document.edits and not tab.busy)
    edit = tab.document.edits[key]
    assert edit.text == 'Label'

    window.change_mode('view')
    xy = QPointF(*point(canvas.sheet.to_screen, edit.x+2, edit.y+3))
    position = canvas.transform().map(xy).toPoint()
    QTest.mouseDClick(canvas, Qt.MouseButton.LeftButton, pos=position)
    assert tab.view.mode == 'text' and tab.text.hasFocus()
    assert tab.selected == edit
    QTest.keyClick(tab.text, Qt.Key.Key_Home)
    QTest.keyClick(tab.text, Qt.Key.Key_Delete)
    QTest.keyClicks(tab.text, 'R')
    assert tab.text.text() == 'Rabel'
    assert tab.document.edits[key].text == 'Label'
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(7, 7))
    wait_until(lambda: tab.document.edits[key].text == 'Rabel' and not tab.busy)

    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=position)
    assert tab.selected.id == key[1]
    QTest.keyClick(canvas, Qt.Key.Key_Delete)
    wait_until(lambda: key not in tab.document.edits and not tab.busy)
    tab.travel(-1)
    wait_until(lambda: key in tab.document.edits and not tab.busy)
    assert tab.document.edits[key].text == 'Rabel'


def test_toolbar_reflows_with_readable_russian_and_english_labels(window, app):
    header = window.findChild(ui_module.Toolbar)
    brand = header.findChild(QLabel, 'brand')
    for width in (1320, 780, 1000):
        window.resize(width, 820)
        for language in ('ru', 'en'):
            window.language(language)
            app.processEvents()
            assert header.width() == window.centralWidget().width()
            for widget in [brand, *header.findChildren(QToolButton)]:
                assert header.rect().contains(widget.geometry())
                assert widget.width() >= widget.sizeHint().width()


def test_fit_page_tracks_viewport_and_manual_zoom_survives_resize(window, app, tmp_path):
    tab = make_tab(window, tmp_path)
    canvas = tab.view
    tab.sheets[1] = Sheet(842, 595, (0, 0, 842, 595), 0)

    def assert_fitted():
        page = canvas.transform().mapRect(QRectF(0, 0, canvas.sheet.width, canvas.sheet.height))
        assert QRectF(canvas.rect()).contains(page)
        assert page.center().x() == pytest.approx(canvas.width()/2)
        assert page.center().y() == pytest.approx(canvas.height()/2)
        assert page.width()/page.height() == pytest.approx(canvas.sheet.width/canvas.sheet.height)

    app.processEvents()
    assert_fitted()
    initial = canvas.zoom
    window.resize(1040, 630)
    app.processEvents()
    assert canvas.zoom < initial
    assert_fitted()
    canvas.zoom_at(2.)
    manual = canvas.zoom
    window.resize(1320, 900)
    app.processEvents()
    assert canvas.zoom == manual

    tab.page(1)
    assert_fitted()
    tab.page(0)
    assert canvas.zoom == manual
    window.actions['fit'].trigger()
    assert_fitted()
    wheel = SimpleNamespace(
        angleDelta=lambda: QPoint(0, 120),
        pixelDelta=lambda: QPoint(),
        position=lambda: QPointF(canvas.rect().center()),
        accept=lambda: None,
    )
    fitted_zoom = canvas.zoom
    canvas.wheelEvent(wheel)
    assert canvas.zoom > fitted_zoom
    assert canvas.fit_page

    start = canvas.rect().center()
    initial_pan = QPointF(canvas.pan)
    QTest.mousePress(canvas, Qt.MouseButton.MiddleButton, pos=start)
    QTest.mouseMove(canvas, start+QPoint(60, 25))
    QTest.mouseRelease(canvas, Qt.MouseButton.MiddleButton, pos=start+QPoint(60, 25))
    assert canvas.pan != initial_pan
    assert canvas.fit_page

    window.resize(1160, 810)
    app.processEvents()
    assert_fitted()



def test_tile_order_fit_clamp_and_peek_stats(window):
    canvas = Canvas()
    canvas.resize(800, 600)
    canvas.set_sheet(0, Sheet(1, 1, (0, 0, 1, 1), 0))
    assert canvas.zoom <= 24.
    assert math.isfinite(canvas.zoom)
    canvas.set_sheet(0, Sheet(float('nan'), float('nan'), (0, 0, float('nan'), float('nan')), 0))
    assert math.isfinite(canvas.zoom)

    canvas.set_sheet(0, Sheet(100, 100, (0, 0, 100, 100), 0))
    canvas.cache = TileCache()
    high = QImage(8, 8, QImage.Format.Format_RGB32)
    high.fill(Qt.GlobalColor.red)
    low = QImage(8, 8, QImage.Format.Format_RGB32)
    low.fill(Qt.GlobalColor.blue)
    high_key, low_key = Tile(0, 2., 0, 0), Tile(0, 1., 0, 0)
    canvas.cache.put(high_key, high)
    canvas.cache.put(low_key, low)
    assert [key.level for key, _ in canvas.tiles_for_paint()] == [1., 2.]
    hits, misses = canvas.cache.hits, canvas.cache.misses
    assert canvas.cache.peek(high_key) is not None
    assert (canvas.cache.hits, canvas.cache.misses) == (hits, misses)


def test_region_starts_marquee_and_wheel_release_clear_drag(window, tmp_path):
    tab = make_tab(window, tmp_path)
    canvas = tab.view
    canvas.set_sheet(0, Sheet(100, 100, (0, 0, 100, 100), 0))
    canvas.zoom = 1.
    canvas.pan = QPointF()
    canvas.selection = (20., 20., 60., 60.)
    canvas.move_enabled = True
    inside = QPointF(*point(canvas.sheet.to_screen, 30., 30.))

    canvas.set_mode('text')
    press = QMouseEvent(QEvent.Type.MouseButtonPress, inside,
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    canvas.mousePressEvent(press)
    assert canvas.drag and canvas.drag[0] == 'move'
    canvas.drag = None

    canvas.set_mode('region')
    press = QMouseEvent(QEvent.Type.MouseButtonPress, inside,
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    canvas.mousePressEvent(press)
    assert canvas.drag and canvas.drag[0] == 'region'
    canvas.drag = None
    canvas.marquee = None

    previews = []
    canvas.move_preview.connect(previews.append)
    canvas.set_mode('text')
    canvas.drag = ('move', QPointF(0, 0), (0., 0.))
    fake_wheel = SimpleNamespace(
        angleDelta=lambda: QPoint(0, 120),
        pixelDelta=lambda: QPoint(0, 0),
        position=lambda: QPointF(10, 10),
        accept=lambda: None,
    )
    canvas.wheelEvent(fake_wheel)
    assert canvas.drag is None
    assert previews[-1] is None

    canvas.drag = ('move', QPointF(0, 0), (0., 0.))
    release = QMouseEvent(QEvent.Type.MouseButtonRelease, inside,
                          Qt.MouseButton.RightButton, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier)
    canvas.mouseReleaseEvent(release)
    assert canvas.drag is None
    assert previews[-1] is None


def test_edit_failed_preserves_read_only_and_bridge_does_not_crash(window, tmp_path, monkeypatch):
    tab = make_tab(window, tmp_path)
    tab.selected = SimpleNamespace(original=SimpleNamespace(editable=False), x=10., y=10., text='x')
    tab.set_busy(True)
    errors = []
    monkeypatch.setattr(tab, 'preview', lambda: (_ for _ in ()).throw(RuntimeError('preview failed')))
    monkeypatch.setattr(window, 'error', errors.append)
    monkeypatch.setattr(ui_module.LOG, 'exception', lambda *args, **kwargs: None)
    tab.edit_failed(RuntimeError('edit failed'))
    assert tab.busy is False
    assert not tab.form.isEnabled()
    assert tab.view.move_enabled is False
    assert errors

    monkeypatch.setattr(window, 'error', lambda error: (_ for _ in ()).throw(RuntimeError('dialog failed')))
    tab.set_busy(True)
    tab.edit_failed(RuntimeError('edit failed again'))
    assert tab.busy is False

    tab.bridge.finish(lambda future: (_ for _ in ()).throw(RuntimeError('callback failed')), Task(None))


def test_render_ready_invalidates_edited_tiles(window, tmp_path, monkeypatch):
    tab = make_tab(window, tmp_path)
    tab.current = 0
    tab.view.set_sheet(0, tab.sheets[0], fit=False)
    image = QImage(64, 64, QImage.Format.Format_RGB32)
    image.fill(0xffffffff)
    tile = Tile(0, 1.0, 0, 0)
    tab.view.cache.put(tile, image)
    assert tile in tab.view.cache
    path = Path(tab.document.workspace.name) / '1.pdf'
    old = tab.document.key
    monkeypatch.setattr(tab.bridge, 'submit', lambda *args, **kwargs: None)
    tab.render_ready(Task(None), old + ':1', path, {}, None,
                     {0: [(100., 600., 200., 700.)]}, None, 1.0, tab.job_token)
    assert tab.document.metrics.invalidated >= 1
    assert tile not in tab.view.cache
    tab.clear_selection()
