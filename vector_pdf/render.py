from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from math import ceil
from time import perf_counter

import pypdfium2 as pdfium
from pypdfium2 import raw

from .geometry import Sheet

TILE = 512


@dataclass(frozen=True)
class Tile:
    page: int
    level: float
    col: int
    row: int

    @property
    def bounds(self):
        return (self.col*TILE/self.level, self.row*TILE/self.level,
                (self.col+1)*TILE/self.level, (self.row+1)*TILE/self.level)


def overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


class TileCache:
    def __init__(self, budget=128*1024**2):
        self._data = OrderedDict()
        self.bytes = self.hits = self.misses = 0
        self.budget = budget

    def __contains__(self, key):
        return key in self._data

    def get(self, key):
        value = self._data.get(key)
        if value is None:
            self.misses += 1
        else:
            self.hits += 1
            self._data.move_to_end(key)
        return value

    def items(self):
        return self._data.items()

    def put(self, key, image):
        if key in self._data:
            self._remove(key)
        self._data[key] = image
        self.bytes += image.sizeInBytes()
        while self.bytes > self.budget:
            self._remove(next(iter(self._data)))

    def _remove(self, key):
        self.bytes -= self._data.pop(key).sizeInBytes()

    def invalidate(self, page, rectangles):
        keys = [key for key in self._data if key.page == page and
                any(overlap(key.bounds, bounds) for bounds in rectangles)]
        for key in keys:
            self._remove(key)
        return len(keys)


class RenderEngine:
    """All PDFium calls, including destruction, stay on the same worker."""
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='pdfium')
        self.documents = {}
        self.pages = OrderedDict()

    def open(self, key, path):
        start = perf_counter()
        document = pdfium.PdfDocument(path)
        try:
            sheets = []
            for i in range(len(document)):
                if i == 0:
                    page = document[i]
                    sheets.append(self._sheet(page))
                    page.close()
                else:
                    width, height = document.get_page_size(i)
                    sheets.append(Sheet(width, height, (0, 0, width, height), 0))
            if not sheets:
                raise ValueError('The PDF contains no pages.')
        except Exception:
            document.close()
            raise
        self.close(key)
        self.documents[key] = document
        return sheets, (perf_counter()-start)*1000

    def attach(self, key, path):
        self.documents[key] = pdfium.PdfDocument(path)

    @staticmethod
    def _sheet(page):
        return Sheet(*page.get_size(), tuple(page.get_bbox()), page.get_rotation())

    def sheet(self, key, number):
        return self._sheet(self._page(key, number))

    def close(self, key):
        for pair in list(self.pages):
            if pair[0] == key:
                self.pages.pop(pair).close()
        obj = self.documents.pop(key, None)
        if obj is not None:
            obj.close()

    def _page(self, key, number):
        pair = key, number
        if pair not in self.pages:
            self.pages[pair] = self.documents[key][number]
        self.pages.move_to_end(pair)
        while len(self.pages) > 4:
            self.pages.popitem(last=False)[1].close()
        return self.pages[pair]

    def render(self, key, tile):
        start = perf_counter()
        page = self._page(key, tile.page)
        return self._render(page, tile, start)

    @staticmethod
    def _render(page, tile, start):
        width, height = (ceil(value*tile.level) for value in page.get_size())
        left, top = tile.col*TILE, tile.row*TILE
        w, h = min(TILE, width-left), min(TILE, height-top)
        if min(w, h) < 1:
            return None
        bitmap = pdfium.PdfBitmap.new_native(w, h, raw.FPDFBitmap_BGRx)
        try:
            bitmap.fill_rect((255, 255, 255, 255), 0, 0, w, h)
            raw.FPDF_RenderPageBitmap(bitmap, page, -left, -top, width, height, 0,
                                      raw.FPDF_ANNOT | raw.FPDF_LCD_TEXT | raw.FPDF_RENDER_LIMITEDIMAGECACHE)
            return w, h, bitmap.stride, bytes(bitmap.buffer), (perf_counter()-start)*1000
        finally:
            bitmap.close()

    def thumbnail(self, key, number):
        start = perf_counter()
        page = self.pages.get((key, number))
        disposable = page is None
        if disposable:
            page = self.documents[key][number]
        try:
            scale = min(140/page.get_width(), 78/page.get_height())
            return self._render(page, Tile(number, scale, 0, 0), start)
        finally:
            if disposable:
                page.close()

    def shutdown(self):
        def close_all():
            for key in list(self.documents):
                self.close(key)
        self.executor.submit(close_all)
        self.executor.shutdown(wait=True, cancel_futures=False)
