"""Run with VECTOR_TEST_PDF and optionally QT_QPA_PLATFORM=offscreen."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QLabel
from vector_pdf.object_index import index_page

from vector_pdf.geometry import point
from vector_pdf.ui import MainWindow, configure_fonts
from sample import stamp_settings


def wait_until(app, condition, description, seconds=25):
    limit = monotonic()+seconds
    while monotonic() < limit:
        app.processEvents()
        if condition():
            return
        QTest.qWait(25)
    raise AssertionError(description)


def run():
    app = QApplication([])
    app.setStyle('Fusion')
    configure_fonts(app)
    window = MainWindow()
    errors = []
    window.error = errors.append
    window.show()
    source = Path(os.environ['VECTOR_TEST_PDF'])
    shot = os.getenv('VECTOR_SCREENSHOT_DIR')
    settings = stamp_settings()
    with TemporaryDirectory() as directory:
        destination = Path(directory) / 'acceptance.pdf'
        QFileDialog.getSaveFileName = lambda *args: (str(destination), 'PDF')
        try:
            window.open_file(source)
            tab = window.current_tab()
            wait_until(app, lambda: len(tab.sheets) == 9 and tab.view.cache.bytes, 'document or tiles missing')
            wait_until(app, lambda: 2 in tab.thumb_rev, 'thumbnails missing')
            if shot:
                window.grab().save(str(Path(shot) / 'viewer.png'))
            canvas = tab.view
            canvas.zoom = 1.8
            l, b, r, t = settings['box']
            p = QPointF(*point(canvas.sheet.to_screen, (l+r)/2, (b+t)/2))
            canvas.pan = QPointF(canvas.width()/2, canvas.height()/2)-p*canvas.zoom
            canvas.refresh()
            window.change_mode('region')
            a, b = [canvas.transform().map(QPointF(*point(canvas.sheet.to_screen, *xy))).toPoint()
                    for xy in ((l, b), (r, t))]
            QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=a)
            QTest.mouseMove(canvas, b, 10)
            QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=b)
            wait_until(app, lambda: any(tab.matches.item(i).text() == settings['old']
                      for i in range(tab.matches.count())), 'stamp surname absent from selected area')
            for i in range(tab.matches.count()):
                if tab.matches.item(i).text() == settings['old']:
                    tab.matches.setCurrentRow(i)
                    tab.select_item(tab.matches.item(i))
                    break
            tab.text.setText(settings['new'])
            assert canvas.preview and canvas.preview.text == settings['new']
            QTest.mouseClick(tab.apply_button, Qt.MouseButton.LeftButton)
            wait_until(app, lambda: tab.document.cursor == 1 and not tab.busy, 'apply failed')
            assert tab.document.metrics.invalidated >= 1
            tab.travel(-1)
            wait_until(app, lambda: tab.document.cursor == 0 and not tab.busy, 'undo failed')
            tab.travel(1)
            wait_until(app, lambda: tab.document.cursor == 1 and not tab.busy, 'redo failed')
            edit = next(iter(tab.document.edits.values()))
            tab.load_edit(edit)
            window.change_mode('text')
            window.canvas('rotate')
            center = QPointF(canvas.rect().center())
            origin = canvas.transform().map(QPointF(*point(canvas.sheet.to_screen, edit.x, edit.y)))
            canvas.pan += center-origin
            canvas.refresh()
            start = canvas.transform().map(QPointF(*point(canvas.sheet.to_screen, edit.x+3, edit.y+2))).toPoint()
            target = start + type(start)(49, 60)
            QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseMove(canvas, target, 60)
            assert canvas.preview and canvas.preview.x != edit.x
            QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=target)
            wait_until(app, lambda: tab.document.cursor == 2 and not tab.busy, 'drag did not commit')
            moved = tab.document.edits[(0, edit.id)]
            assert abs(moved.x-edit.x) > 5 and abs(moved.y-edit.y) > 5
            tab.travel(-1)
            wait_until(app, lambda: tab.document.cursor == 1 and not tab.busy, 'undo drag failed')
            assert tab.document.edits[(0, edit.id)].x == edit.x
            tab.travel(1)
            wait_until(app, lambda: tab.document.cursor == 2 and not tab.busy, 'redo drag failed')
            tab.save()
            wait_until(app, lambda: destination.exists() and not tab.busy, 'save failed')
            assert not errors, errors
            result = next(r for r in index_page(destination, 0).area(moved.bounds()) if r.text == settings['new'])
            assert abs(result.placement[0]-moved.x) < .001
            assert abs(result.placement[1]-moved.y) < .001
            window.theme('solar')
            assert tab.text.font().family() == 'Exo 2'
            assert window.findChild(QLabel, 'brand').font().family() == 'Russo One'
            # Restore the selected property card for the screenshot after the redo.
            tab.load_edit(next(iter(tab.document.edits.values())))
            wait_until(app, lambda: not tab.tile_job and not tab.tiles, 'render queue was not drained')
            if shot:
                window.grab().save(str(Path(shot) / 'stamp-edit.png'))
            tab.family.setCurrentIndex(tab.family.findData('ISO 3098'))
            assert tab.italic.isEnabled()
            tab.italic.setChecked(True)
            assert tab.read_fields().shear() > .2
            tab.family.showPopup()
            wait_until(app, lambda: tab.family.view().isVisible(), 'font menu did not open')
            assert tab.family.view().palette().base().color().name() == '#fffaf0'
            if shot:
                tab.family.view().grab().save(str(Path(shot) / 'solar-font-menu.png'))
            tab.family.hidePopup()
            print('PASS: open, select, edit, drag on rotated view, undo/redo, solar theme, save and reopen')
            print(tab.document.metrics)
        finally:
            window.close()
            window.editor.executor.shutdown(wait=True, cancel_futures=True)
            window.render.shutdown()


if __name__ == '__main__':
    run()
