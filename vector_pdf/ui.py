from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import replace
import faulthandler
import logging
from logging.handlers import RotatingFileHandler
from math import cos, hypot, isfinite, radians, sin
import os
from pathlib import Path
import platform
from tempfile import mkstemp
import sys
from time import perf_counter, time
from threading import Thread
from weakref import ref

from PySide6.QtCore import QEvent, QObject, QPointF, QSettings, QSize, Qt, QTimer, QUrl, Signal, qInstallMessageHandler
from PySide6.QtGui import (
    QAction, QActionGroup, QColor, QDesktopServices, QFont, QFontDatabase, QIcon, QKeySequence,
    QPainter, QPalette, QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QBoxLayout, QCheckBox, QComboBox, QDockWidget, QDoubleSpinBox, QFileDialog, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QLayout, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSplitter, QTabWidget, QToolButton, QVBoxLayout,
    QWidget, QColorDialog,
)

from .canvas import Canvas, image_from_render
from .document import (
    Document, LocalEditor, cleanup_temp_files, cleanup_workspaces, same_path, scan_recovery,
)
from .geometry import box, compose, point
from .palette import STRIPES, THEMES, stylesheet
from .render import RenderEngine, TileCache
from .fonts import ASSETS, FALLBACKS, import_font, learn_fonts
from .version import REPOSITORY, VERSION
from .writer import Edit, file_stamp
from . import updater

LOG = logging.getLogger(__name__)
MAX_TEXT_LENGTH = 100_000
MIN_TEXT_LENGTH = 1_500
THUMBNAIL_BUDGET = 80
INACTIVE_TILE_BUDGET = 32*1024**2
APP_ICON = ASSETS / 'vector-pdf.png'
_LOG_HANDLE = None
_FAULT_HANDLE = None


def safe_mode():
    return os.environ.get('VECTORPDF_SAFE_MODE', '').strip().lower() in {'1', 'true', 'yes', 'on'}


def log_directory():
    base = os.environ.get('LOCALAPPDATA')
    if base:
        return Path(base) / 'VectorPDF' / 'logs'
    return Path.home() / '.local' / 'share' / 'vector-pdf' / 'logs'


def setup_logging():
    """Install a rotating application log once; never let logging break startup."""
    global _LOG_HANDLE
    if _LOG_HANDLE is not None:
        return Path(_LOG_HANDLE.name)
    try:
        directory = log_directory()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'vector-pdf.log'
        handler = RotatingFileHandler(path, maxBytes=2*1024**2, backupCount=3, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        _LOG_HANDLE = handler.stream
        try:
            global _FAULT_HANDLE
            _FAULT_HANDLE = open(directory / 'faulthandler.log', 'a', encoding='utf-8')
            faulthandler.enable(_FAULT_HANDLE)
        except Exception:
            pass
        return path
    except Exception:
        return None


def install_exception_hooks():
    previous = sys.excepthook

    def hook(kind, value, traceback):
        try:
            LOG.critical('Uncaught exception', exc_info=(kind, value, traceback))
        except Exception:
            pass
        previous(kind, value, traceback)

    sys.excepthook = hook

    def thread_hook(args):
        try:
            LOG.critical('Uncaught thread exception in %s',
                         getattr(args.thread, 'name', '?'),
                         exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        except Exception:
            pass

    try:
        import threading
        threading.excepthook = thread_hook
    except (ImportError, AttributeError):
        pass

    def qt_handler(mode, context, message):
        try:
            from PySide6.QtCore import QtMsgType
            level = logging.WARNING if mode != QtMsgType.QtInfoMsg else logging.INFO
            logging.getLogger('qt').log(level, '%s', message)
        except Exception:
            pass

    try:
        qInstallMessageHandler(qt_handler)
    except Exception:
        pass


def component_versions():
    values = {
        'VECTOR PDF': VERSION,
        'Python': platform.python_version(),
        'Qt': '—',
        'PySide6': '—',
        'PDFium': '—',
        'QPDF': '—',
        'pikepdf': '—',
        'Platform': platform.platform(),
        'Build': 'portable' if getattr(sys, 'frozen', False) else 'source',
    }
    try:
        from PySide6 import QtCore, __version__ as pyside_version
        values['Qt'] = QtCore.qVersion()
        values['PySide6'] = pyside_version
    except Exception:
        pass
    try:
        import pypdfium2.version as pdfium_version
        values['PDFium'] = str(pdfium_version.PDFIUM_INFO)
    except Exception:
        pass
    try:
        import pikepdf
        values['pikepdf'] = pikepdf.__version__
        values['QPDF'] = getattr(pikepdf, '__libqpdf_version__', '—')
    except Exception:
        pass
    return values


def about_text():
    lines = ['VECTOR PDF', '']
    lines.extend(f'{name}: {value}' for name, value in component_versions().items())
    lines.extend([
        '',
        'Application license: MIT.',
        'Qt/PySide6: LGPL/GPL. PDFium: Apache/BSD. QPDF: Apache-2.0.',
        'pikepdf: MPL-2.0. fontTools: MIT. DejaVu fonts: Bitstream Vera license.',
        '',
        f'Project: https://github.com/{REPOSITORY}',
        'MIT license: https://opensource.org/licenses/MIT',
        'Qt LGPL: https://www.qt.io/licensing/open-source-lgpl-obligations',
        f'Third-party overview: https://github.com/{REPOSITORY}/blob/main/THIRD_PARTY.md',
        'License overview: THIRD_PARTY.md in the application folder.',
        f'Logs: {log_directory()}',
    ])
    return '\n'.join(lines)


def pdf_target(path):
    """Append .pdf instead of replacing an unrelated suffix."""
    target = Path(path)
    if target.suffix.lower() != '.pdf':
        target = target.with_name(target.name + '.pdf')
    return target


def friendly_error(error):
    if isinstance(error, FileNotFoundError):
        return 'The file is no longer available. It may have been moved or deleted.'
    if isinstance(error, PermissionError):
        return 'Windows denied access to the file. Close any program that uses it and try again.'
    if isinstance(error, ValueError):
        return str(error).strip() or 'The operation was rejected.'
    winerror = getattr(error, 'winerror', None)
    if winerror in (5, 32):
        return 'The file is in use by another program. Close it and try again.'
    text = str(error).strip()
    return text or 'The operation could not be completed.'


def write_ready_token(path):
    """Atomically write the version/PID readiness token used by the updater."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = mkstemp(prefix='.ready-', suffix='.txt', dir=destination.parent)
    os.close(fd)
    try:
        with open(temporary, 'w', encoding='utf-8') as handle:
            handle.write(f'version={VERSION}\npid={os.getpid()}\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Ribbon(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedHeight(12)

    def paintEvent(self, event):
        with QPainter(self) as painter:
            for i, color in enumerate(STRIPES):
                painter.fillRect(0, i*3, self.width(), 3, QColor(color))


class Toolbar(QWidget):
    def __init__(self):
        super().__init__(objectName='toolbar')
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.groups = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self.groups.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.groups.setContentsMargins(15, 10, 12, 10)
        self.groups.setSpacing(8)
        self.files = QHBoxLayout()
        self.tools = QHBoxLayout()
        for group in (self.files, self.tools):
            group.setSpacing(7)
        self.groups.addLayout(self.files)
        self.groups.addLayout(self.tools, 1)

    def arrange(self):
        margins = self.groups.contentsMargins()
        width = (self.files.minimumSize().width() + self.tools.minimumSize().width()
                 + margins.left() + margins.right() + self.groups.spacing())
        direction = (QBoxLayout.Direction.LeftToRight if self.width() >= width
                     else QBoxLayout.Direction.TopToBottom)
        if self.groups.direction() != direction:
            self.groups.setDirection(direction)
            self.updateGeometry()

    def event(self, event):
        result = super().event(event)
        if event.type() in (QEvent.Type.Resize, QEvent.Type.LayoutRequest):
            self.arrange()
        return result


class Language:
    def __init__(self, code):
        self.code = code
        self.bindings = []

    def get(self, ru, en):
        return ru if self.code == 'ru' else en

    def bind(self, setter, ru, en):
        self.bindings.append((ref(setter.__self__), setter.__name__, ru, en))
        setter(self.get(ru, en))

    def change(self, code):
        self.code = code
        live = []
        for item in self.bindings:
            owner, method, ru, en = item
            if (widget := owner()) is None:
                continue
            try:
                getattr(widget, method)(self.get(ru, en))
            except RuntimeError:
                continue
            live.append(item)
        self.bindings = live


class Bridge(QObject):
    complete = Signal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.complete.connect(self.finish, Qt.ConnectionType.QueuedConnection)

    def submit(self, executor, fn, done, *args):
        future = executor.submit(fn, *args)

        def completed(result):
            try:
                self.complete.emit(done, result)
            except RuntimeError:
                pass

        future.add_done_callback(completed)
        return future

    def finish(self, callback, future):
        try:
            if not future.cancelled():
                callback(future)
        except Exception:
            # A Python exception in a Qt slot must never escape into Qt.  Log it and
            # let the workspace's own try/finally protect busy flags.
            try:
                LOG.exception('Unhandled exception in an asynchronous UI callback')
            except Exception:
                pass


class Workspace(QWidget):
    def __init__(self, window, document):
        super().__init__()
        self.window, self.document = window, document
        self.lang, self.bridge = window.lang, window.bridge
        self.render, self.editor = window.render, window.editor
        self.closed = False
        self.busy = False
        self.sheets = []
        self.sheet_loaded = set()
        self.current = 0
        self.selected = None
        self.auto_apply = False
        self.results = {}
        self.edit_color = None
        self.position_draft = None
        self.drag_origin = None
        self.active_area = None
        self.pending_area = None
        self.indexing = set()
        self.permission_checked = False
        self.read_only = False
        self.tile_job = None
        self.tile_key = None
        self.tiles = []
        self.thumb_jobs = {}
        self.thumb_rev = {}
        self.thumb_icons = OrderedDict()
        self.thumb_attempts = {}
        self.thumb_retry_after = {}
        self.view_states = {}
        self.job_token = 0
        self.save_token = 0
        self.pending_action = None
        self.export_name = None
        self.view = Canvas()
        self.view.cache = TileCache()
        self.view.surface = window.surface
        self.view.set_mode('view')
        self.view.demand.connect(self.demand)
        self.view.hit.connect(self.hit)
        self.view.edit_hit.connect(lambda xy: self.hit(xy, focus_text=True))
        self.view.area.connect(self.area)
        self.view.zoomed.connect(lambda _: self.status())
        self.view.coordinate.connect(self.position)
        self.view.move_preview.connect(self.preview_move)
        self.view.move_finished.connect(self.finish_move)
        self.xy = None

        self.splitter = QSplitter()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.splitter)
        self.left, ll = self.panel('Листы', 'Sheets')
        self.pages = QListWidget()
        self.pages.setViewMode(QListWidget.ViewMode.IconMode)
        self.pages.setFlow(QListWidget.Flow.TopToBottom)
        self.pages.setWrapping(False)
        self.pages.setMovement(QListWidget.Movement.Static)
        self.pages.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.pages.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.pages.setIconSize(QSize(140, 78))
        self.pages.setGridSize(QSize(150, 106))
        self.pages.currentRowChanged.connect(self.page)
        self.pages.verticalScrollBar().valueChanged.connect(lambda _: self.queue_thumbnails())
        ll.addWidget(self.pages)
        self.splitter.addWidget(self.left)
        self.splitter.addWidget(self.view)
        self.right, rl = self.panel('Свойства текста', 'Text properties')
        self.note = QLabel()
        self.note.setObjectName('panelNote')
        self.note.setWordWrap(True)
        self.note.setTextFormat(Qt.TextFormat.PlainText)
        self.note.setOpenExternalLinks(False)
        rl.addWidget(self.note)
        self.matches = QListWidget()
        self.matches.setMaximumHeight(126)
        self.matches.itemActivated.connect(self.select_item)
        self.matches.itemClicked.connect(self.select_item)
        rl.addWidget(self.matches)

        self.form = QWidget()
        QApplication.instance().focusChanged.connect(self.form_focus_changed)
        fields = QFormLayout(self.form)
        fields.setContentsMargins(0, 2, 0, 0)
        self.text = QLineEdit()
        self.text.setMaxLength(MAX_TEXT_LENGTH)
        self.text.textChanged.connect(self.preview)
        self.text.returnPressed.connect(self.apply)
        self.text.setPlaceholderText('')
        self.family = QComboBox()
        for family in ('Original', *FALLBACKS):
            self.family.addItem(family, family)
        self.family.currentIndexChanged.connect(self.preview)
        self.size = self.spin(.01, 5000, ' pt')
        self.angle = self.spin(-360, 360, '°')
        self.italic = QCheckBox()
        self.lang.bind(self.italic.setText, 'Наклонный шрифт', 'Italic')
        self.italic.toggled.connect(self.preview)
        self.color = QPushButton()
        self.color.clicked.connect(self.choose_color)
        for widget, ru, en in [(self.text, 'Текст', 'Text'), (self.family, 'Шрифт', 'Font'),
                               (self.size, 'Размер', 'Size'),
                               (self.angle, 'Угол', 'Angle'), (self.color, 'Цвет', 'Color')]:
            label = QLabel()
            self.lang.bind(label.setText, ru, en)
            label.setBuddy(widget)
            fields.addRow(label, widget)
        fields.addRow(self.italic)
        self.font_notice = QLabel()
        self.font_notice.setWordWrap(True)
        self.font_notice.setObjectName('panelNote')
        self.font_notice.setTextFormat(Qt.TextFormat.PlainText)
        self.font_notice.setOpenExternalLinks(False)
        fields.addRow(self.font_notice)
        row = QHBoxLayout()
        self.apply_button = QPushButton(objectName='primary')
        self.remove_button = QPushButton()
        self.lang.bind(self.apply_button.setText, 'Применить', 'Apply')
        self.lang.bind(self.remove_button.setText, 'Удалить', 'Delete')
        self.apply_button.clicked.connect(self.apply)
        self.remove_button.clicked.connect(self.remove)
        row.addWidget(self.apply_button)
        row.addWidget(self.remove_button)
        fields.addRow(row)
        rl.addWidget(self.form)
        rl.addStretch()
        self.right_scroll = QScrollArea()
        self.right_scroll.setWidget(self.right)
        self.right_scroll.setWidgetResizable(True)
        self.right_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.right_scroll.setMinimumWidth(200)
        self.splitter.addWidget(self.right_scroll)
        self.left.setMinimumWidth(120)
        self.right.setMinimumWidth(200)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([155, 810, 285])
        self.clear_selection()
        self.notice('Открытие PDF…', 'Opening PDF…')

    def panel(self, ru, en):
        widget = QWidget(objectName='side')
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(11, 12, 11, 11)
        title = QLabel(objectName='panelHeading')
        self.lang.bind(title.setText, ru, en)
        layout.addWidget(title)
        return widget, layout

    def spin(self, lower, upper, suffix):
        widget = QDoubleSpinBox()
        widget.setRange(lower, upper)
        widget.setDecimals(4)
        widget.setSuffix(suffix)
        widget.valueChanged.connect(self.preview)
        return widget

    def start(self):
        try:
            if self.document.stamp is None:
                self.document.stamp = file_stamp(self.document.source)
        except OSError as error:
            self.window.error(error)
            return False

        def open_snapshot():
            return self.render.open(self.document.key, self.document.ensure_basis())

        self.bridge.submit(self.render.executor, open_snapshot, self.opened)
        self.bridge.submit(self.editor.executor, self.editor.check, self.permissions, self.document)
        return True

    def permissions(self, task):
        if self.closed:
            return
        self.permission_checked = True
        try:
            try:
                task.result()
            except Exception as error:
                self.read_only = True
                self.view.set_mode('view')
                self.notice('Только просмотр. '+str(error), 'Read-only. '+str(error))
            self.status()
            self.window.sync_mode(self)
            self.window.update_actions()
        finally:
            QTimer.singleShot(0, self.queue_thumbnails)

    def opened(self, task):
        if self.closed:
            return
        try:
            self.sheets, elapsed = task.result()
        except Exception as error:
            self.window.error(error)
            self.window.close_tab(self.window.tabs.indexOf(self))
            return
        self.document.metrics.opened = elapsed
        self.sheet_loaded.add(0)
        self.pages.blockSignals(True)
        try:
            for i, sheet in enumerate(self.sheets, 1):
                item = QListWidgetItem(f'{i}   ·   {format_name(sheet)}')
                item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
                self.pages.addItem(item)
            self.pages.setCurrentRow(0)
        finally:
            self.pages.blockSignals(False)
        self.view_states.clear()
        self.view.set_sheet(0, self.sheets[0])
        if not self.read_only:
            self.notice('Выберите «Текст» или «Область», чтобы начать правку.',
                        'Choose Text or Area to begin editing.')
        self.window.sync_mode(self)
        self.status()
        self.view.refresh()
        QTimer.singleShot(550, self.queue_thumbnails)

    def status(self):
        self.window.update_status()

    def position(self, xy):
        self.xy = xy
        self.status()

    def set_busy(self, flag):
        self.busy = bool(flag)
        try:
            self.pages.setEnabled(not self.busy)
        except RuntimeError:
            pass
        self.window.update_actions()
        if not self.busy:
            QTimer.singleShot(0, self.queue_thumbnails)

    def page(self, number):
        if self.closed or number < 0 or number >= len(self.sheets):
            return
        if self.busy:
            self._restore_page_row()
            return
        if number == self.current:
            return
        if self.has_draft():
            self._restore_page_row()
            self.resolve_draft(lambda: self._switch_page(number), self._restore_page_row)
            return
        self._switch_page(number)

    def _restore_page_row(self):
        if self.closed:
            return
        try:
            blocked = self.pages.blockSignals(True)
            self.pages.setCurrentRow(self.current)
            self.pages.blockSignals(blocked)
        except RuntimeError:
            pass

    def _switch_page(self, number):
        if self.closed or self.busy or number < 0 or number >= len(self.sheets) or number == self.current:
            return
        if 0 <= self.current < len(self.sheets):
            self.view_states[self.current] = self.view.view_state()
        self.current = number
        try:
            if self.pages.currentRow() != number:
                blocked = self.pages.blockSignals(True)
                self.pages.setCurrentRow(number)
                self.pages.blockSignals(blocked)
        except RuntimeError:
            pass
        self.tiles.clear()
        self.active_area = self.pending_area = None
        self.xy = None
        self.clear_selection()
        self.matches.clear()
        self.matches.hide()
        self.view.set_sheet(number, self.sheets[number], view_state=self.view_states.get(number))
        self.status()
        if number not in self.sheet_loaded:
            self.bridge.submit(self.render.executor, self.render.sheet,
                               lambda task: self.sheet_ready(number, task), self.document.key, number)
        else:
            self.queue_thumbnails()

    def sheet_ready(self, number, task):
        if self.closed:
            return
        try:
            self.sheets[number] = task.result()
            self.sheet_loaded.add(number)
        except Exception as error:
            if number == self.current:
                self.notice(str(error))
            self.status()
            return
        if number == self.current and not self.busy:
            try:
                self.view.set_sheet(number, self.sheets[number], fit=False)
            except (TypeError, ValueError, ZeroDivisionError):
                LOG.exception('Could not apply the sheet geometry')
        self.status()
        QTimer.singleShot(0, self.queue_thumbnails)

    def queue_thumbnails(self):
        if (self.closed or self.busy or not self.left.isVisible() or not self.sheets
                or self.tile_job or self.thumb_jobs or not self.pages.count()):
            return
        try:
            viewport = self.pages.viewport().rect()
            visible = [i for i in range(min(len(self.sheets), self.pages.count()))
                       if self.pages.item(i) is not None
                       and self.pages.visualItemRect(self.pages.item(i)).intersects(viewport)]
        except RuntimeError:
            return
        now = time()
        number = next((i for i in visible if i not in self.thumb_rev
                       and now >= self.thumb_retry_after.get(i, 0)), None)
        if number is None:
            return
        key = self.document.key
        self.thumb_jobs[number] = self.bridge.submit(self.render.executor, self.render.thumbnail,
            lambda task: self.thumbnail_done(number, key, task), key, number)

    def thumbnail_done(self, number, key, task):
        try:
            self.thumb_jobs.pop(number, None)
            if self.closed:
                return
            try:
                data = task.result()
                if not data or self.document.key != key:
                    raise ValueError('empty thumbnail')
                pixmap = QPixmap.fromImage(image_from_render(data))
                icon = QIcon(pixmap)
                icon.addPixmap(pixmap, QIcon.Mode.Selected)
                if self.pages.item(number) is not None:
                    self._store_thumbnail(number, icon)
                self.thumb_rev[number] = key
                self.thumb_attempts.pop(number, None)
                self.thumb_retry_after.pop(number, None)
            except Exception:
                attempts = self.thumb_attempts.get(number, 0)+1
                self.thumb_attempts[number] = attempts
                if attempts >= 2:
                    self.thumb_rev[number] = key
                else:
                    self.thumb_retry_after[number] = time()+.7
                    QTimer.singleShot(750, self.queue_thumbnails)
            if self.tiles:
                self.next_tile()
            else:
                QTimer.singleShot(30, self.queue_thumbnails)
        finally:
            if number in self.thumb_jobs:
                self.thumb_jobs.pop(number, None)

    def _store_thumbnail(self, number, icon):
        self.thumb_icons[number] = icon
        self.thumb_icons.move_to_end(number)
        self.pages.item(number).setIcon(icon)
        while len(self.thumb_icons) > THUMBNAIL_BUDGET:
            for candidate in list(self.thumb_icons):
                if candidate == self.current:
                    continue
                self.thumb_icons.pop(candidate, None)
                self.thumb_rev.pop(candidate, None)
                item = self.pages.item(candidate)
                if item is not None:
                    item.setIcon(QIcon())
                break
            else:
                break

    def demand(self, tiles):
        if self.closed:
            return
        self.tiles = tiles
        if self.tile_job and self.tile_key not in tiles and self.tile_job.cancel():
            self.tile_job, self.tile_key = None, None
        self.next_tile()

    def next_tile(self):
        if self.closed or self.tile_job:
            return
        while self.tiles:
            key = self.tiles.pop(0)
            if self.view.cache.peek(key) is not None:
                self.view.cache.hits += 1
                continue
            self.view.cache.misses += 1
            source = self.document.key
            self.tile_key = key
            self.tile_job = self.bridge.submit(self.render.executor, self.render.render,
                lambda task: self.tile_done(source, key, task), source, key)
            return
        QTimer.singleShot(135, self.queue_thumbnails)

    def tile_done(self, source, key, task):
        try:
            self.tile_job, self.tile_key = None, None
            if self.closed:
                return
            try:
                data = task.result()
                if data and self.document.key == source:
                    self.document.metrics.render = data[-1]
                    self.view.cache.put(key, image_from_render(data))
                    if key.page == self.current:
                        self.view.update()
            except Exception as error:
                self.notice(str(error))
        finally:
            self.tile_job, self.tile_key = None, None
            self.next_tile()

    def notice(self, ru, en=None):
        self.note.setText(self.lang.get(ru, en or ru))

    def clear_selection(self):
        self.selected = None
        self.auto_apply = False
        self.drag_origin = self.position_draft = None
        self.view.cancel_move()
        self.view.move_enabled = False
        self.view.selection = self.view.preview = self.view.move_bounds = None
        self.view.update()
        self.form.hide()
        self.matches.clear()
        self.matches.hide()
        self.results.clear()

    def current_draft(self):
        if self.selected is None or self.busy or not self.form.isEnabled():
            return None
        try:
            edit = self.read_fields()
        except Exception:
            LOG.exception('Could not read the property draft')
            return None
        if self.auto_apply and not self.selected.text and edit is not None and not edit.text:
            return None
        return edit if edit is not None and edit != self.selected else None

    def has_draft(self):
        return self.current_draft() is not None

    def form_focus_changed(self, previous, current):
        if (self.auto_apply and previous is not None and self.form.isAncestorOf(previous)
                and (current is None or not self.form.isAncestorOf(current))):
            QTimer.singleShot(0, self, self.finish_text_input)

    def finish_text_input(self):
        focus = QApplication.focusWidget()
        if (self.closed or self.busy or not self.auto_apply or self.view.drag
                or (focus is not None and self.form.isAncestorOf(focus))):
            return
        self.apply()

    def resolve_draft(self, action, canceled=None):
        """Ask before throwing away text that is still only in the form."""
        if not self.has_draft():
            action()
            return True
        answer = QMessageBox.StandardButton.Apply
        if not self.auto_apply:
            answer = self.window.ask(
                self.lang.get('Несохранённые изменения', 'Unapplied change'),
                self.lang.get('Применить текущий текст перед продолжением?',
                              'Apply the current text before continuing?'),
                QMessageBox.StandardButton.Apply | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
        if answer == QMessageBox.StandardButton.Apply:
            self.pending_action = action
            if not self.apply():
                self.pending_action = None
                if canceled:
                    canceled()
            return True
        if answer == QMessageBox.StandardButton.Discard:
            self.clear_selection()
            action()
            return True
        if canceled:
            canceled()
        return False

    def set_mode(self, mode, prompt=True):
        if self.closed or self.busy:
            return False
        if self.read_only:
            self._apply_mode('view')
            return True
        if mode == self.view.mode:
            if not prompt and mode in ('view', 'region') and self.has_draft():
                return False
            self._apply_mode(mode)
            return True
        if mode in ('view', 'region') and self.has_draft():
            if not prompt:
                return False
            return self.resolve_draft(lambda: self._apply_mode(mode))
        self._apply_mode(mode)
        return True

    def _apply_mode(self, mode):
        if self.closed:
            return
        self.view.set_mode(mode)
        if mode == 'view':
            self.active_area = self.pending_area = None
            self.clear_selection()
            if self.read_only:
                self.notice('Только просмотр. Сохранение копии доступно.',
                            'Read-only. Saving a copy is still available.')
            else:
                self.notice('Режим просмотра. Колесо мыши меняет масштаб.',
                            'View mode. Scroll to zoom.')
        elif mode == 'region':
            self.active_area = self.pending_area = None
            self.clear_selection()
            self.notice('Обведите прямоугольником нужную часть чертежа.',
                        'Drag a rectangle over the area you want to edit.')
        else:
            self.notice('Нажмите на текст. Средняя кнопка мыши перемещает лист.',
                        'Click text. Drag with the middle button to pan.')

    def hit(self, xy, focus_text=False):
        margin = 7 / max(self.view.zoom, .01)
        x, y = xy
        self.query((x-margin, y-margin, x+margin, y+margin), xy, focus_text)

    def area(self, bounds):
        if bounds[2]-bounds[0] < 1e-5 or bounds[3]-bounds[1] < 1e-5:
            return self.hit(bounds[:2])
        self.query(bounds, None)

    def query(self, bounds, xy, focus_text=False):
        if self.busy or not self.sheets or self.read_only or not self.permission_checked:
            return
        if self.current not in self.sheet_loaded:
            self.notice('Подождите, пока загрузятся размеры листа.', 'Wait until the sheet dimensions load.')
            return
        if self.auto_apply and self.has_draft():
            self.resolve_draft(lambda: self.query(bounds, xy, focus_text))
            return
        number = self.current
        if number in self.document.indexes:
            self.display_query(bounds, xy, self.document.indexes[number], focus_text)
            return
        self.pending_area = number, bounds, xy, focus_text
        self.notice('Индексируем текст листа…', 'Indexing text on this sheet…')
        if number not in self.indexing:
            self.indexing.add(number)
            self.bridge.submit(self.editor.executor, self.editor.index,
                lambda task: self.index_ready(number, task), self.document, number)

    def index_ready(self, number, task):
        if self.closed:
            return
        self.indexing.discard(number)
        try:
            index = task.result()
        except Exception as error:
            if number == self.current:
                self.notice(str(error))
                self.pending_area = None
            return
        self.document.indexes[number] = index
        self.document.metrics.index = index.elapsed*1000
        self.document.metrics.objects = index.objects
        if self.pending_area and self.pending_area[0] == number and number == self.current:
            _, bounds, xy, focus_text = self.pending_area
            self.pending_area = None
            self.display_query(bounds, xy, index, focus_text)

    def display_query(self, bounds, xy, index, focus_text=False):
        start = perf_counter()
        self.active_area = bounds if xy is None else None
        runs = {r.id: self.document.get(self.current, r) for r in index.area(bounds)
                if (self.current, r.id) not in self.document.edits}
        for (page, id_), edit in self.document.edits.items():
            if page != self.current or not edit.text:
                continue
            try:
                edit_bounds = edit.bounds()
            except Exception:
                LOG.debug('An existing edit has no usable bounds', exc_info=True)
                continue
            if intersects(edit_bounds, bounds):
                runs[id_] = edit
        self.clear_selection()
        if xy and runs:
            x, y = xy
            try:
                choice = min(runs.values(), key=lambda edit: hit_distance(edit, x, y))
                if focus_text and self.view.mode != 'text':
                    self.window.change_mode('text')
                if self.load_edit(choice) and focus_text and self.form.isEnabled():
                    self.auto_apply = True
                    self.text.setFocus(Qt.FocusReason.MouseFocusReason)
                    self.text.end(False)
            except Exception:
                LOG.exception('Could not select the nearest text object')
                self.clear_selection()
                self.notice('Этот текстовый объект нельзя безопасно редактировать.',
                            'This text object cannot be edited safely.')
        elif runs:
            self.matches.show()
            self.results.update(runs)
            for key, edit in runs.items():
                item = QListWidgetItem(edit.text)
                item.setData(Qt.ItemDataRole.UserRole, key)
                self.matches.addItem(item)
            self.matches.setCurrentRow(0)
            self.select_item(self.matches.item(0))
        else:
            self.view.selection = self.active_area
            self.view.update()
            self.notice('В области нет доступного текста. Кривые и вложенные формы можно просматривать.',
                        'No supported text in this area. Paths and nested forms can be viewed.')
        self.document.metrics.area = len(runs)
        self.document.metrics.ui = (perf_counter()-start)*1000

    def select_item(self, item):
        if item is None:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        if key in self.results:
            self.load_edit(self.results[key])

    def load_edit(self, edit):
        if edit is None:
            return False
        try:
            bounds = edit.bounds()
        except Exception as error:
            LOG.exception('The selected text has no usable bounds')
            self.selected = None
            self.clear_selection()
            self.notice('Этот текстовый объект нельзя безопасно редактировать. ' + str(error),
                        'This text object cannot be edited safely. ' + str(error))
            return False
        self.selected = edit
        self.auto_apply = False
        self.position_draft = (edit.x, edit.y)
        self.drag_origin = None
        self.view.move_bounds = None
        self.view.selection = bounds
        self.view.preview = None
        self.view.update()
        self.edit_color = edit.color
        temporary = [self.text, self.family, self.size, self.angle, self.italic]
        for widget in temporary:
            widget.blockSignals(True)
        try:
            self.text.setMaxLength(max(MAX_TEXT_LENGTH, len(edit.text)))
            self.text.setText(edit.text)
            family_index = self.family.findData(edit.family)
            if family_index < 0:
                self.family.addItem(str(edit.family), edit.family)
                family_index = self.family.findData(edit.family)
            self.family.setCurrentIndex(family_index)
            self.size.setRange(min(.01, float(edit.size)), max(5000., float(edit.size)))
            self.size.setValue(edit.size)
            self.angle.setRange(min(-360., float(edit.angle)), max(360., float(edit.angle)))
            self.angle.setValue(edit.angle)
            try:
                italic = edit.is_italic()
            except Exception:
                LOG.debug('Could not inspect the italic style', exc_info=True)
                italic = False
            self.italic.setChecked(italic)
        finally:
            for widget in temporary:
                widget.blockSignals(False)
        editable = not self.read_only and (not edit.original or edit.original.editable)
        self.view.move_enabled = editable and not self.busy
        self.form.setEnabled(editable and not self.busy)
        self.form.show()
        self.remove_button.setEnabled(editable and bool(edit.text))
        self.color_label()
        self.font_label(edit)
        if self.read_only:
            self.notice('Только просмотр. Сохранение копии доступно.',
                        'Read-only. Saving a copy is still available.')
        elif not editable:
            reason = getattr(edit.original, 'reason', '') if edit.original else ''
            detail = f' {reason}' if reason else ''
            self.notice('Текст доступен для просмотра. Структура объекта ограничивает правку.'+detail,
                        'Text is available for inspection. Its structure restricts editing.'+detail)
        elif edit.original:
            notice = f'{edit.original.font.name} · перетащите рамку'
            english = f'{edit.original.font.name} · drag the selection'
            if 'clip' in str(getattr(edit.original, 'reason', '')).lower():
                notice += ' · текст внутри clipping; перемещённая копия будет на верхнем слое'
                english += ' · text is clipped; the moved copy will be placed on the top layer'
            self.notice(notice, english)
        else:
            self.notice('Введите текст и перетащите его на нужное место.',
                        'Enter text and drag it into place.')
        return True

    def font_label(self, edit):
        try:
            warning = bool(edit.text and edit.family == 'Original' and edit.current_font() != 'Original')
            family = edit.current_font()
            self.italic.setEnabled(not edit.native_italic())
            if edit.native_italic() and not self.italic.isChecked():
                blocked = self.italic.blockSignals(True)
                self.italic.setChecked(True)
                self.italic.blockSignals(blocked)
            self.font_notice.setText(self.lang.get(f'Для этих символов встраивается {family}.',
                f'{family} will be embedded for these characters.') if warning else '')
        except Exception:
            LOG.debug('Could not update the font preview label', exc_info=True)
            self.font_notice.clear()

    def color_label(self):
        try:
            label = QColor.fromRgbF(*self.edit_color).name().upper() if self.edit_color else self.lang.get('Исходный', 'Original')
        except (TypeError, ValueError):
            label = self.lang.get('Исходный', 'Original')
        self.color.setText(label)

    def read_fields(self):
        if self.selected is None:
            return None

        def entered(control, previous):
            return previous if abs(control.value()-previous) < .0005 else control.value()
        x, y = self.position_draft
        edit = replace(self.selected, text=self.text.text(), x=x, y=y,
                       size=entered(self.size, self.selected.size),
                       angle=entered(self.angle, self.selected.angle), color=self.edit_color,
                       family=self.family.currentData())
        try:
            native_italic = edit.native_italic()
        except Exception:
            LOG.debug('Could not inspect native italics', exc_info=True)
            native_italic = False
        try:
            is_italic = edit.is_italic()
        except Exception:
            LOG.debug('Could not inspect the italic style', exc_info=True)
            is_italic = bool(self.italic.isChecked())
        if not native_italic and self.italic.isChecked() != is_italic:
            edit = replace(edit, italic=self.italic.isChecked())
        return edit

    def choose_color(self):
        initial = QColor.fromRgbF(*self.edit_color) if self.edit_color else QColor('black')
        color = QColorDialog.getColor(initial, self, self.lang.get('Цвет текста', 'Text color'))
        if color.isValid():
            self.edit_color = color.redF(), color.greenF(), color.blueF()
            self.color_label()
            self.preview()

    def preview(self, *_):
        if self.selected is None or self.busy or not self.form.isEnabled():
            return
        try:
            edit = self.read_fields()
            if edit is None:
                return
            self.font_label(edit)
            self.view.preview = edit if edit != self.selected else None
            self.remove_button.setEnabled(bool(edit.text))
            if edit.text:
                try:
                    self.view.selection = edit.bounds()
                except (TypeError, ValueError, FileNotFoundError, OSError):
                    self.view.selection = None
            self.view.update()
        except Exception:
            LOG.exception('Could not update the edit preview')
            self.view.preview = None
            self.font_notice.setText(self.lang.get('Предпросмотр недоступен.', 'Preview unavailable.'))
            self.view.update()

    def preview_move(self, delta):
        if not self.selected or self.busy:
            return
        if delta is None:
            self.drag_origin = None
            self.view.move_bounds = None
            self.preview()
            return
        try:
            if self.drag_origin is None:
                self.drag_origin = self.read_fields()
            if self.drag_origin is None:
                return
            dx, dy = delta
            edit = replace(self.drag_origin, x=self.drag_origin.x+dx, y=self.drag_origin.y+dy)
            self.view.preview = edit
            if self.view.selection:
                l, b, r, t = self.view.selection
                self.view.move_bounds = l+dx, b+dy, r+dx, t+dy
            self.view.update()
        except Exception:
            LOG.exception('Could not update the drag preview')
            self.view.preview = None
            self.view.update()

    def finish_move(self, delta):
        try:
            if delta is None or not self.selected or self.busy:
                self.preview_move(None)
                self.finish_text_input()
                return
            self.preview_move(delta)
            edit = self.view.preview
            self.drag_origin = None
            self.view.move_bounds = None
            if not edit or not edit.text:
                return
            self.position_draft = (edit.x, edit.y)
            self.edit(edit)
        finally:
            self.drag_origin = None
            self.view.move_bounds = None

    def add(self):
        if self.has_draft():
            self.resolve_draft(self._add)
            return
        self._add()

    def _add(self):
        if self.closed or self.busy or self.read_only:
            return
        if not self.sheets or not self.permission_checked:
            self.notice('Подождите, пока загрузится документ.', 'Wait until the document is loaded.')
            return
        if self.current not in self.sheet_loaded:
            self.notice('Подождите, пока загрузятся размеры листа.', 'Wait until the sheet dimensions load.')
            return
        self.active_area = self.pending_area = None
        self.matches.clear()
        self.matches.hide()
        self.results.clear()
        center = self.view.visible().center()
        position = self.view.transform().map(center)
        x, y = self.view.document_point(position)
        self.text.setMaxLength(MAX_TEXT_LENGTH)
        try:
            loaded = self.load_edit(Edit(self.current, self.document.allocate(), '', x, y, 12., 0.,
                                         family='ISO 3098'))
        except Exception as error:
            self.window.error(error)
            return
        if not loaded:
            return
        self.view.selection = None
        self.view.update()
        self.remove_button.setEnabled(False)
        self.auto_apply = True
        self.text.setFocus()

    def apply(self):
        if not self.selected or self.busy or not self.form.isEnabled():
            return False
        try:
            edit = self.read_fields()
        except Exception:
            LOG.exception('Could not read the edit fields')
            return False
        if not edit or not edit.text:
            if self.auto_apply and self.selected.text and edit is not None:
                return self.remove()
            return False
        return self.edit(edit)

    def keyPressEvent(self, event):
        if (event.key() == Qt.Key.Key_Delete and not event.isAutoRepeat()
                and event.modifiers() == Qt.KeyboardModifier.NoModifier and self.view.drag is None):
            focus = QApplication.focusWidget()
            if focus is None or isinstance(focus, QPushButton) or not self.form.isAncestorOf(focus):
                if self.remove():
                    event.accept()
                    return
        super().keyPressEvent(event)

    def remove(self):
        if self.selected and not self.busy and self.form.isEnabled():
            if self.selected.original is None and (self.selected.page, self.selected.id) not in self.document.edits:
                self.clear_selection()
                return True
            return self.edit(replace(self.selected, text=''))
        return False

    def edit(self, edit):
        if self.busy or self.closed:
            return False
        state = self.document.edits.copy()
        key = edit.page, edit.id
        if (edit.original and edit == Edit.from_run(edit.page, edit.original)) or (not edit.original and not edit.text):
            state.pop(key, None)
        else:
            state[key] = edit
        if state != self.document.edits:
            self.change_state(state, None, edit if edit.text else None)
            return True
        return False

    def travel(self, step):
        if self.auto_apply and self.has_draft():
            self.resolve_draft(lambda: self.travel(step))
            return
        target = self.document.cursor+step
        if not self.busy and 0 <= target < len(self.document.states):
            self.change_state(self.document.states[target], target, None)

    def _discard(self, path):
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            LOG.debug('Could not remove temporary version %s', path, exc_info=True)

    def change_state(self, state, cursor, selection):
        if self.closed:
            return
        try:
            affected = self.differences(state)
        except Exception as error:
            self.window.error(error)
            return
        self.job_token += 1
        token = self.job_token
        self.set_busy(True)
        self.form.setEnabled(False)
        self.view.move_enabled = False
        self.notice('Применяем локальную правку…', 'Applying local edit…')
        try:
            key, path = self.document.new_version()
        except Exception as error:
            self.set_busy(False)
            self.window.error(error)
            self.update_selection_flags()
            return
        try:
            self.bridge.submit(self.editor.executor, self.editor.save,
                lambda task: self.prepared(task, key, path, state, cursor, affected, selection, token),
                self.document, path, state)
        except Exception as error:
            self._discard(path)
            self.set_busy(False)
            self.window.error(error)
            self.update_selection_flags()

    def differences(self, state):
        dirty = {}
        before = self.document.edits
        for key in before.keys() | state.keys():
            left, right = before.get(key), state.get(key)
            if left == right:
                continue
            original = (left or right).original
            bounds = []
            for edit in (left, right):
                if edit and edit.text:
                    try:
                        bounds.append(edit.bounds())
                    except Exception:
                        LOG.debug('Skipping unavailable edit bounds', exc_info=True)
            if original:
                try:
                    bounds.append(original.bounds)
                except Exception:
                    LOG.debug('Skipping unavailable original bounds', exc_info=True)
            if bounds:
                dirty.setdefault(key[0], []).extend(bounds)
        return dirty

    def prepared(self, task, key, path, state, cursor, affected, selection, token):
        if self.closed or token != self.job_token:
            self._discard(path)
            return
        try:
            elapsed = task.result()
        except Exception as error:
            self._discard(path)
            self.edit_failed(error)
            return
        try:
            self.bridge.submit(self.render.executor, self.render.attach,
                lambda result: self.render_ready(result, key, path, state, cursor, affected,
                                                 selection, elapsed, token), key, path)
        except Exception as error:
            self._discard(path)
            self.edit_failed(error)

    def render_ready(self, task, key, path, state, cursor, affected, selection, elapsed, token=None):
        if self.closed or (token is not None and token != self.job_token):
            self._discard(path)
            return
        try:
            task.result()
        except Exception as error:
            self._discard(path)
            self.edit_failed(error)
            return
        document = self.document
        old = document.key
        try:
            document.key = key
            if cursor is None:
                document.commit(state)
            else:
                document.cursor = cursor
            self.tiles.clear()
            invalidated = 0
            for page, rectangles in affected.items():
                if not 0 <= page < len(self.sheets):
                    continue
                sheet = self.sheets[page]
                try:
                    converted = [box(sheet.to_screen, bounds) for bounds in rectangles]
                    expanded = [(l-2, t-2, r+2, b+2) for l, t, r, b in converted]
                    invalidated += self.view.cache.invalidate(page, expanded)
                except (TypeError, ValueError, ZeroDivisionError, KeyError):
                    LOG.debug('Could not invalidate edited tiles', exc_info=True)
                self.thumb_rev.pop(page, None)
            document.metrics.invalidated = invalidated
            document.metrics.edit = elapsed
            document.write_journal(path)
            self.set_busy(False)
            self.clear_selection()
            show = selection if selection is not None and selection.page == self.current else None
            if show is not None:
                if not self.load_edit(show):
                    self.notice('Правка применена. Выберите текст заново.',
                                'Edit applied. Select the text again.')
            elif selection is not None:
                self.notice('Правка применена. Выбранный текст относится к другому листу.',
                            'Edit applied. The selected text belonged to another sheet.')
            else:
                self.notice('Правка применена. Сохраните копию PDF.', 'Edit applied. Save a PDF copy.')
            self.view.refresh()
            self.window.rename(self)
            try:
                self.bridge.submit(self.render.executor, self.render.close,
                                   lambda _: self.clear_old(old), old)
            except Exception:
                LOG.exception('Could not schedule closing the previous PDF version')
        except Exception as error:
            LOG.exception('Unexpected error after an edit was attached')
            self.edit_failed(error)
        finally:
            self.set_busy(False)
            self.window.update_actions()
            pending = self.pending_action
            self.pending_action = None
            if pending is not None:
                try:
                    pending()
                except Exception:
                    LOG.exception('Could not continue after applying the draft')

    def clear_old(self, key):
        try:
            if ':' in key:
                (Path(self.document.workspace.name) / (key.split(':', 1)[1]+'.pdf')).unlink(missing_ok=True)
        except OSError:
            LOG.debug('Could not remove the previous temporary version', exc_info=True)

    def update_selection_flags(self):
        editable = (self.selected is not None and not self.read_only and not self.busy
                    and (self.selected.original is None or self.selected.original.editable))
        try:
            self.form.setEnabled(editable)
        except RuntimeError:
            pass
        self.view.move_enabled = editable

    def edit_failed(self, error):
        if self.closed:
            return
        try:
            self.set_busy(False)
            self.pending_action = None
            self.update_selection_flags()
            if self.selected:
                self.position_draft = (self.selected.x, self.selected.y)
            self.notice('Правка отклонена. Исходный PDF сохранён.',
                        'Edit blocked. The source PDF was preserved.')
            try:
                self.window.error(error)
            except Exception:
                LOG.exception('Could not show the edit error dialog')
            if self.selected:
                try:
                    self.preview()
                except Exception:
                    LOG.exception('The failed edit preview could not be restored')
                    self.view.preview = None
                    self.view.update()
        finally:
            self.set_busy(False)
            self.window.update_actions()

    def save_state(self):
        state = self.document.edits.copy()
        draft = self.current_draft()
        if draft is not None:
            key = draft.page, draft.id
            if draft.original is None and not draft.text:
                state.pop(key, None)
            else:
                state[key] = draft
        return state, draft

    def _finish_after(self, after, success):
        if after is None:
            return
        try:
            after(success)
        except Exception:
            LOG.exception('Could not finish the save continuation')

    def save(self, after=None):
        if self.closed:
            self._finish_after(after, False)
            return False
        if self.busy:
            self.notice('Дождитесь окончания операции.', 'Wait for the current operation.')
            self._finish_after(after, False)
            return False
        if not self.sheets:
            self.notice('Сначала откройте PDF.', 'Open a PDF before saving.')
            self._finish_after(after, False)
            return False
        settings = getattr(self.window, 'settings', None)
        try:
            last = settings.value('last-directory', str(self.document.source.parent)) if settings else str(self.document.source.parent)
        except Exception:
            last = str(self.document.source.parent)
        if not last:
            last = str(self.document.source.parent)
        default = str(Path(str(last)) / (self.document.source.stem+'_edited.pdf'))
        try:
            selected, _ = QFileDialog.getSaveFileName(self, self.lang.get('Сохранить копию PDF', 'Save PDF copy'),
                                                      default, 'PDF (*.pdf)')
        except Exception as error:
            self.window.error(error)
            self._finish_after(after, False)
            return False
        if not selected:
            self._finish_after(after, False)
            return False
        target = pdf_target(selected)
        try:
            conflict = self.window.destination_conflict(target)
        except Exception as error:
            self.window.error(error)
            self._finish_after(after, False)
            return False
        if conflict:
            self.window.message(conflict)
            self._finish_after(after, False)
            return False
        try:
            if target.exists():
                answer = self.window.ask(
                    self.lang.get('Файл существует', 'File exists'),
                    self.lang.get(f'Файл «{target.name}» уже существует. Заменить его?',
                                  f'“{target.name}” already exists. Replace it?'),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    self._finish_after(after, False)
                    return False
        except Exception as error:
            self.window.error(error)
            self._finish_after(after, False)
            return False
        if settings:
            try:
                settings.setValue('last-directory', str(target.parent))
            except Exception:
                LOG.debug('Could not remember the last save directory', exc_info=True)
        state, draft = self.save_state()
        self.save_token += 1
        token = self.save_token
        self.set_busy(True)
        self.form.setEnabled(False)
        self.view.move_enabled = False
        if self.document.source_changed():
            self.notice('Исходник изменился; экспортируем из внутренней копии…',
                        'Source changed; exporting from the internal snapshot…')
        else:
            self.notice('Сохраняем копию PDF…', 'Saving PDF copy…')
        try:
            self.bridge.submit(self.editor.executor, self.editor.save,
                lambda task: self.saved(target, state, draft, token, after, task),
                self.document, target, state)
        except Exception as error:
            self.set_busy(False)
            self.update_selection_flags()
            self.window.error(error)
            self.notice('Не удалось сохранить копию. Исходный PDF не изменён.',
                        'Saving failed. The source PDF was not changed.')
            self._finish_after(after, False)
            return False
        return True

    def saved(self, target, state, draft, token, after, task):
        success = False
        try:
            if self.closed or token != self.save_token:
                return
            try:
                elapsed = task.result()
            except Exception as error:
                self.notice('Не удалось сохранить копию. Исходный PDF не изменён.',
                            'Saving failed. The source PDF was not changed.')
                self.window.error(error)
                return
            document = self.document
            if state != document.edits:
                document.commit(state)
            if draft is not None:
                key = draft.page, draft.id
                if key in state:
                    self.selected = state[key]
                    self.position_draft = (self.selected.x, self.selected.y)
                    self.view.preview = None
                    try:
                        self.view.selection = self.selected.bounds()
                    except Exception:
                        self.view.selection = None
                else:
                    self.clear_selection()
            document.exported = state
            document.export_name = target.name
            document.metrics.saved = elapsed
            success = True
            try:
                self.window.rename(self)
            except Exception:
                LOG.exception('Could not rename the saved tab')
            try:
                changed = document.source_changed()
                self.notice(
                    f'Копия сохранена: {target.name}' +
                    (' Исходник изменился; экспортирована внутренняя копия.' if changed else ''),
                    f'Copy saved: {target.name}' +
                    (' Source changed; exported from the internal snapshot.' if changed else ''),
                )
            except Exception:
                LOG.exception('Could not show the save notice')
        except Exception as error:
            LOG.exception('Unexpected error after saving a copy')
            self.notice('Не удалось завершить сохранение копии.',
                        'The copy was saved, but the window state could not be updated.')
            self.window.error(error)
        finally:
            if not self.closed and token == self.save_token:
                self.set_busy(False)
                self.update_selection_flags()
                self.window.update_actions()
                QTimer.singleShot(0, self.queue_thumbnails)
                self._finish_after(after, success)

    def close_workspace(self):
        if self.closed:
            return
        self.closed = True
        self.document.disposed = True
        self.pending_action = None
        if self.tile_job:
            self.tile_job.cancel()
        for future in self.thumb_jobs.values():
            future.cancel()
        self.thumb_jobs.clear()
        self.thumb_icons.clear()
        key = self.document.key
        try:
            self.bridge.submit(self.render.executor, self.render.close,
                               lambda _: self.document.cleanup(), key)
        except Exception:
            LOG.exception('Could not schedule workspace cleanup')
            self.document.cleanup()


def hit_distance(edit, x, y):
    left, bottom, right, top = edit.bounds()
    outside = hypot(max(left-x, x-right, 0.), max(bottom-y, y-top, 0.))
    angle = radians(edit.angle)
    ux, uy = cos(angle), sin(angle)
    dx, dy = x-edit.x, y-edit.y
    baseline = abs(dx*uy - dy*ux)
    if edit.original and edit.text == edit.original.text and edit.keeps_placement():
        last = edit.original.glyphs[-1]
        end_x, end_y = point(compose(last.ctm, last.matrix), last.advance, 0.)
        length = (end_x-edit.x)*ux + (end_y-edit.y)*uy
        along = dx*ux + dy*uy
        baseline = hypot(baseline, max(-along, along-length, 0.))
    return outside, baseline


def intersects(a, b):
    return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]


def format_name(sheet):
    w, h = sorted(round(p*25.4/72) for p in (sheet.width, sheet.height))
    for label, (short, long) in {'A0': (841, 1189), 'A1': (594, 841), 'A2': (420, 594),
                                'A3': (297, 420), 'A4': (210, 297)}.items():
        if abs(w-short) <= 2 and abs(h-long) <= 2:
            return label
    return f'{w} × {h} mm'


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        app = QApplication.instance()
        if app is not None:
            app.setApplicationName('VECTOR PDF')
            app.setApplicationVersion(VERSION)
        self.setWindowTitle('Vector')
        self.resize(1320, 820)
        self.setMinimumSize(780, 480)
        self.setAcceptDrops(True)
        self.settings = QSettings('VectorPDF', 'VectorPDF')
        self.safe_mode = safe_mode()
        self.lang = Language(self.settings.value('language', 'ru'))
        self.appearance = self.settings.value('appearance', 'dark' if self.settings.value('dark', True, type=bool) else 'light')
        if self.appearance not in THEMES:
            self.appearance = 'dark'
        self.surface = THEMES[self.appearance][-1]
        self.setWindowIcon(QIcon(str(APP_ICON)))
        self.bridge = Bridge(self)
        self.render = RenderEngine()
        self.editor = LocalEditor()
        self.actions = {}
        self.mode = 'view'
        self.updating = False
        self.pending_update = None
        self.restart_requested = False
        self._active_index = None
        self._active_tab = None
        self._suppress_draft = False
        self._pending_quit = False
        self._allow_close = False
        self._quit_queue = []
        self._quit_position = 0
        self._quit_tabs = []

        root = QWidget()
        vertical = QVBoxLayout(root)
        vertical.setContentsMargins(0, 0, 0, 0)
        vertical.setSpacing(0)
        self.setCentralWidget(root)
        header = Toolbar()
        row = header.files
        mark = QLabel()
        mark.setPixmap(self.windowIcon().pixmap(QSize(29, 29)))
        row.addWidget(mark)
        brand = QLabel('VECTOR', objectName='brand')
        row.addWidget(brand)
        row.addSpacing(13)
        for name, ru, en, slot, shortcuts in [
            ('open', 'Открыть', 'Open', self.open_dialog, 'Ctrl+O'),
            ('save', 'Сохранить копию', 'Save copy', lambda: self.call('save'), 'Ctrl+Shift+S'),
            ('undo', '↶', '↶', lambda: self.call('travel', -1), 'Ctrl+Z'),
            ('redo', '↷', '↷', lambda: self.call('travel', 1), 'Ctrl+Y'),
        ]:
            self.button(row, name, ru, en, slot, shortcuts)
        self.lang.bind(self.actions['undo'].setToolTip, 'Отменить', 'Undo')
        self.lang.bind(self.actions['redo'].setToolTip, 'Повторить', 'Redo')
        row.addStretch()
        row = header.tools
        self.mode_group = QActionGroup(self)
        self.mode_group.setExclusive(True)
        for name, ru, en in [('view', 'Просмотр', 'View'), ('text', 'Текст', 'Text'), ('region', 'Область', 'Area')]:
            action = self.button(row, name, ru, en, lambda mode=name: self.change_mode(mode))
            action.setCheckable(True)
            self.mode_group.addAction(action)
        self.actions['view'].setChecked(True)
        self.button(row, 'add', '+ Текст', '+ Text', self.add)
        row.addStretch()
        for name, ru, en, fn in [
            ('minus', '−', '−', lambda: self.canvas('zoom_at', 1/1.24)),
            ('plus', '+', '+', lambda: self.canvas('zoom_at', 1.24)),
            ('fit', 'По листу', 'Fit sheet', lambda: self.canvas('fit')),
            ('rotate', '↻', '↻', lambda: self.canvas('rotate')),
        ]:
            self.button(row, name, ru, en, fn)
        self.lang.bind(self.actions['rotate'].setToolTip, 'Повернуть вид на 90°', 'Rotate the view by 90°')
        vertical.addWidget(header)
        vertical.addWidget(Ribbon())

        self.tabs = QTabWidget(tabsClosable=True, movable=True)
        self.tabs.currentChanged.connect(self.current_changed)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        vertical.addWidget(self.tabs, 1)

        self.empty = QWidget(objectName='drawing')
        blank = QVBoxLayout(self.empty)
        blank.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        blank.setSpacing(13)
        heading = QLabel(objectName='emptyTitle', alignment=Qt.AlignmentFlag.AlignCenter)
        self.lang.bind(heading.setText, 'Откройте чертёж PDF', 'Open a PDF drawing')
        subtitle = QLabel(objectName='subhead', alignment=Qt.AlignmentFlag.AlignCenter)
        self.lang.bind(subtitle.setText, 'Перетащите файл сюда или выберите его на диске.',
                       'Drop a file here or choose it from disk.')
        open_button = QPushButton(objectName='primary')
        self.lang.bind(open_button.setText, 'Открыть PDF', 'Open PDF')
        open_button.clicked.connect(self.open_dialog)
        blank.addWidget(heading)
        blank.addWidget(subtitle)
        blank.addWidget(open_button, 0, Qt.AlignmentFlag.AlignHCenter)
        vertical.addWidget(self.empty, 1)

        status = QWidget(objectName='status')
        bar = QHBoxLayout(status)
        bar.setContentsMargins(13, 5, 13, 6)
        self.status_sheet, self.status_message, self.status_view, self.status_coord = QLabel(), QLabel(), QLabel(), QLabel()
        self.status_version = QLabel(f'v{VERSION}')
        self.status_message.setObjectName('panelNote')
        self.status_version.setObjectName('panelNote')
        for label in (self.status_sheet, self.status_message, self.status_view,
                      self.status_coord, self.status_version):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setOpenExternalLinks(False)
        bar.addWidget(self.status_sheet)
        bar.addWidget(self.status_message, 1)
        bar.addWidget(self.status_view)
        bar.addSpacing(15)
        bar.addWidget(self.status_coord)
        bar.addSpacing(15)
        bar.addWidget(self.status_version)
        vertical.addWidget(status)
        self.build_menu()
        self.update_menu()
        self.make_diagnostics()
        self.apply_theme()
        self.current_changed()
        if sys.platform == 'win32' and not self.safe_mode:
            QTimer.singleShot(1700, self.start_automatic_update)
        if not os.environ.get('VECTORPDF_NO_RECOVERY'):
            QTimer.singleShot(350, self.start_recovery)

    def start_automatic_update(self):
        try:
            if self.auto_update.isChecked():
                self.check_updates(False)
        except RuntimeError:
            pass

    def button(self, layout, name, ru, en, slot, shortcut=None):
        action = QAction(self)
        self.lang.bind(action.setText, ru, en)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(lambda _=False: slot())
        self.addAction(action)
        self.actions[name] = action
        button = QToolButton()
        button.setDefaultAction(action)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        if name == 'open':
            button.setObjectName('primary')
        layout.addWidget(button)
        return action

    def build_menu(self):
        file = self.menuBar().addMenu('')
        self.lang.bind(file.setTitle, 'Файл', 'File')
        file.addAction(self.actions['open'])
        file.addAction(self.actions['save'])
        reload_source = file.addAction('')
        self.lang.bind(reload_source.setText, 'Перечитать исходник', 'Reload source')
        reload_source.setShortcut(QKeySequence('F5'))
        reload_source.triggered.connect(self.reload_source)
        font = file.addAction('')
        self.lang.bind(font.setText, 'Добавить шрифт…', 'Import font…')
        font.triggered.connect(self.import_font)
        close = file.addAction('')
        self.lang.bind(close.setText, 'Закрыть вкладку', 'Close tab')
        close.setShortcut(QKeySequence('Ctrl+W'))
        close.triggered.connect(lambda: self.close_tab(self.tabs.currentIndex()))
        view = self.menuBar().addMenu('')
        self.lang.bind(view.setTitle, 'Вид', 'View')
        self.show_thumbs = view.addAction('')
        self.lang.bind(self.show_thumbs.setText, 'Миниатюры', 'Thumbnails')
        self.show_thumbs.setCheckable(True)
        self.show_thumbs.setChecked(True)
        self.show_thumbs.triggered.connect(self.toggle_thumbs)
        themes = view.addMenu('')
        self.lang.bind(themes.setTitle, 'Тема', 'Theme')
        self.theme_group = QActionGroup(self)
        self.theme_group.setExclusive(True)
        for key, ru, en in (('dark', 'Тёмная', 'Dark'), ('light', 'Светлая', 'Light'),
                            ('solar', 'Солнечная Orbita', 'Orbita Solar')):
            action = themes.addAction('')
            self.lang.bind(action.setText, ru, en)
            action.setCheckable(True)
            self.theme_group.addAction(action)
            action.setChecked(key == self.appearance)
            action.triggered.connect(lambda _checked, key=key: self.theme(key))
        lang = self.menuBar().addMenu('')
        self.lang.bind(lang.setTitle, 'Язык', 'Language')
        lang.addAction('Русский', lambda: self.language('ru'))
        lang.addAction('English', lambda: self.language('en'))
        help_menu = self.menuBar().addMenu('')
        self.lang.bind(help_menu.setTitle, 'Справка', 'Help')
        about = help_menu.addAction('')
        self.lang.bind(about.setText, 'О программе', 'About')
        about.triggered.connect(self.about)
        licenses = help_menu.addAction('')
        self.lang.bind(licenses.setText, 'Сторонние лицензии', 'Third-party licenses')
        licenses.triggered.connect(self.show_licenses)
        logs = help_menu.addAction('')
        self.lang.bind(logs.setText, 'Открыть папку журналов', 'Open log folder')
        logs.triggered.connect(self.open_logs)
        self.view_menu = view

    def update_menu(self):
        menu = self.menuBar().addMenu('')
        self.lang.bind(menu.setTitle, 'Обновления', 'Updates')
        self.update_menu_widget = menu
        check = menu.addAction('')
        self.lang.bind(check.setText, 'Проверить обновления', 'Check for updates')
        check.triggered.connect(lambda: self.check_updates(True))
        self.check_update_action = check
        self.auto_update = menu.addAction('')
        self.lang.bind(self.auto_update.setText, 'Загружать автоматически', 'Download automatically')
        self.auto_update.setCheckable(True)
        self.auto_update.setChecked(self.settings.value('auto-update', False, type=bool))
        self.auto_update.toggled.connect(lambda flag: self.settings.setValue('auto-update', flag))
        self.restart_update = menu.addAction('')
        self.lang.bind(self.restart_update.setText, 'Перезапустить и обновить', 'Restart and update')
        self.restart_update.setEnabled(False)
        self.restart_update.triggered.connect(self.restart_for_update)
        if self.safe_mode:
            check.setEnabled(False)
            self.auto_update.setEnabled(False)
            self.restart_update.setEnabled(False)
            notice = menu.addAction('')
            self.lang.bind(notice.setText, 'Безопасный режим: обновления отключены',
                           'Safe mode: updates are disabled')
            notice.setEnabled(False)
        else:
            menu.setEnabled(sys.platform == 'win32')

    def check_updates(self, manual):
        if self.safe_mode or self.updating or self.pending_update or sys.platform != 'win32':
            return
        self.updating = True
        if manual:
            self.flash(self.lang.get('Проверяем обновления…', 'Checking for updates…'), 30000)
        def work():
            result = Future()
            try:
                if release := updater.check():
                    result.set_result(updater.arm(release))
                else:
                    result.set_result(None)
            except Exception as error:
                LOG.warning('Update check failed', exc_info=True)
                result.set_exception(error)
            try:
                self.bridge.complete.emit(lambda future: self.update_ready(future, manual), result)
            except RuntimeError:
                pass
        Thread(target=work, name='vector-update', daemon=True).start()

    def update_ready(self, future, manual):
        self.updating = False
        try:
            self.pending_update = future.result()
        except Exception as error:
            self.flash('')
            if manual:
                self.error(error)
            return
        if self.pending_update:
            self.restart_update.setEnabled(True)
            message = self.lang.get(
                f'Обновление {self.pending_update} готово. Оно установится при следующем запуске.',
                f'Update {self.pending_update} is ready. It will start on the next launch.')
            if manual:
                self.message(message, icon=QMessageBox.Icon.Information)
            else:
                self.flash(message, 10000)
        elif manual:
            self.message(self.lang.get('Установлена последняя версия.', 'You are on the latest version.'),
                         icon=QMessageBox.Icon.Information)

    def restart_for_update(self):
        if self.pending_update:
            self.restart_requested = True
            self.close()

    def flash(self, text, timeout=8000):
        self.status_message.setText(text)

        def clear():
            try:
                if self.status_message.text() == text:
                    self.status_message.setText('')
            except RuntimeError:
                pass

        QTimer.singleShot(timeout, clear)

    def make_diagnostics(self):
        self.dock = QDockWidget()
        self.lang.bind(self.dock.setWindowTitle, 'Диагностика', 'Diagnostics')
        self.diagnostics = QLabel()
        self.diagnostics.setContentsMargins(12, 10, 12, 12)
        self.diagnostics.setTextFormat(Qt.TextFormat.PlainText)
        self.diagnostics.setOpenExternalLinks(False)
        self.diagnostics.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.dock.setWidget(self.diagnostics)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock)
        action = self.dock.toggleViewAction()
        self.lang.bind(action.setText, 'Диагностика', 'Diagnostics')
        self.view_menu.addAction(action)
        self.dock.hide()
        timer = QTimer(self)
        timer.setInterval(750)
        timer.timeout.connect(self.show_metrics)
        timer.start()
        self.sample_timer = timer

    def show_metrics(self):
        tab = self.current_tab()
        if not self.dock.isVisible() or not tab:
            return
        m, canvas = tab.document.metrics, tab.view
        cache = canvas.cache
        hits = cache.hits + cache.misses
        try:
            import psutil
            rss = f'{psutil.Process().memory_info().rss/1048576:.0f} MiB'
        except ImportError:
            rss = '—'
        pairs = [
            ('Открытие / индекс', 'Open / index', f'{m.opened:.1f} / {m.index:.1f} ms'),
            ('Текстовых операций / в области', 'Text operations / in area', f'{m.objects} / {m.area}'),
            ('Отрисовка PDF / кадр UI', 'PDF render / UI paint', f'{m.render:.1f} / {canvas.ui_ms:.1f} ms'),
            ('Частота отрисовки / выбор', 'Paint rate / selection', f'{canvas.fps:.1f} FPS / {m.ui:.1f} ms'),
            ('Правка / запись копии', 'Edit / save copy', f'{m.edit:.1f} / {m.saved:.1f} ms'),
            ('Кеш / попадания', 'Cache / hits', f'{cache.bytes/1048576:.1f} MiB / {cache.hits/max(1, hits):.0%}'),
            ('Обновлено тайлов / память процесса', 'Invalidated tiles / process RAM', f'{m.invalidated} / {rss}'),
        ]
        self.diagnostics.setText('\n'.join(f'{self.lang.get(ru,en)}    {value}' for ru,en,value in pairs))

    def theme(self, value):
        self.appearance = value
        self.settings.setValue('appearance', value)
        self.apply_theme()

    def apply_theme(self):
        base, panel, raised, _, text, muted, select, self.surface = THEMES[self.appearance]
        palette = self.palette()
        roles = QPalette.ColorRole
        for role, color in ((roles.Window, panel), (roles.Base, raised), (roles.AlternateBase, base),
                            (roles.Button, raised), (roles.Text, text), (roles.WindowText, text),
                            (roles.ButtonText, text), (roles.Highlight, select),
                            (roles.HighlightedText, text), (roles.PlaceholderText, muted)):
            palette.setColor(role, QColor(color))
        self.setPalette(palette)
        self.setStyleSheet(stylesheet(self.appearance))
        self.empty.setStyleSheet('QWidget#drawing { background: '+self.surface+'; }')
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if tab is None:
                continue
            canvas = tab.view
            canvas.surface = self.surface
            canvas.update()

    def language(self, code):
        self.lang.change(code)
        self.settings.setValue('language', code)
        tab = self.current_tab()
        if tab and not tab.busy and not tab.has_draft():
            tab.set_mode(self.mode, prompt=False)
        self.update_status()
        self.show_metrics()

    def current_tab(self):
        return self.tabs.currentWidget()

    def call(self, name, *args):
        if tab := self.current_tab():
            return getattr(tab, name)(*args)
        return None

    def canvas(self, name, *args):
        if tab := self.current_tab():
            return getattr(tab.view, name)(*args)
        return None

    def sync_mode(self, tab=None):
        tab = tab or self.current_tab()
        if tab is None:
            return
        mode = self.mode
        if tab.read_only or not tab.sheets:
            mode = 'view'
        if tab.has_draft() and mode in ('view', 'region') and mode != tab.view.mode:
            actual = self.actions.get(tab.view.mode)
            if actual is not None:
                actual.setChecked(True)
            return
        action = self.actions.get(mode)
        if action is not None:
            action.setChecked(True)
        if not tab.sheets:
            tab.view.set_mode('view')
            return
        try:
            tab.set_mode(mode, prompt=False)
        except RuntimeError:
            pass

    def _set_global_mode(self, mode):
        self.mode = mode
        action = self.actions.get(mode)
        if action is not None:
            action.setChecked(True)

    def change_mode(self, mode):
        tab = self.current_tab()
        if tab and tab.busy:
            self.flash(self.lang.get('Дождитесь окончания операции.', 'Wait for the current operation.'))
            return
        if tab and tab.read_only:
            self.mode = mode
            self.actions['view'].setChecked(True)
            tab.set_mode('view')
            return
        if (tab and mode in ('view', 'region') and mode != tab.view.mode
                and tab.has_draft()):
            def apply_mode():
                self._set_global_mode(mode)
                tab.set_mode(mode, prompt=False)
            tab.resolve_draft(apply_mode)
            actual = self.actions.get(tab.view.mode)
            if actual is not None:
                actual.setChecked(True)
            return
        self._set_global_mode(mode)
        if tab:
            tab.set_mode(mode, prompt=False)
        self.update_actions()

    def add(self):
        tab = self.current_tab()
        if tab is None or tab.busy or tab.read_only:
            return
        self.change_mode('text')
        tab.add()

    def toggle_thumbs(self, state):
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if tab is not None:
                tab.left.setVisible(state)
        if state:
            self.call('queue_thumbnails')

    def update_actions(self):
        tab = self.current_tab()
        ready = bool(tab and tab.sheets)
        idle = bool(ready and not tab.busy)
        editing = bool(idle and tab.permission_checked and not tab.read_only)
        save_ok = bool(idle and (not tab.read_only or not tab.document.edits))
        for key in ('view', 'fit', 'plus', 'minus', 'rotate'):
            self.actions[key].setEnabled(ready)
        for key in ('text', 'region'):
            self.actions[key].setEnabled(editing)
        self.actions['save'].setEnabled(save_ok)
        self.actions['add'].setEnabled(editing)
        self.actions['undo'].setEnabled(editing and tab.document.cursor > 0)
        self.actions['redo'].setEnabled(editing and tab.document.cursor+1 < len(tab.document.states))
        if tab is not None:
            try:
                tab.pages.setEnabled(not tab.busy)
            except RuntimeError:
                pass

    def trim_inactive(self):
        active = self.current_tab()
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if tab is None or tab is active:
                continue
            try:
                tab.view.cache.trim(INACTIVE_TILE_BUDGET)
            except (RuntimeError, AttributeError):
                pass

    def current_changed(self, *_):
        tab = self.current_tab()
        previous = getattr(self, '_active_tab', None)
        if (not self._suppress_draft and previous is not None and previous is not tab
                and self.tabs.indexOf(previous) >= 0 and not previous.closed and not previous.busy
                and previous.has_draft()):
            if previous.auto_apply:
                previous.apply()
            elif not self.confirm_tab_draft(previous):
                return
        self._active_tab = tab

        has = bool(self.tabs.count())
        self.empty.setVisible(not has)
        self.tabs.setVisible(has)
        if tab:
            if tab.sheets:
                tab.view.refresh()
            self.sync_mode(tab)
            try:
                tab.queue_thumbnails()
            except RuntimeError:
                pass
        self.trim_inactive()
        self.update_actions()
        self.update_status()

    def confirm_tab_draft(self, previous):
        answer = self.ask(
            self.lang.get('Неприменённый текст', 'Unapplied text'),
            self.lang.get('Применить текущий текст перед переключением вкладки?',
                          'Apply the current text before switching tabs?'),
            QMessageBox.StandardButton.Apply | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Apply:
            previous.apply()
        elif answer == QMessageBox.StandardButton.Discard:
            previous.clear_selection()
        else:
            self._suppress_draft = True
            try:
                self.tabs.setCurrentWidget(previous)
            finally:
                self._suppress_draft = False
            return False
        return True

    def update_status(self):
        self.update_actions()
        tab = self.current_tab()
        sheet = tab.view.sheet if tab else None
        if not sheet:
            self.status_sheet.setText(self.lang.get('PDF локально', 'Local PDF'))
            self.status_view.clear()
            self.status_coord.clear()
            return
        loaded = tab.current in tab.sheet_loaded
        name = format_name(sheet) if loaded else ''
        page = self.lang.get('Лист', 'Sheet')
        text = f'{page} {tab.current+1} / {len(tab.sheets)}'
        if name:
            text += f'  ·  {name}'
        self.status_sheet.setText(text)
        self.status_view.setText(f'{tab.view.zoom:.0%}  ·  {(tab.view.rotation+sheet.rotation)%360}°')
        if tab.xy and loaded and all(isfinite(v) for v in tab.xy):
            x, y = (v*25.4/72 for v in tab.xy)
            self.status_coord.setText(f'X {x:.1f}    Y {y:.1f} mm')
        else:
            self.status_coord.clear()

    def import_font(self):
        path, _ = QFileDialog.getOpenFileName(self, self.lang.get('Добавить шрифт', 'Import font'),
                                              '', 'TrueType (*.ttf)')
        if not path:
            return
        try:
            key = import_font(path)
            QFontDatabase.addApplicationFont(path)
        except Exception as error:
            self.error(error)
            return
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if tab is None:
                continue
            choice = tab.family
            if choice.findData(key) < 0:
                choice.addItem(key, key)
        if tab := self.current_tab():
            tab.family.setCurrentIndex(tab.family.findData(key))
        self.flash(self.lang.get('Добавлен шрифт ', 'Font imported ')+Path(path).stem)

    def open_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(self, self.lang.get('Открыть PDF', 'Open PDF'),
                                                '', 'PDF (*.pdf)')
        for path in paths:
            self.open_file(path)

    def samefile(self, left, right):
        return same_path(left, right)

    def destination_conflict(self, target):
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if tab is None:
                continue
            if same_path(target, tab.document.source):
                return self.lang.get(
                    f'Файл «{target.name}» уже открыт в другой вкладке. Выберите другое имя или закройте эту вкладку.',
                    f'“{target.name}” is open in another tab. Choose another name or close that tab.')
        return None

    def open_file(self, path):
        if self._pending_quit:
            return
        document = None
        workspace = None
        try:
            resolved = Path(path).resolve()
            for i in range(self.tabs.count()):
                tab = self.tabs.widget(i)
                if tab is not None and same_path(tab.document.source, resolved):
                    self.tabs.setCurrentIndex(i)
                    return
            stamp = file_stamp(resolved)
        except Exception as error:
            self.error(error)
            return
        try:
            document = Document(resolved)
            document.stamp = stamp
            workspace = Workspace(self, document)
            index = self.tabs.addTab(workspace, resolved.name)
            workspace.left.setVisible(self.show_thumbs.isChecked())
            self.tabs.setTabToolTip(index, str(resolved))
            self.tabs.setCurrentIndex(index)
            if not workspace.start():
                self.finish_close(workspace)
                return
            self.current_changed()
        except Exception as error:
            LOG.exception('Could not open %s', resolved)
            if workspace is not None:
                self.finish_close(workspace)
            elif document is not None:
                document.cleanup()
            self.error(error)

    def reload_source(self):
        tab = self.current_tab()
        if tab is None:
            return
        if tab.busy:
            self.message(self.lang.get('Дождитесь окончания операции.', 'Wait for the current operation.'),
                         icon=QMessageBox.Icon.Information)
            return
        source = tab.document.source
        try:
            file_stamp(source)
        except OSError as error:
            self.error(error)
            return
        if tab.has_draft() or tab.document.modified:
            answer = self.ask(
                self.lang.get('Перечитать исходник', 'Reload source'),
                self.lang.get('Отбросить текущие правки и перечитать файл с диска?',
                              'Discard the current edits and reload the file from disk?'),
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Discard:
                return
        self.finish_close(tab)
        self.open_file(source)

    def rename(self, workspace):
        index = self.tabs.indexOf(workspace)
        if index < 0:
            return
        name = workspace.document.export_name or workspace.document.source.name
        self.tabs.setTabText(index, name + (' *' if workspace.document.modified else ''))

    def message(self, text, title=None, icon=QMessageBox.Icon.Warning):
        box = QMessageBox(self)
        box.setWindowTitle(title or 'VECTOR PDF')
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(str(text))
        box.setIcon(icon)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()

    def ask(self, title, text, buttons, default):
        box = QMessageBox(self)
        box.setWindowTitle(title or 'VECTOR PDF')
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(str(text))
        box.setIcon(QMessageBox.Icon.Question)
        box.setStandardButtons(buttons)
        box.setDefaultButton(default)
        try:
            return QMessageBox.StandardButton(box.exec())
        except Exception:
            LOG.exception('Could not show a confirmation dialog')
            return default

    def error(self, error, title=None):
        LOG.warning('UI operation failed: %s', error, exc_info=True)
        self.message(friendly_error(error), title or self.lang.get(
            'VECTOR PDF · Операция прервана', 'VECTOR PDF · Operation stopped'))

    def about(self):
        box = QMessageBox(self)
        box.setWindowTitle(self.lang.get('О программе VECTOR PDF', 'About VECTOR PDF'))
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(about_text())
        box.setIcon(QMessageBox.Icon.Information)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()

    def show_licenses(self):
        path = Path(__file__).resolve().parent.parent / 'THIRD_PARTY.md'
        if path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            self.message(self.lang.get('Файл THIRD_PARTY.md не найден рядом с приложением.',
                                       'THIRD_PARTY.md was not found next to the application.'),
                         icon=QMessageBox.Icon.Information)

    def open_logs(self):
        try:
            directory = log_directory()
            directory.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))
        except OSError as error:
            self.error(error)

    def start_recovery(self):
        if getattr(self, '_recovery_started', False):
            return
        self._recovery_started = True

        def work():
            result = Future()
            try:
                cleanup_workspaces()
                cleanup_temp_files()
                try:
                    last = self.settings.value('last-directory')
                except Exception:
                    last = None
                if last:
                    cleanup_temp_files(Path(str(last)))
                result.set_result(scan_recovery())
            except Exception as error:
                LOG.warning('Recovery scan failed', exc_info=True)
                result.set_exception(error)
            try:
                self.bridge.complete.emit(self.recovery_ready, result)
            except RuntimeError:
                pass

        Thread(target=work, name='vector-recovery', daemon=True).start()

    def recovery_ready(self, future):
        try:
            candidates = future.result()
        except Exception:
            return
        if not candidates or self._allow_close:
            return
        lines = []
        for candidate in candidates[:6]:
            lines.append(f'{candidate.source.name}  →  {candidate.version.name}')
        if len(candidates) > 6:
            lines.append(f'… +{len(candidates)-6}')
        text = self.lang.get(
            'Найдены версии после аварийного закрытия. Открыть их?\n\n',
            'Recoverable versions were found after an unclean close. Open them?\n\n') + '\n'.join(lines)
        answer = self.ask(self.lang.get('Восстановление', 'Recovery'), text,
                          QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Discard,
                          QMessageBox.StandardButton.Discard)
        if answer == QMessageBox.StandardButton.Open:
            for candidate in candidates:
                self.open_file(candidate.version)
        elif answer == QMessageBox.StandardButton.Discard:
            import shutil
            for candidate in candidates:
                shutil.rmtree(candidate.workspace, ignore_errors=True)

    def confirm(self, workspace):
        if workspace is None:
            return 'discard'
        if workspace.busy:
            self.message(self.lang.get(
                'Вкладка ещё применяет правку. Дождитесь окончания операции и закройте её снова.',
                'This tab is still applying an edit. Wait for it to finish, then close it again.'),
                icon=QMessageBox.Icon.Information)
            return 'cancel'
        if not workspace.has_draft() and not workspace.document.modified:
            return 'discard'
        name = workspace.document.export_name or workspace.document.source.name
        answer = self.ask(
            self.lang.get('Несохранённые правки', 'Unsaved edits'),
            self.lang.get(f'Сохранить копию PDF «{name}» перед закрытием вкладки?',
                          f'Save a PDF copy of “{name}” before closing this tab?'),
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Save:
            return 'save'
        if answer == QMessageBox.StandardButton.Discard:
            return 'discard'
        return 'cancel'

    def finish_close(self, workspace):
        if workspace is None:
            return
        index = self.tabs.indexOf(workspace)
        if index < 0:
            try:
                workspace.close_workspace()
            finally:
                workspace.deleteLater()
            return
        self._suppress_draft = True
        try:
            workspace.close_workspace()
            self.tabs.removeTab(index)
            workspace.deleteLater()
            if getattr(self, '_active_tab', None) is workspace:
                self._active_tab = None
        finally:
            self._suppress_draft = False
        self.current_changed()

    def close_tab(self, index):
        if index < 0 or index >= self.tabs.count():
            return
        tab = self.tabs.widget(index)
        if tab is None:
            return
        action = self.confirm(tab)
        if action == 'cancel':
            return
        if action == 'save':
            try:
                tab.save(after=lambda ok, tab=tab: self.finish_close(tab) if ok else None)
            except Exception as error:
                LOG.exception('Could not start saving a tab before closing')
                self.error(error)
            return
        self.finish_close(tab)

    def _cleanup_tabs(self, tabs):
        for tab in tabs:
            if tab is None:
                continue
            try:
                tab.close_workspace()
            except Exception:
                LOG.exception('Could not clean up a tab while closing')

    def _save_quit_next(self):
        if not self._pending_quit:
            return
        if self._quit_position >= len(self._quit_queue):
            self._pending_quit = False
            self._allow_close = True
            self.close()
            return
        tab = self._quit_queue[self._quit_position]
        if tab is None or tab.closed:
            self._quit_position += 1
            QTimer.singleShot(0, self._save_quit_next)
            return

        def done(ok):
            if not self._pending_quit:
                return
            if not ok:
                self._pending_quit = False
                self.restart_requested = False
                return
            self._quit_position += 1
            QTimer.singleShot(0, self._save_quit_next)

        try:
            tab.save(after=done)
        except Exception as error:
            LOG.exception('Could not start saving a tab before quitting')
            done(False)
            self.error(error)

    def closeEvent(self, event):
        tabs = [self.tabs.widget(i) for i in range(self.tabs.count())]
        if self._allow_close:
            self._cleanup_tabs(tabs)
            super().closeEvent(event)
            return
        if self._pending_quit:
            event.ignore()
            return
        busy = [tab for tab in tabs if tab is not None and tab.busy]
        if busy:
            self.message(self.lang.get(
                'Одна из вкладок ещё применяет правку. Дождитесь окончания операции и закройте приложение снова.',
                'A tab is still applying an edit. Wait for it to finish, then close the application again.'),
                icon=QMessageBox.Icon.Information)
            self.restart_requested = False
            event.ignore()
            return
        needs = [tab for tab in tabs if tab is not None
                 and (tab.has_draft() or tab.document.modified)]
        if not needs:
            self._cleanup_tabs(tabs)
            super().closeEvent(event)
            return
        lines = '\n'.join(f'• {tab.document.source.name}'
                          + (self.lang.get(' (неприменённый текст)', ' (unapplied text)')
                             if tab.has_draft() else '') for tab in needs[:8])
        if len(needs) > 8:
            lines += f'\n… +{len(needs)-8}'
        answer = self.ask(
            self.lang.get('Несохранённые правки', 'Unsaved edits'),
            self.lang.get(f'Перед выходом сохранить копии для {len(needs)} вкладок?\n\n',
                          f'Save copies for {len(needs)} tabs before quitting?\n\n') + lines,
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Discard:
            self._cleanup_tabs(tabs)
            super().closeEvent(event)
            return
        if answer != QMessageBox.StandardButton.Save:
            self.restart_requested = False
            event.ignore()
            return
        event.ignore()
        self._pending_quit = True
        self._quit_tabs = tabs
        self._quit_queue = list(needs)
        self._quit_position = 0
        QTimer.singleShot(0, self._save_quit_next)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(url.toLocalFile().lower().endswith('.pdf') for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            if (path := url.toLocalFile()).lower().endswith('.pdf'):
                self.open_file(path)
        event.acceptProposedAction()


def configure_fonts(app):
    for filename in ('RussoOne-Regular.ttf', 'Exo2-Variable.ttf', 'osifont.ttf',
                     'DejaVuSans.ttf', 'DejaVuSerif.ttf', 'DejaVuSansMono.ttf'):
        QFontDatabase.addApplicationFont(str(ASSETS / filename))
    ui_font = QFont()
    ui_font.setFamily('Exo 2')
    ui_font.setWeight(QFont.Weight.Normal)
    ui_font.setPointSize(10)
    app.setFont(ui_font)


def register_fallback_fonts():
    for path in FALLBACKS.values():
        try:
            if isinstance(path, (str, os.PathLike)) and Path(path).is_file():
                QFontDatabase.addApplicationFont(str(path))
        except (OSError, RuntimeError, TypeError):
            LOG.debug('Could not register %s with Qt', path, exc_info=True)


def main(ready_token=None):
    try:
        setup_logging()
        install_exception_hooks()
    except Exception:
        pass
    app = QApplication(sys.argv)
    app.setApplicationName('VECTOR PDF')
    app.setApplicationVersion(VERSION)
    app.setStyle('Fusion')
    configure_fonts(app)
    try:
        learn_fonts()
    except Exception:
        LOG.exception('Could not load imported fonts')
    register_fallback_fonts()
    window = MainWindow()
    window.show()
    for path in sys.argv[1:]:
        window.open_file(path)
    if ready_token:
        try:
            write_ready_token(ready_token)
        except OSError:
            LOG.warning('Could not write the updater readiness token', exc_info=True)
    exit_code = app.exec()
    window.editor.executor.shutdown(wait=True, cancel_futures=True)
    window.render.shutdown()
    if window.restart_requested:
        updater.dispatch()
    return exit_code
