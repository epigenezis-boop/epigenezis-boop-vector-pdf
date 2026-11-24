from math import ceil, floor, hypot, log
from time import perf_counter

from PySide6.QtCore import QPointF, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QTransform
from PySide6.QtWidgets import QApplication, QWidget

from .geometry import box, inverse, point
from .render import TILE, Tile, overlap


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
        self.setMinimumSize(370, 280)
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

    def set_sheet(self, page, sheet, fit=True):
        self.page, self.sheet = page, sheet
        self.cancel_move()
        self.selection = self.marquee = self.preview = None
        self.move_enabled = False
        if fit:
            self.rotation = 0
            self.fit()
        else:
            self.refresh()

    def transform(self):
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
        if self.sheet and not self.timer.isActive():
            self.timer.start()

    def visible(self):
        return self.transform().inverted()[0].mapRect(QRectF(self.rect())).intersected(
            QRectF(0, 0, self.sheet.width, self.sheet.height))

    def request(self):
        if not self.sheet or not self.cache or not self.isVisible():
            return
        view = self.visible()
        if view.isEmpty():
            self.demand.emit([])
            return
        ratio = self.devicePixelRatioF()
        level = round(2.0 ** (round(2*log(self.zoom*ratio, 2))/2), 7)
        unit = TILE/level
        cols = range(max(0, floor(view.left()/unit)), min(ceil(self.sheet.width/unit), ceil(view.right()/unit)))
        rows = range(max(0, floor(view.top()/unit)), min(ceil(self.sheet.height/unit), ceil(view.bottom()/unit)))
        center = view.center()
        needed = [Tile(self.page, level, c, r) for r in rows for c in cols]
        needed.sort(key=lambda k: hypot((k.col+.5)*unit-center.x(), (k.row+.5)*unit-center.y()))
        self.demand.emit([k for k in needed if self.cache.get(k) is None])

    def fit(self):
        if not self.sheet:
            return
        w, h = self.sheet.width, self.sheet.height
        if self.rotation in (90, 270):
            w, h = h, w
        scale = max(.01, min((self.width()-48)/w, (self.height()-48)/h))
        self.zoom = scale
        self.pan = QPointF((self.width()-w*scale)/2, (self.height()-h*scale)/2)
        self.zoomed.emit(self.zoom)
        self.refresh()

    def rotate(self):
        if not self.sheet:
            return
        center = QPointF(self.rect().center())
        anchor = self.transform().inverted()[0].map(center)
        self.rotation = (self.rotation+90)%360
        self.pan += center-self.transform().map(anchor)
        self.refresh()

    def zoom_at(self, multiplier, anchor=None):
        if not self.sheet:
            return
        anchor = anchor if anchor is not None else QPointF(self.rect().center())
        world = self.transform().inverted()[0].map(anchor)
        self.zoom = min(24., max(.01, self.zoom*multiplier))
        self.pan += anchor-self.transform().map(world)
        self.zoomed.emit(self.zoom)
        self.refresh()

    def document_point(self, where):
        local = self.transform().inverted()[0].map(where)
        return point(inverse(self.sheet.to_screen), local.x(), local.y())

    def set_mode(self, mode):
        if self.mode != mode:
            self.cancel_move()
        self.mode = mode
        self.setCursor(Qt.CursorShape.OpenHandCursor if mode == 'view' else Qt.CursorShape.CrossCursor)

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if delta and self.sheet:
            self.zoom_at(1.18**(delta/120.), event.position())
            event.accept()

    def mousePressEvent(self, event):
        if not self.sheet:
            return
        left = event.button() == Qt.MouseButton.LeftButton
        if event.button() == Qt.MouseButton.MiddleButton or (left and self.mode == 'view'):
            self.drag = ('pan', event.position(), self.pan)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif left and self.on_selection(event.position()):
            self.setFocus()
            self.drag = ('move', event.position(), self.document_point(event.position()))
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif left and self.mode == 'region':
            self.drag = ('region', event.position(), None)
            self.marquee = QRectF(event.position(), event.position())
        elif left:
            self.hit.emit(self.document_point(event.position()))

    def mouseMoveEvent(self, event):
        if not self.sheet:
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
        if not self.move_enabled or not self.selection or self.mode == 'view':
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
            return
        if self.drag[0] == 'move':
            if event.button() != Qt.MouseButton.LeftButton:
                return
            delta = self.move_delta(event.position())
            self.drag = None
            self.move_finished.emit(delta)
        elif self.drag[0] == 'region':
            x, y = self.document_point(self.drag[1])
            u, v = self.document_point(event.position())
            self.area.emit((min(x, u), min(y, v), max(x, u), max(y, v)))
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

    def paintEvent(self, event):
        start = perf_counter()
        painter = QPainter(self)
        painter.fillRect(event.rect(), QColor(self.surface))
        if self.sheet:
            painter.setTransform(self.transform())
            sheet_rect = QRectF(0, 0, self.sheet.width, self.sheet.height)
            painter.fillRect(sheet_rect.adjusted(5, 5, 5, 5), QColor(0, 0, 0, 75))
            painter.fillRect(sheet_rect, QColor('#ffffff'))
            painter.setClipRect(sheet_rect)
            view = self.visible()
            wanted = (view.left(), view.top(), view.right(), view.bottom())
            items = [(key, image) for key, image in self.cache.items()
                     if key.page == self.page and overlap(key.bounds, wanted)]
            items.sort(key=lambda entry: abs(log(entry[0].level/(self.zoom*self.devicePixelRatioF()))), reverse=True)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            for key, image in items:
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
        bounds = self.move_bounds or self.selection
        if bounds:
            l, t, r, b = box(self.sheet.to_screen, bounds)
            rect = QRectF(l, t, r-l, b-t)
            painter.fillRect(rect, QColor(20, 130, 238, 36))
            pen = QPen(QColor('#147ce8'), 1.5, Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawRect(rect)
        if self.preview and self.preview.text:
            edit = self.preview
            x, y = point(self.sheet.to_screen, edit.x, edit.y)
            painter.save()
            painter.translate(x, y)
            painter.rotate(self.sheet.rotation-edit.angle)
            font = QFont('DejaVu Sans')
            font.setPixelSize(max(1, round(edit.size)))
            painter.setFont(font)
            painter.setPen(QColor(10, 105, 218, 220))
            painter.drawText(QPointF(0., 0.), edit.text)
            painter.restore()


def image_from_render(data):
    width, height, stride, pixels, _ = data
    return QImage(pixels, width, height, stride, QImage.Format.Format_RGB32).copy()
