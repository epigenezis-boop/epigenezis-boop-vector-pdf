from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from .palette import STRIPES, THEMES

TOOL_ICONS = ('add', 'undo', 'redo', 'rotate')


def toolbar_icon(name, theme):
    _, _, _, _, ink, muted, _, _ = THEMES[theme]
    accent = STRIPES[2] if theme == 'dark' else '#bc4b2c'
    icon = QIcon()
    for mode, foreground, detail in (
        (QIcon.Mode.Normal, ink, accent),
        (QIcon.Mode.Disabled, muted, muted),
    ):
        for scale in (1, 2):
            pixmap = QPixmap(24 * scale, 24 * scale)
            pixmap.setDevicePixelRatio(scale)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(foreground), 2.4, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.SquareCap, Qt.PenJoinStyle.RoundJoin))
            path, marker = QPainterPath(), QPainterPath()
            if name == 'add':
                path.moveTo(3, 5)
                path.lineTo(14, 5)
                path.moveTo(8.5, 5)
                path.lineTo(8.5, 19)
                path.moveTo(5.5, 19)
                path.lineTo(11.5, 19)
                marker.moveTo(14, 14.5)
                marker.lineTo(21, 14.5)
                marker.moveTo(17.5, 11)
                marker.lineTo(17.5, 18)
            elif name == 'rotate':
                circle = QRectF(4, 4, 15, 15)
                path.arcMoveTo(circle, 55)
                path.arcTo(circle, 55, 295)
                marker.moveTo(15, 14)
                marker.lineTo(19, 11)
                marker.lineTo(21, 16)
            else:
                if name == 'redo':
                    painter.translate(24, 0)
                    painter.scale(-1, 1)
                path.moveTo(4, 12)
                path.cubicTo(8, 5, 17, 4, 20, 10)
                path.cubicTo(22, 14, 19, 18, 15, 19)
                marker.moveTo(4, 6)
                marker.lineTo(4, 12)
                marker.lineTo(10, 12)
            painter.drawPath(path)
            painter.setPen(QPen(QColor(detail), 2.4, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.SquareCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(marker)
            painter.end()
            icon.addPixmap(pixmap, mode)
    return icon
