from math import ceil, floor, hypot, isfinite, log
from time import perf_counter

from PySide6.QtCore import QPointF, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QTransform
from PySide6.QtWidgets import QApplication, QWidget

from .geometry import box, inverse, point
from .render import TILE, Tile, overlap

MIN_ZOOM, MAX_ZOOM = .01, 24.
PREVIEW_FAMILIES = {
    'ISO 3098': 'osifont',
    'Sans': 'DejaVu Sans',
    'Serif': 'DejaVu Serif',
    'Mono': 'DejaVu Sans Mono',
}


class Canvas(QWidget):
    area = Signal(object)
    hit = Signal(object)
    coordinate = Signal(object)
    zoomed = Signal(float)
    demand = Signal(object)
    move_preview = Signal(object)
    move_finished = Signal(object)

    def __init__(self):
        super().__init__()
        self.setObjectName('drawing')
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setMinimumSize(240, 180)
        self.sheet = None
        self.page = 0
        self.zoom = 1.
        self.rotation = 0
        self.pan = QPointF()
        self.cache = None
        self.mode = 'view'
        self.drag = None
        self.marquee = None
        self.selection = None
        self.preview = None
        self.move_enabled = False
        self.move_bounds = None
        self.surface = "#151522"
        self.fps = 0.
        self.ui_ms = 0.
        self.frames = 0
        self.frame_epoch = perf_counter()
        self.timer = QTimer(self, singleShot=True, interval=20)
        self.timer.timeout.connect(self.request)

    def sheet_valid(self):
        sheet = self.sheet
        if sheet is None:
            return False
        try:
            values = (sheet.width, sheet.height, *sheet.crop)
            return (all(isfinite(value) for value in values) and sheet.width > 0
                    and sheet.height > 0 and sheet.crop[2] > sheet.crop[0]
                    and sheet.crop[3] > sheet.crop[1])
        except (TypeError, ValueError):
            return False

    def set_sheet(self, page, sheet, fit=True):
        self.page, self.sheet = page, sheet
        self.cancel_move()
        self.drag = None
        self.selection = self.marquee = self.preview = None
        self.move_enabled = False
        self.move_bounds = None
        if fit:
            self.rotation = 0
            self.fit()
        else:
            self.refresh()

    def view_state(self):
        return self.zoom, self.rotation

    def restore_view(self, zoom, rotation):
        """Restore a per-page view and center the sheet in the viewport."""
        if not self.sheet_valid():
            self.zoom, self.pan = 1., QPointF()
            self.zoomed.emit(self.zoom)
            self.refresh()
            return
        self.zoom = min(MAX_ZOOM, max(MIN_ZOOM, float(zoom)))
        self.rotation = int(rotation) % 360
        if self.rotation not in (0, 90, 180, 270):
            self.rotation = 0
        w, h = self.sheet.width, self.sheet.height
        if self.rotation in (90, 270):
            w, h = h, w
        self.pan = QPointF((self.width()-w*self.zoom)/2, (self.height()-h*self.zoom)/2)
        self.zoomed.emit(self.zoom)
        self.refresh()

    def transform(self):
        if not self.sheet_valid():
            return QTransform()
        t = QTransform()
        t.translate(self.pan.x(), self.pan.y())
        t.scale(self.zoom, self.zoom)
        if self.rotation == 90:
            t.translate(self.sheet.height, 0)
        elif self.rotation == 180:
            t.translate(self.sheet.width, self.sheet.height)
        elif self.rotation == 270:
            t.translate(0, self.sheet.width)
        t.rotate(self.rotation)
        return t

    def refresh(self):
        self.update()
        if self.sheet_valid() and not self.timer.isActive():
            self.timer.start()

    def visible(self):
        if not self.sheet_valid():
            return QRectF()
        transform, invertible = self.transform().inverted()
        if not invertible:
            return QRectF()
        return transform.mapRect(QRectF(self.rect())).intersected(
            QRectF(0, 0, self.sheet.width, self.sheet.height))

    def request(self):
        if not self.sheet_valid() or not self.cache or not self.isVisible() or self.zoom <= 0:
            return
        view = self.visible()
        if view.isEmpty():
            self.demand.emit([])
            return
        ratio = self.devicePixelRatioF()
        level = round(2.0 ** (round(2*log(self.zoom*ratio, 2))/2), 7)
        if not isfinite(level) or level <= 0:
            return
        unit = TILE/level
        cols = range(max(0, floor(view.left()/unit)), min(ceil(self.sheet.width/unit), ceil(view.right()/unit)))
        rows = range(max(0, floor(view.top()/unit)), min(ceil(self.sheet.height/unit), ceil(view.bottom()/unit)))
        center = view.center()
        needed = [Tile(self.page, level, c, r) for r in rows for c in cols]
        needed.sort(key=lambda k: hypot((k.col+.5)*unit-center.x(), (k.row+.5)*unit-center.y()))
        self.demand.emit([k for k in needed if self.cache.peek(k) is None])

    def fit(self):
        if self.sheet is None:
            return
        if not self.sheet_valid():
            self.zoom, self.pan = 1., QPointF()
            self.zoomed.emit(self.zoom)
            self.refresh()
            return
        w, h = self.sheet.width, self.sheet.height
        if self.rotation in (90, 270):
            w, h = h, w
        available_w, available_h = max(20, self.width()-48), max(20, self.height()-48)
        scale = min(available_w/w, available_h/h)
        if not isfinite(scale) or scale <= 0:
            scale = 1.
        self.zoom = min(MAX_ZOOM, max(MIN_ZOOM, scale))
        self.pan = QPointF((self.width()-w*self.zoom)/2, (self.height()-h*self.zoom)/2)
        self.zoomed.emit(self.zoom)
        self.refresh()

    def rotate(self):
        if not self.sheet_valid():
            return
        center = QPointF(self.rect().center())
        transform, invertible = self.transform().inverted()
        if not invertible:
            return
        anchor = transform.map(center)
        self.rotation = (self.rotation+90)%360
        self.pan += center-self.transform().map(anchor)
        self.zoomed.emit(self.zoom)
        self.refresh()

    def zoom_at(self, multiplier, anchor=None):
        if not self.sheet_valid():
            return
        anchor = anchor if anchor is not None else QPointF(self.rect().center())
        transform, invertible = self.transform().inverted()
        if not invertible:
            return
        world = transform.map(anchor)
        self.zoom = min(MAX_ZOOM, max(MIN_ZOOM, self.zoom*multiplier))
        self.pan += anchor-self.transform().map(world)
        self.zoomed.emit(self.zoom)
        self.refresh()

    def document_point(self, where):
        if not self.sheet_valid():
            return 0., 0.
        transform, invertible = self.transform().inverted()
        if not invertible:
            return 0., 0.
        local = transform.map(where)
        try:
            return point(inverse(self.sheet.to_screen), local.x(), local.y())
        except (KeyError, ZeroDivisionError, TypeError, ValueError):
            return 0., 0.

    def set_mode(self, mode):
        if self.mode != mode:
            self.cancel_move()
            self.drag = None
            self.marquee = None
            self.move_bounds = None
        self.mode = mode
        self.setCursor(Qt.CursorShape.OpenHandCursor if mode == 'view' else Qt.CursorShape.CrossCursor)

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if delta and self.sheet:
            if self.drag:
                if self.drag[0] == 'move':
                    self.move_preview.emit(None)
                self.drag = None
                self.marquee = None
                self.move_bounds = None
                self.set_mode(self.mode)
                self.update()
            self.zoom_at(1.18**(delta/120.), event.position())
            event.accept()

    def mousePressEvent(self, event):
        if not self.sheet_valid():
            return
        left = event.button() == Qt.MouseButton.LeftButton
        if event.button() == Qt.MouseButton.MiddleButton or (left and self.mode == 'view' and not self.drag):
            self.drag = ('pan', event.position(), self.pan)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif left and self.mode == 'region':
            # The area tool always starts a marquee, even over a previous selection.
            self.drag = ('region', event.position(), None)
            self.marquee = QRectF(event.position(), event.position())
            self.update()
        elif left and self.mode == 'text' and self.on_selection(event.position()):
            self.setFocus()
            self.drag = ('move', event.position(), self.document_point(event.position()))
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif left:
            self.hit.emit(self.document_point(event.position()))

    def mouseMoveEvent(self, event):
        if not self.sheet_valid():
            return
        self.coordinate.emit(self.document_point(event.position()))
        if self.drag and self.drag[0] == 'pan':
            self.pan = self.drag[2]+event.position()-self.drag[1]
            self.refresh()
        elif self.drag and self.drag[0] == 'move':
            if (delta := self.move_delta(event.position())) is not None:
                self.move_preview.emit(delta)
        elif self.drag:
            self.marquee = QRectF(self.drag[1], event.position()).normalized()
            self.update()
        elif self.mode != 'view':
            self.setCursor(Qt.CursorShape.SizeAllCursor if self.on_selection(event.position())
                           else Qt.CursorShape.CrossCursor)

    def on_selection(self, where):
        if not self.move_enabled or not self.selection or self.mode != 'text':
            return False
        x, y = self.document_point(where)
        l, b, r, t = self.selection
        return l <= x <= r and b <= y <= t

    def move_delta(self, where):
        _, start, origin = self.drag
        if (where-start).manhattanLength() < QApplication.startDragDistance():
            return None
        x, y = self.document_point(where)
        return x-origin[0], y-origin[1]

    def cancel_move(self):
        if self.drag and self.drag[0] == 'move':
            self.drag = None
            self.move_preview.emit(None)
            self.move_bounds = None
            self.update()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self.drag and self.drag[0] == 'move':
            self.cancel_move()
            self.set_mode(self.mode)
            event.accept()
            return
        super().keyPressEvent(event)

    def mouseReleaseEvent(self, event):
        if not self.drag:
            self.marquee = None
            return
        kind = self.drag[0]
        left = event.button() == Qt.MouseButton.LeftButton
        if kind == 'move' and left:
            delta = self.move_delta(event.position())
            self.drag = None
            self.move_finished.emit(delta)
        elif kind == 'region' and left:
            x, y = self.document_point(self.drag[1])
            u, v = self.document_point(event.position())
            self.drag = None
            self.marquee = None
            self.area.emit((min(x, u), min(y, v), max(x, u), max(y, v)))
        elif kind == 'move':
            # A release of another button cancels the move without committing.
            self.move_preview.emit(None)
            self.move_bounds = None
        # Any release clears every kind of drag, including pan and region.
        self.drag = None
        self.marquee = None
        self.set_mode(self.mode)
        self.update()

    def resizeEvent(self, event):
        self.refresh()
        super().resizeEvent(event)

    def showEvent(self, event):
        self.refresh()
        super().showEvent(event)

    def tiles_for_paint(self):
        if not self.sheet_valid() or not self.cache:
            return []
        view = self.visible()
        if view.isEmpty():
            return []
        wanted = (view.left(), view.top(), view.right(), view.bottom())
        items = [(key, image) for key, image in self.cache.items()
                 if key.page == self.page and overlap(key.bounds, wanted)]
        # Draw low resolution first and the sharpest tiles last.
        items.sort(key=lambda entry: entry[0].level)
        return items

    def paintEvent(self, event):
        start = perf_counter()
        painter = QPainter(self)
        painter.fillRect(event.rect(), QColor(self.surface))
        if self.sheet_valid():
            painter.setTransform(self.transform())
            sheet_rect = QRectF(0, 0, self.sheet.width, self.sheet.height)
            painter.fillRect(sheet_rect.adjusted(5, 5, 5, 5), QColor(0, 0, 0, 75))
            painter.fillRect(sheet_rect, QColor('#ffffff'))
            painter.setClipRect(sheet_rect)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            for key, image in self.tiles_for_paint():
                px = key.col*TILE/key.level
                py = key.row*TILE/key.level
                painter.drawImage(QRectF(px, py, image.width()/key.level, image.height()/key.level), image)
            self.paint_selection(painter)
            painter.resetTransform()
            painter.setClipping(False)
        if self.marquee:
            painter.fillRect(self.marquee, QColor(20, 126, 237, 46))
            painter.setPen(QPen(QColor('#1482ee'), 1.))
            painter.drawRect(self.marquee)
        painter.end()
        end = perf_counter()
        self.ui_ms = (end-start)*1000
        self.frames += 1
        if end-self.frame_epoch >= .7:
            self.fps = self.frames/(end-self.frame_epoch)
            self.frame_epoch, self.frames = end, 0

    def paint_selection(self, painter):
        if not self.sheet_valid():
            return
        bounds = self.move_bounds or self.selection
        if bounds:
            try:
                l, t, r, b = box(self.sheet.to_screen, bounds)
                rect = QRectF(l, t, r-l, b-t)
            except (TypeError, ValueError, ZeroDivisionError, KeyError):
                rect = None
            if rect is not None:
                painter.fillRect(rect, QColor(20, 130, 238, 36))
                pen = QPen(QColor('#147ce8'), 1.5, Qt.PenStyle.DashLine)
                pen.setCosmetic(True)
                painter.setPen(pen)
                painter.drawRect(rect)
        if self.preview and self.preview.text:
            edit = self.preview
            try:
                x, y = point(self.sheet.to_screen, edit.x, edit.y)
                painter.save()
                try:
                    painter.translate(x, y)
                    painter.rotate(self.sheet.rotation-edit.angle)
                    if (shear := edit.shear()):
                        painter.shear(-shear, 0.)
                    family = edit.current_font()
                    name = PREVIEW_FAMILIES.get(family)
                    if name is None and family.startswith('Local · '):
                        name = family.split(' · ', 1)[1]
                    font = QFont(name or 'DejaVu Sans')
                    font.setItalic(edit.native_italic())
                    font.setPixelSize(max(1, round(edit.size)))
                    painter.setFont(font)
                    painter.setPen(preview_color(edit))
                    painter.drawText(QPointF(0., 0.), edit.text)
                finally:
                    painter.restore()
            except (RuntimeError, TypeError, ValueError, ZeroDivisionError):
                pass


def preview_color(edit):
    color = None
    fill = getattr(edit, 'fill_color', None)
    if callable(fill):
        try:
            color = fill()
        except Exception:
            color = None
    if color is None:
        color = getattr(edit, 'color', None)
    if color is not None:
        try:
            if len(color) == 3:
                return QColor.fromRgbF(*color)
        except (TypeError, ValueError):
            pass
    return QColor(10, 105, 218, 220)


def image_from_render(data):
    width, height, stride, pixels, _ = data
    return QImage(pixels, width, height, stride, QImage.Format.Format_RGB32).copy()
