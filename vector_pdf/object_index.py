from dataclasses import dataclass, replace
from math import hypot, isfinite
from time import perf_counter

import pikepdf as q
from rtree.index import Index

from .fonts import read_font
from .geometry import IDENTITY, box, compose, point, similarity, union

SHOW = {'Tj', 'TJ', "'", '"'}
OPS = 'q Q cm gs BT ET Tf Tr Tm Tc Tw Tz Ts Td TD T* Tj TJ \' " Do BMC BDC EMC'
STYLES = {'Tc': 'spacing', 'Tw': 'word_space', 'Tz': 'horizontal', 'Ts': 'rise', 'Tr': 'mode'}


def streams(page):
    obj = page.obj.get('/Contents')
    if obj is None:
        return []
    return list(obj) if isinstance(obj, q.Array) else [obj]


@dataclass(frozen=True)
class Graphics:
    ctm: tuple = IDENTITY
    font: str = ''
    size: float = 0
    mode: int = 0
    spacing: float = 0
    word_space: float = 0
    horizontal: float = 100
    rise: float = 0
    unsafe: bool = False


@dataclass(frozen=True)
class Glyph:
    stream: int
    ordinal: int
    text: str
    data: bytes
    font_key: str
    matrix: tuple
    ctm: tuple
    size: float
    advance: float
    horizontal: float
    bounds: tuple
    scope: tuple
    isolated: bool


@dataclass(frozen=True)
class TextRun:
    id: int
    glyphs: tuple
    font: object
    bounds: tuple
    spaces: tuple = ()

    @property
    def text(self):
        return ''.join((' ' if i in self.spaces else '') + glyph.text
                       for i, glyph in enumerate(self.glyphs))

    @property
    def placement(self):
        glyph = self.glyphs[0]
        matrix = compose(glyph.ctm, glyph.matrix)
        scale, angle = similarity(matrix) or (1., 0.)
        return *matrix[-2:], glyph.size*scale, angle

    @property
    def editable(self):
        return all(g.isolated for g in self.glyphs)


class PageIndex:
    def __init__(self, runs, objects, forms, elapsed):
        self.runs = tuple(runs)
        self.objects = objects
        self.forms = forms
        self.elapsed = elapsed
        self.tree = Index(((run.id, run.bounds, None) for run in runs)) if runs else Index()

    def area(self, bounds):
        return [self.runs[i] for i in sorted(self.tree.intersection(bounds))]


def gap(first, second, fonts):
    a = compose(first.ctm, first.matrix)
    b = compose(second.ctm, second.matrix)
    if (first.stream != second.stream or first.ordinal+1 != second.ordinal or
            fonts[first.font_key].name != fonts[second.font_key].name or first.scope != second.scope or
            first.ctm != second.ctm or first.horizontal != second.horizontal or
            first.isolated != second.isolated or abs(first.size-second.size) > .02 or
            any(abs(x-y) > .002 for x, y in zip(a[:4], b[:4]))):
        return None
    scale = hypot(a[0], a[1])
    if scale < 1e-8:
        return None
    dx, dy = b[4]-a[4], b[5]-a[5]
    along = (dx*a[0]+dy*a[1])/scale
    across = abs(dx*a[1]-dy*a[0])/scale
    em = first.size * scale
    spacing = along - first.advance*scale
    # Limit the gap to a typographic word space to keep adjacent table cells separate.
    if along <= 0 or across >= .12*em or spacing > .52*em:
        return None
    return spacing > .21*em and not (first.text[-1].isspace() or second.text[0].isspace())


def index_page(path, number):
    start = perf_counter()
    with q.Pdf.open(path) as pdf:
        page = pdf.pages[number]
        resources = page.Resources
        fonts = {}
        for key, value in resources.get('/Font', q.Dictionary()).items():
            try:
                font = read_font(value)
            except (ValueError, UnicodeError, AttributeError, TypeError, IndexError, StopIteration, q.PdfError):
                continue
            if font is not None:
                fonts[str(key)] = font
        forms = sum(str(obj.get('/Subtype')) == '/Form'
                    for obj in resources.get('/XObject', q.Dictionary()).values())
        glyphs = []
        state = Graphics()
        stack = []
        marked = []
        mark_id = 0
        matrix = None
        inside = False
        explicit = False
        candidates = []
        tainted = False
        objects = 0
        for stream_number, stream in enumerate(streams(page)):
            ordinal = -1
            if inside:
                tainted = True
            for args, operator in q.parse_content_stream(stream, OPS):
                op = str(operator)
                if op == 'q':
                    stack.append(state)
                    tainted |= inside
                elif op == 'Q':
                    state = stack.pop() if stack else Graphics(unsafe=True)
                    tainted |= inside
                elif op == 'cm' and len(args) == 6:
                    state = replace(state, ctm=compose(state.ctm, tuple(map(float, args))))
                    tainted |= inside
                elif op == 'gs':
                    state = replace(state, unsafe=True)
                    tainted |= inside
                elif op in ('BDC', 'BMC'):
                    mark_id += 1
                    marked.append(mark_id)
                elif op == 'EMC':
                    if marked:
                        marked.pop()
                    else:
                        state = replace(state, unsafe=True)
                elif op == 'BT':
                    matrix, explicit, inside, tainted = IDENTITY, False, True, inside
                    candidates = []
                elif op == 'ET':
                    for glyph in candidates:
                        safe = not tainted and len(candidates) == 1 and glyph.scope == tuple(marked)
                        glyphs.append(replace(glyph, isolated=glyph.isolated and safe))
                    inside, candidates = False, []
                elif op == 'Tf' and len(args) == 2:
                    state = replace(state, font=str(args[0]), size=float(args[1]))
                elif op in STYLES and len(args) == 1:
                    state = replace(state, **{STYLES[op]: float(args[0])})
                elif op == 'Tm' and inside and len(args) == 6:
                    matrix, explicit = tuple(map(float, args)), True
                elif op in ('Td', 'TD', 'T*'):
                    explicit = False
                    tainted |= inside
                elif op in SHOW:
                    ordinal += 1
                    objects += 1
                    font = fonts.get(state.font)
                    if not (inside and explicit and matrix and op == 'Tj' and len(args) == 1 and isinstance(args[0], q.String)
                            and font and 0 < state.size <= 5000):
                        tainted = True
                        explicit = False
                        continue
                    data = bytes(args[0])
                    text = font.decode(data)
                    if text is None:
                        tainted = True
                        explicit = False
                        continue
                    horizontal = state.horizontal / 100
                    advance = font.width(data)*state.size*horizontal
                    shape = compose(state.ctm, matrix)
                    if not all(isfinite(v) for v in shape):
                        explicit = False
                        tainted = True
                        continue
                    left, bottom, right, top = font.bounds
                    bounds = box(shape, (min(0, left)*state.size*horizontal, min(bottom, font.descender)*state.size,
                                         max(0.4*state.size, advance+(max(1, right)-1)*state.size*horizontal),
                                         max(top, font.ascender)*state.size))
                    safe = (not state.unsafe and state.mode == 0 and .05 <= horizontal <= 10 and
                            state.rise == state.spacing == state.word_space == 0 and
                            similarity(state.ctm) is not None and similarity(shape) is not None)
                    candidates.append(Glyph(stream_number, ordinal, text, data, state.font, matrix,
                                            state.ctm, state.size, advance, horizontal, bounds, tuple(marked), safe))
                    explicit = False
        runs, collected, spaces = [], [], []
        for glyph in glyphs:
            if collected:
                separator = gap(collected[-1], glyph, fonts)
                if separator is None:
                    append(runs, collected, fonts, spaces)
                    collected, spaces = [], []
                elif separator:
                    spaces.append(len(collected))
            collected.append(glyph)
        if collected:
            append(runs, collected, fonts, spaces)
        return PageIndex(runs, objects, forms, perf_counter()-start)


def append(runs, glyphs, fonts, spaces):
    if not ''.join(g.text for g in glyphs).strip():
        return
    runs.append(TextRun(len(runs), tuple(glyphs), fonts[glyphs[0].font_key],
                        union(g.bounds for g in glyphs), tuple(spaces)))
