from concurrent.futures import Future
from dataclasses import replace
from math import hypot
from pathlib import Path
import sys
from time import perf_counter
from threading import Thread
from weakref import ref

from PySide6.QtCore import QObject, QPointF, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QFontDatabase, QIcon, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDockWidget, QDoubleSpinBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPushButton, QSplitter, QTabWidget, QToolButton, QVBoxLayout, QWidget,
    QColorDialog,
)

from .canvas import Canvas, image_from_render
from .document import Document, LocalEditor
from .geometry import box, union
from .palette import STRIPES, THEMES, stylesheet
from .render import RenderEngine, TileCache
from .fonts import ASSETS, import_font, learn_fonts
from .writer import Edit, FALLBACKS, file_stamp
from . import updater


class Ribbon(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedHeight(12)

    def paintEvent(self, event):
        with QPainter(self) as painter:
            for i, color in enumerate(STRIPES):
                painter.fillRect(0, i*3, self.width(), 3, QColor(color))


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

    def __init__(self):
        super().__init__()
        self.complete.connect(self.finish, Qt.ConnectionType.QueuedConnection)

    def submit(self, executor, fn, done, *args):
        future = executor.submit(fn, *args)
        future.add_done_callback(lambda result: self.complete.emit(done, result))
        return future

    def finish(self, callback, future):
        if not future.cancelled():
            callback(future)


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
        self.view = Canvas()
        self.view.cache = TileCache()
        self.view.surface = window.surface
        self.view.set_mode('view')
        self.view.demand.connect(self.demand)
        self.view.hit.connect(self.hit)
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
        rl.addWidget(self.note)
        self.matches = QListWidget()
        self.matches.setMaximumHeight(126)
        self.matches.itemActivated.connect(self.select_item)
        self.matches.itemClicked.connect(self.select_item)
        rl.addWidget(self.matches)

        self.form = QWidget()
        fields = QFormLayout(self.form)
        fields.setContentsMargins(0, 2, 0, 0)
        self.text = QLineEdit()
        self.text.setMaxLength(1500)
        self.text.textChanged.connect(self.preview)
        self.text.returnPressed.connect(self.apply)
        self.family = QComboBox()
        for family in ('Original', *FALLBACKS):
            self.family.addItem(family, family)
        self.family.currentIndexChanged.connect(self.preview)
        self.size = self.spin(.25, 4000, ' pt')
        self.angle = self.spin(-360, 360, '°')
        self.color = QPushButton()
        self.color.clicked.connect(self.choose_color)
        for widget, ru, en in [(self.text, 'Текст', 'Text'), (self.family, 'Шрифт', 'Font'),
                               (self.size, 'Размер', 'Size'),
                               (self.angle, 'Угол', 'Angle'), (self.color, 'Цвет', 'Color')]:
            label = QLabel()
            self.lang.bind(label.setText, ru, en)
            label.setBuddy(widget)
            fields.addRow(label, widget)
        self.font_notice = QLabel()
        self.font_notice.setWordWrap(True)
        self.font_notice.setObjectName('panelNote')
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
        self.splitter.addWidget(self.right)
        self.left.setMinimumWidth(155)
        self.right.setMinimumWidth(270)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([178, 810, 285])
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
        widget.setDecimals(3)
        widget.setSuffix(suffix)
        widget.valueChanged.connect(self.preview)
        return widget

    def start(self):
        self.document.stamp = file_stamp(self.document.source)
        self.bridge.submit(self.render.executor, self.render.open, self.opened,
                           self.document.key, self.document.source)
        self.bridge.submit(self.editor.executor, self.editor.check, self.permissions, self.document)

    def permissions(self, task):
        if self.closed:
            return
        self.permission_checked = True
        try:
            task.result()
        except Exception as error:
            self.read_only = True
            self.view.set_mode('view')
            self.notice('Только просмотр. '+str(error), 'Read-only. '+str(error))
        self.status()

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
        for i, sheet in enumerate(self.sheets, 1):
            item = QListWidgetItem(f'{i}   ·   {format_name(sheet)}')
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            self.pages.addItem(item)
        self.pages.setCurrentRow(0)
        self.pages.blockSignals(False)
        self.view.set_sheet(0, self.sheets[0])
        if not self.read_only:
            self.notice('Выберите «Текст» или «Область», чтобы начать правку.',
                        'Choose Text or Area to begin editing.')
        self.status()
        self.view.refresh()
        QTimer.singleShot(550, self.queue_thumbnails)

    def status(self):
        self.window.update_status()

    def position(self, xy):
        self.xy = xy
        self.status()

    def page(self, number):
        if self.closed or number < 0 or number == self.current:
            return
        self.current = number
        self.tiles.clear()
        self.active_area = self.pending_area = None
        self.xy = None
        self.clear_selection()
        self.view.set_sheet(number, self.sheets[number])
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
            self.notice(str(error))
            return
        if number == self.current:
            self.view.set_sheet(number, self.sheets[number], fit=False)
            self.view.refresh()

    def queue_thumbnails(self):
        if (self.closed or self.busy or not self.left.isVisible() or not self.sheets
                or self.tile_job or self.thumb_jobs):
            return
        visible = [i for i in range(len(self.sheets)) if
                   self.pages.visualItemRect(self.pages.item(i)).intersects(self.pages.viewport().rect())]
        number = next((i for i in visible if i not in self.thumb_rev), None)
        if number is None:
            return
        key = self.document.key
        self.thumb_jobs[number] = self.bridge.submit(self.render.executor, self.render.thumbnail,
            lambda task: self.thumbnail_done(number, key, task), key, number)

    def thumbnail_done(self, number, key, task):
        self.thumb_jobs.pop(number, None)
        if self.closed:
            return
        try:
            data = task.result()
            if data and self.document.key == key:
                pixmap = QPixmap.fromImage(image_from_render(data))
                icon = QIcon(pixmap)
                icon.addPixmap(pixmap, QIcon.Mode.Selected)
                self.pages.item(number).setIcon(icon)
                self.thumb_rev[number] = key
        except Exception:
            self.thumb_rev[number] = key
        if self.tiles:
            self.next_tile()
        else:
            QTimer.singleShot(30, self.queue_thumbnails)

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
            if key in self.view.cache:
                continue
            source = self.document.key
            self.tile_key = key
            self.tile_job = self.bridge.submit(self.render.executor, self.render.render,
                lambda task: self.tile_done(source, key, task), source, key)
            return
        QTimer.singleShot(135, self.queue_thumbnails)

    def tile_done(self, source, key, task):
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
        self.next_tile()

    def notice(self, ru, en=None):
        self.note.setText(self.lang.get(ru, en or ru))

    def clear_selection(self):
        self.selected = None
        self.drag_origin = self.position_draft = None
        self.view.cancel_move()
        self.view.move_enabled = False
        self.view.selection = self.view.preview = self.view.move_bounds = None
        self.view.update()
        self.form.hide()
        self.matches.clear()
        self.matches.hide()
        self.results.clear()

    def set_mode(self, mode):
        if self.read_only:
            self.view.set_mode('view')
            return
        self.view.set_mode(mode)
        if mode == 'view':
            self.active_area = self.pending_area = None
            self.clear_selection()
            self.notice('Режим просмотра. Колесо мыши меняет масштаб.',
                        'View mode. Scroll to zoom.')
        elif mode == 'region':
            self.notice('Обведите прямоугольником нужную часть чертежа.',
                        'Drag a rectangle over the area you want to edit.')
        else:
            self.notice('Нажмите на текст. Средняя кнопка мыши перемещает лист.',
                        'Click text. Drag with the middle button to pan.')

    def hit(self, xy):
        margin = 7 / self.view.zoom
        x, y = xy
        self.query((x-margin, y-margin, x+margin, y+margin), xy)

    def area(self, bounds):
        if bounds[2]-bounds[0] < 1e-5 or bounds[3]-bounds[1] < 1e-5:
            return self.hit(bounds[:2])
        self.query(bounds, None)

    def query(self, bounds, xy):
        if self.busy or not self.sheets or self.read_only or not self.permission_checked:
            return
        if self.current not in self.sheet_loaded:
            self.notice('Подождите, пока загрузятся размеры листа.', 'Wait until the sheet dimensions load.')
            return
        number = self.current
        if number in self.document.indexes:
            self.display_query(bounds, xy, self.document.indexes[number])
            return
        self.pending_area = number, bounds, xy
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
            _, bounds, xy = self.pending_area
            self.pending_area = None
            self.display_query(bounds, xy, index)

    def display_query(self, bounds, xy, index):
        start = perf_counter()
        self.active_area = bounds if xy is None else None
        runs = {r.id: self.document.get(self.current, r) for r in index.area(bounds)
                if (self.current, r.id) not in self.document.edits}
        for (page, id_), edit in self.document.edits.items():
            if page == self.current and edit.text and intersects(edit.bounds(), bounds):
                runs[id_] = edit
        self.clear_selection()
        if xy and runs:
            x, y = xy
            choice = min(runs.values(), key=lambda edit: hypot(edit.x-x, edit.y-y))
            self.load_edit(choice)
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
        key = item.data(Qt.ItemDataRole.UserRole)
        if key in self.results:
            self.load_edit(self.results[key])

    def load_edit(self, edit):
        self.selected = edit
        self.position_draft = (edit.x, edit.y)
        self.drag_origin = None
        self.view.move_bounds = None
        self.view.selection = edit.bounds()
        self.view.preview = None
        self.view.update()
        self.edit_color = edit.color
        temporary = [self.text, self.family, self.size, self.angle]
        for widget in temporary:
            widget.blockSignals(True)
        self.text.setText(edit.text)
        self.family.setCurrentIndex(self.family.findData(edit.family))
        self.size.setValue(edit.size)
        self.angle.setValue(edit.angle)
        for widget in temporary:
            widget.blockSignals(False)
        editable = not edit.original or edit.original.editable
        self.view.move_enabled = editable and not self.busy
        self.form.setEnabled(editable and not self.busy)
        self.form.show()
        self.remove_button.setEnabled(bool(edit.text))
        self.color_label()
        self.font_label(edit)
        if not editable:
            self.notice('Текст доступен для просмотра. Структура объекта ограничивает правку.',
                        'Text is available for inspection. Its structure restricts editing.')
        elif edit.original:
            self.notice(f'{edit.original.font.name} · перетащите рамку',
                        f'{edit.original.font.name} · drag the selection')
        else:
            self.notice('Введите текст и перетащите его на нужное место.',
                        'Enter text and drag it into place.')

    def font_label(self, edit):
        warning = edit.text and edit.family == 'Original' and edit.current_font() != 'Original'
        family = edit.current_font()
        self.font_notice.setText(self.lang.get(f'Для этих символов встраивается {family}.',
            f'{family} will be embedded for these characters.') if warning else '')

    def color_label(self):
        label = QColor.fromRgbF(*self.edit_color).name().upper() if self.edit_color else self.lang.get('Исходный', 'Original')
        self.color.setText(label)

    def read_fields(self):
        def entered(control, previous):
            return previous if abs(control.value()-previous) < .0005 else control.value()
        x, y = self.position_draft
        return replace(self.selected, text=self.text.text(), x=x, y=y,
                       size=entered(self.size, self.selected.size),
                       angle=entered(self.angle, self.selected.angle), color=self.edit_color,
                       family=self.family.currentData())

    def choose_color(self):
        initial = QColor.fromRgbF(*self.edit_color) if self.edit_color else QColor('black')
        color = QColorDialog.getColor(initial, self, self.lang.get('Цвет текста', 'Text color'))
        if color.isValid():
            self.edit_color = color.redF(), color.greenF(), color.blueF()
            self.color_label()
            self.preview()

    def preview(self, *_):
        if self.selected and not self.busy:
            edit = self.read_fields()
            self.font_label(edit)
            self.view.preview = edit if edit != self.selected else None
            if edit.original is None and edit.text:
                try:
                    self.view.selection = edit.bounds()
                except ValueError:
                    self.view.selection = None
            self.view.update()

    def preview_move(self, delta):
        if not self.selected or self.busy:
            return
        if delta is None:
            self.drag_origin = None
            self.view.move_bounds = None
            self.preview()
            return
        if self.drag_origin is None:
            self.drag_origin = self.read_fields()
        dx, dy = delta
        edit = replace(self.drag_origin, x=self.drag_origin.x+dx, y=self.drag_origin.y+dy)
        self.view.preview = edit
        if self.view.selection:
            l, b, r, t = self.view.selection
            self.view.move_bounds = l+dx, b+dy, r+dx, t+dy
        self.view.update()

    def finish_move(self, delta):
        if delta is None or not self.selected or self.busy:
            self.preview_move(None)
            return
        self.preview_move(delta)
        edit = self.view.preview
        self.drag_origin = None
        self.view.move_bounds = None
        if not edit or not edit.text:
            return
        self.position_draft = (edit.x, edit.y)
        if edit.original is None and (edit.page, edit.id) not in self.document.edits:
            self.preview()
        else:
            self.edit(edit)

    def add(self):
        if not self.sheets or self.busy or self.read_only or not self.permission_checked:
            return
        center = self.view.visible().center()
        position = self.view.transform().map(center)
        x, y = self.view.document_point(position)
        self.load_edit(Edit(self.current, self.document.allocate(), '', x, y, 12., 0., family='ISO 3098'))
        self.view.selection = None
        self.text.setFocus()

    def apply(self):
        if not self.selected or self.busy or not self.form.isEnabled():
            return
        edit = self.read_fields()
        if not edit.text:
            return
        self.edit(edit)

    def remove(self):
        if self.selected and not self.busy and self.form.isEnabled():
            self.edit(replace(self.selected, text=''))

    def edit(self, edit):
        state = self.document.edits.copy()
        key = edit.page, edit.id
        if (edit.original and edit == Edit.from_run(edit.page, edit.original)) or (not edit.original and not edit.text):
            state.pop(key, None)
        else:
            state[key] = edit
        if state != self.document.edits:
            self.change_state(state, None, edit if edit.text else None)

    def travel(self, step):
        target = self.document.cursor+step
        if not self.busy and 0 <= target < len(self.document.states):
            self.change_state(self.document.states[target], target, None)

    def change_state(self, state, cursor, selection):
        try:
            affected = self.differences(state)
        except Exception as error:
            self.window.error(error)
            return
        self.busy = True
        self.form.setEnabled(False)
        self.view.move_enabled = False
        self.notice('Применяем локальную правку…', 'Applying local edit…')
        key, path = self.document.new_version()
        self.window.update_actions()
        self.bridge.submit(self.editor.executor, self.editor.save,
            lambda task: self.prepared(task, key, path, state, cursor, affected, selection),
            self.document, path, state)

    def differences(self, state):
        dirty = {}
        before = self.document.edits
        for key in before.keys() | state.keys():
            left, right = before.get(key), state.get(key)
            if left == right:
                continue
            original = (left or right).original
            bounds = [edit.bounds() for edit in (left, right) if edit and edit.text]
            if original:
                bounds.append(original.bounds)
            if bounds:
                dirty.setdefault(key[0], []).extend(bounds)
        return dirty

    def prepared(self, task, key, path, state, cursor, affected, selection):
        try:
            elapsed = task.result()
        except Exception as error:
            self.edit_failed(error)
            return
        self.bridge.submit(self.render.executor, self.render.attach,
            lambda result: self.render_ready(result, key, path, state, cursor, affected, selection, elapsed), key, path)

    def render_ready(self, task, key, path, state, cursor, affected, selection, elapsed):
        try:
            task.result()
        except Exception as error:
            path.unlink(missing_ok=True)
            self.edit_failed(error)
            return
        document = self.document
        old = document.key
        document.key = key
        if cursor is None:
            document.commit(state)
        else:
            document.cursor = cursor
        self.tiles.clear()
        invalidated = 0
        for page, rectangles in affected.items():
            sheet = self.sheets[page]
            converted = [box(sheet.to_screen, bounds) for bounds in rectangles]
            expanded = [(l-2, t-2, r+2, b+2) for l, t, r, b in converted]
            invalidated += self.view.cache.invalidate(page, expanded)
            self.thumb_rev.pop(page, None)
        document.metrics.invalidated = invalidated
        document.metrics.edit = elapsed
        self.busy = False
        self.clear_selection()
        if selection:
            self.load_edit(selection)
        else:
            self.notice('Правка применена. Сохраните копию PDF.', 'Edit applied. Save a PDF copy.')
        self.view.refresh()
        self.window.rename(self)
        self.window.update_actions()
        self.bridge.submit(self.render.executor, self.render.close, lambda _: self.clear_old(old), old)

    def clear_old(self, key):
        if ':' in key:
            (Path(self.document.workspace.name) / (key.split(':', 1)[1]+'.pdf')).unlink(missing_ok=True)

    def edit_failed(self, error):
        self.busy = False
        self.form.setEnabled(True)
        self.view.move_enabled = bool(self.selected)
        if self.selected:
            self.position_draft = (self.selected.x, self.selected.y)
            self.preview()
        self.notice('Правка отклонена. Исходный PDF сохранён.', 'Edit blocked. Source PDF preserved.')
        self.window.error(error)
        self.window.update_actions()

    def save(self):
        if self.busy:
            return
        path = self.document.source
        default = str(path.with_name(path.stem+'_edited.pdf'))
        selected, _ = QFileDialog.getSaveFileName(self, self.lang.get('Сохранить копию PDF', 'Save PDF copy'),
                                                  default, 'PDF (*.pdf)')
        if not selected:
            return
        target = Path(selected)
        if target.suffix.lower() != '.pdf':
            target = target.with_suffix('.pdf')
        self.busy = True
        self.window.update_actions()
        self.notice('Сохраняем копию PDF…', 'Saving PDF copy…')
        state = self.document.edits.copy()
        self.bridge.submit(self.editor.executor, self.editor.save,
            lambda task: self.saved(target, state, task), self.document, target, state)

    def saved(self, target, state, task):
        self.busy = False
        try:
            self.document.metrics.saved = task.result()
        except Exception as error:
            self.window.error(error)
        else:
            self.document.exported = state
            self.window.rename(self)
            self.notice(f'Копия сохранена: {target.name}', f'Copy saved: {target.name}')
        self.window.update_actions()

    def close_workspace(self):
        self.closed = True
        if self.tile_job:
            self.tile_job.cancel()
        for future in self.thumb_jobs.values():
            future.cancel()
        self.bridge.submit(self.render.executor, self.render.close,
                           lambda _: self.document.workspace.cleanup(), self.document.key)


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
        self.setWindowTitle('VECTOR PDF')
        self.resize(1320, 820)
        self.setMinimumSize(930, 565)
        self.setAcceptDrops(True)
        self.settings = QSettings('VectorPDF', 'VectorPDF')
        self.lang = Language(self.settings.value('language', 'ru'))
        self.appearance = self.settings.value('appearance', 'dark' if self.settings.value('dark', True, type=bool) else 'light')
        if self.appearance not in THEMES:
            self.appearance = 'dark'
        self.surface = THEMES[self.appearance][-1]
        self.setWindowIcon(QIcon(str(ASSETS / 'vector-pdf.png')))
        self.bridge = Bridge()
        self.render = RenderEngine()
        self.editor = LocalEditor()
        self.actions = {}
        self.mode = 'view'
        self.updating = False
        self.pending_update = None
        self.restart_requested = False

        root = QWidget()
        vertical = QVBoxLayout(root)
        vertical.setContentsMargins(0, 0, 0, 0)
        vertical.setSpacing(0)
        self.setCentralWidget(root)
        header = QWidget(objectName='toolbar')
        row = QHBoxLayout(header)
        row.setContentsMargins(15, 10, 12, 10)
        row.setSpacing(7)
        mark = QLabel()
        mark.setPixmap(self.windowIcon().pixmap(QSize(29, 29)))
        row.addWidget(mark)
        brand = QLabel('VECTOR PDF', objectName='brand')
        row.addWidget(brand)
        row.addSpacing(13)
        for name, ru, en, slot, shortcuts in [
            ('open', 'Открыть', 'Open', self.open_dialog, 'Ctrl+O'),
            ('save', 'Сохранить копию', 'Save copy', lambda: self.call('save'), 'Ctrl+Shift+S'),
            ('undo', '↶', '↶', lambda: self.call('travel', -1), 'Ctrl+Z'),
            ('redo', '↷', '↷', lambda: self.call('travel', 1), 'Ctrl+Y'),
        ]:
            self.button(row, name, ru, en, slot, shortcuts)
        self.actions['undo'].setToolTip(self.lang.get('Отменить', 'Undo'))
        self.actions['redo'].setToolTip(self.lang.get('Повторить', 'Redo'))
        row.addSpacing(8)
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
        self.actions['rotate'].setToolTip(self.lang.get('Повернуть вид на 90°', 'Rotate the view by 90°'))
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
        self.status_sheet, self.status_view, self.status_coord = QLabel(), QLabel(), QLabel()
        bar.addWidget(self.status_sheet)
        bar.addStretch()
        bar.addWidget(self.status_view)
        bar.addSpacing(15)
        bar.addWidget(self.status_coord)
        vertical.addWidget(status)
        self.build_menu()
        self.update_menu()
        self.make_diagnostics()
        self.apply_theme()
        self.current_changed()
        if sys.platform == 'win32':
            QTimer.singleShot(1700, lambda: self.check_updates(False)
                              if self.auto_update.isChecked() else None)

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
        if name == 'open':
            button.setObjectName('primary')
        layout.addWidget(button)
        return action

    def build_menu(self):
        file = self.menuBar().addMenu('')
        self.lang.bind(file.setTitle, 'Файл', 'File')
        file.addAction(self.actions['open'])
        file.addAction(self.actions['save'])
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
        self.view_menu = view

    def update_menu(self):
        menu = self.menuBar().addMenu('')
        self.lang.bind(menu.setTitle, 'Обновления', 'Updates')
        menu.setEnabled(sys.platform == 'win32')
        check = menu.addAction('')
        self.lang.bind(check.setText, 'Проверить обновления', 'Check for updates')
        check.triggered.connect(lambda: self.check_updates(True))
        self.auto_update = menu.addAction('')
        self.lang.bind(self.auto_update.setText, 'Загружать автоматически', 'Download automatically')
        self.auto_update.setCheckable(True)
        self.auto_update.setChecked(self.settings.value('auto-update', True, type=bool))
        self.auto_update.toggled.connect(lambda flag: self.settings.setValue('auto-update', flag))
        self.restart_update = menu.addAction('')
        self.lang.bind(self.restart_update.setText, 'Перезапустить и обновить', 'Restart and update')
        self.restart_update.setEnabled(False)
        self.restart_update.triggered.connect(self.restart_for_update)

    def check_updates(self, manual):
        if self.updating or self.pending_update or sys.platform != 'win32':
            return
        self.updating = True
        def work():
            result = Future()
            try:
                if release := updater.check():
                    result.set_result(updater.arm(release))
                else:
                    result.set_result(None)
            except Exception as error:
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
            if manual:
                self.error(error)
            return
        if self.pending_update:
            self.restart_update.setEnabled(True)
            message = self.lang.get(
                f'Обновление {self.pending_update} готово. Оно установится при следующем запуске.',
                f'Update {self.pending_update} is ready. It will start on the next launch.')
            if manual:
                QMessageBox.information(self, 'VECTOR PDF', message)
            else:
                self.statusBar().showMessage(message, 10000)
        elif manual:
            QMessageBox.information(self, 'VECTOR PDF', self.lang.get(
                'Установлена последняя версия.', 'You are on the latest version.'))

    def restart_for_update(self):
        if self.pending_update:
            self.restart_requested = self.close()

    def make_diagnostics(self):
        self.dock = QDockWidget()
        self.lang.bind(self.dock.setWindowTitle, 'Диагностика', 'Diagnostics')
        self.diagnostics = QLabel()
        self.diagnostics.setContentsMargins(12, 10, 12, 12)
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
        self.surface = THEMES[self.appearance][-1]
        self.setStyleSheet(stylesheet(self.appearance))
        self.empty.setStyleSheet('background: '+self.surface)
        for i in range(self.tabs.count()):
            canvas = self.tabs.widget(i).view
            canvas.surface = self.surface
            canvas.update()

    def language(self, code):
        self.lang.change(code)
        self.settings.setValue('language', code)
        tab = self.current_tab()
        if tab and not tab.busy:
            tab.set_mode(self.mode)
        self.update_status()
        self.show_metrics()

    def current_tab(self):
        return self.tabs.currentWidget()

    def call(self, name, *args):
        if tab := self.current_tab():
            getattr(tab, name)(*args)

    def canvas(self, name, *args):
        if tab := self.current_tab():
            getattr(tab.view, name)(*args)

    def change_mode(self, mode):
        self.mode = mode
        self.actions[mode].setChecked(True)
        self.call('set_mode', mode)

    def add(self):
        self.change_mode('text')
        self.call('add')

    def toggle_thumbs(self, state):
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            tab.left.setVisible(state)
        if state:
            self.call('queue_thumbnails')

    def update_actions(self):
        tab = self.current_tab()
        ready = bool(tab and tab.sheets)
        edit = ready and tab.permission_checked and not tab.read_only
        idle = edit and not tab.busy
        for key in ('view', 'fit', 'plus', 'minus', 'rotate'):
            self.actions[key].setEnabled(ready)
        for key in ('text', 'region'):
            self.actions[key].setEnabled(edit)
        self.actions['save'].setEnabled(idle)
        self.actions['add'].setEnabled(idle)
        self.actions['undo'].setEnabled(idle and tab.document.cursor > 0)
        self.actions['redo'].setEnabled(idle and tab.document.cursor+1 < len(tab.document.states))

    def current_changed(self, *_):
        has = bool(self.tabs.count())
        self.empty.setVisible(not has)
        self.tabs.setVisible(has)
        if tab := self.current_tab():
            if tab.sheets:
                tab.set_mode(self.mode)
                tab.view.refresh()
        self.update_actions()
        self.update_status()

    def update_status(self):
        self.update_actions()
        tab = self.current_tab()
        sheet = tab.view.sheet if tab else None
        if not sheet:
            self.status_sheet.setText(self.lang.get('PDF локально', 'Local PDF'))
            self.status_view.clear()
            self.status_coord.clear()
            return
        name = format_name(sheet)
        page = self.lang.get('Лист', 'Sheet')
        self.status_sheet.setText(f'{page} {tab.current+1} / {len(tab.sheets)}  ·  {name}')
        self.status_view.setText(f'{tab.view.zoom:.0%}  ·  {(tab.view.rotation+sheet.rotation)%360}°')
        if tab.xy:
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
        except Exception as error:
            self.error(error)
            return
        for i in range(self.tabs.count()):
            choice = self.tabs.widget(i).family
            if choice.findData(key) < 0:
                choice.addItem(key, key)
        if tab := self.current_tab():
            tab.family.setCurrentIndex(tab.family.findData(key))
        self.status_sheet.setText(self.lang.get('Добавлен шрифт ', 'Font imported ')+Path(path).stem)

    def open_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(self, self.lang.get('Открыть PDF', 'Open PDF'),
                                                '', 'PDF (*.pdf)')
        for path in paths:
            self.open_file(path)

    def open_file(self, path):
        path = Path(path).resolve()
        for i in range(self.tabs.count()):
            if self.tabs.widget(i).document.source == path:
                self.tabs.setCurrentIndex(i)
                return
        try:
            file_stamp(path)
        except OSError as error:
            self.error(error)
            return
        workspace = Workspace(self, Document(path))
        index = self.tabs.addTab(workspace, path.name)
        workspace.left.setVisible(self.show_thumbs.isChecked())
        self.tabs.setTabToolTip(index, str(path))
        self.tabs.setCurrentIndex(index)
        workspace.start()
        self.current_changed()

    def rename(self, workspace):
        index = self.tabs.indexOf(workspace)
        name = workspace.document.source.name
        self.tabs.setTabText(index, name + (' *' if workspace.document.modified else ''))

    def error(self, error):
        QMessageBox.warning(self, self.lang.get('VECTOR PDF · Операция прервана', 'VECTOR PDF · Operation stopped'), str(error))

    def confirm(self, workspace):
        if workspace.busy:
            self.status_sheet.setText(self.lang.get('Дождитесь окончания операции.', 'Wait for the current operation.'))
            return False
        if not workspace.document.modified:
            return True
        result = QMessageBox.question(self,
            self.lang.get('Несохранённые правки', 'Unsaved edits'),
            self.lang.get('Закрыть вкладку и отбросить правки?', 'Close the tab and discard the edits?'),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return result == QMessageBox.StandardButton.Yes

    def close_tab(self, index):
        if index < 0:
            return
        tab = self.tabs.widget(index)
        if self.confirm(tab):
            tab.close_workspace()
            self.tabs.removeTab(index)
            tab.deleteLater()
            self.current_changed()

    def closeEvent(self, event):
        tabs = [self.tabs.widget(i) for i in range(self.tabs.count())]
        if any(not self.confirm(tab) for tab in tabs):
            event.ignore()
            return
        for tab in tabs:
            tab.close_workspace()
        super().closeEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(url.toLocalFile().lower().endswith('.pdf') for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            if (path := url.toLocalFile()).lower().endswith('.pdf'):
                self.open_file(path)
        event.acceptProposedAction()


def configure_fonts(app):
    for filename in ('RussoOne-Regular.ttf', 'Exo2-Variable.ttf'):
        QFontDatabase.addApplicationFont(str(ASSETS / filename))
    ui_font = QFont()
    ui_font.setFamilies(['Russo One', 'Exo 2'])
    ui_font.setPointSize(10)
    app.setFont(ui_font)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName('VECTOR PDF')
    app.setStyle('Fusion')
    configure_fonts(app)
    learn_fonts()
    window = MainWindow()
    window.show()
    for path in sys.argv[1:]:
        window.open_file(path)
    exit_code = app.exec()
    window.editor.executor.shutdown(wait=True, cancel_futures=True)
    window.render.shutdown()
    if window.restart_requested:
        updater.dispatch()
    return exit_code
