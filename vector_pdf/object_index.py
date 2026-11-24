from dataclasses import dataclass, replace
from math import hypot, isfinite
from time import perf_counter

import pikepdf as q
from rtree.index import Index

from .fonts import read_font
from .geometry import IDENTITY, box, compose, similarity, text_transform, union

SHOW = {'Tj', 'TJ', "'", '"'}
OPS = ('q Q cm gs BT ET Tf Tr Tm Tc Tw Tz Ts Td TD T* Tj TJ \' " Do BMC BDC EMC '
       'rg g k RG G K cs CS sc scn SCN re W W* n S s f F f* B B* b b* sh BI ID EI')
STYLES = {'Tc': 'spacing', 'Tw': 'word_space', 'Tz': 'horizontal', 'Ts': 'rise', 'Tr': 'mode'}
PAINT = {'S', 's', 'f', 'F', 'f*', 'B', 'B*', 'b', 'b*', 'sh', 'Do', 'BI', 'ID', 'EI'}
DEVICE_SPACES = {'/DeviceGray', '/DeviceRGB', '/DeviceCMYK', '/G', '/RGB', '/CMYK'}


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
    fill: tuple | None = None
    color_space: str = '/DeviceGray'
    clip: tuple = ()
    unsafe_color: bool = False


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
    fill: tuple | None = None
    clipped: bool = False
    reason: str = ''
    paint_epoch: int = 0


@dataclass(frozen=True)
class TextRun:
    id: int
    glyphs: tuple
    font: object
    bounds: tuple
    spaces: tuple = ()
    reason: str = ''

    @property
    def text(self):
        return ''.join((' ' if i in self.spaces else '') + glyph.text
                       for i, glyph in enumerate(self.glyphs))

    @property
    def placement(self):
        glyph = self.glyphs[0]
        matrix = compose(glyph.ctm, glyph.matrix)
        scale, angle, _ = text_transform(matrix) or (1., 0., 0.)
        return *matrix[-2:], glyph.size*scale, angle

    @property
    def shear(self):
        glyph = self.glyphs[0]
        shape = text_transform(compose(glyph.ctm, glyph.matrix))
        return shape[2] if shape else 0.

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
            fonts[first.font_key].name != fonts[second.font_key].name or
            first.font_key != second.font_key or first.scope != second.scope or
            first.fill != second.fill or first.clipped != second.clipped or
            first.paint_epoch != second.paint_epoch or
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
    space = fonts[first.font_key].space_width
    limit = min(.34, max(.18, space*1.2))*em
    if along <= 0 or across >= .12*em or spacing > limit:
        return None
    return spacing > .21*em and not (first.text[-1].isspace() or second.text[0].isspace())


def _color(args, space):
    try:
        values = tuple(float(value) for value in args)
    except (TypeError, ValueError, OverflowError):
        return None
    if any(not isfinite(value) for value in values):
        return None
    if space == '/DeviceRGB' and len(values) == 3:
        return tuple(min(1., max(0., value)) for value in values)
    if space == '/DeviceGray' and len(values) == 1:
        return (min(1., max(0., values[0])),)*3
    if space == '/DeviceCMYK' and len(values) == 4:
        c, m, y, k = (min(1., max(0., value)) for value in values)
        return ((1-c)*(1-k), (1-m)*(1-k), (1-y)*(1-k))
    return None


def index_page(path, number):
    start = perf_counter()
    with q.Pdf.open(path) as pdf:
        page = pdf.pages[number]
        try:
            resources = page.Resources
        except Exception:
            resources = q.Dictionary()
        if not isinstance(resources, q.Dictionary):
            resources = q.Dictionary()
        fonts = {}
        font_dict = resources.get('/Font', q.Dictionary())
        if isinstance(font_dict, q.Dictionary):
            for key, value in font_dict.items():
                try:
                    font = read_font(value)
                except Exception:
                    continue
                if font is not None:
                    fonts[str(key)] = font
        forms = 0
        xobjects = resources.get('/XObject', q.Dictionary())
        if isinstance(xobjects, q.Dictionary):
            for obj in xobjects.values():
                try:
                    forms += str(obj.get('/Subtype')) == '/Form'
                except Exception:
                    pass
        glyphs = []
        state = Graphics(fill=(0., 0., 0.))
        stack = []
        marked = []
        mark_id = 0
        matrix = None
        inside = False
        explicit = False
        candidates = []
        tainted = False
        paint_epoch = 0
        objects = 0
        try:
            content_streams = streams(page)
        except Exception:
            content_streams = []
        for stream_number, stream in enumerate(content_streams):
            ordinal = -1
            if inside:
                tainted = True
            try:
                instructions = q.parse_content_stream(stream, OPS)
            except Exception:
                # A single malformed stream must not erase text found elsewhere.
                if inside:
                    tainted = True
                inside, candidates = False, []
                continue
            for args, operator in instructions:
                op = str(operator)
                try:
                    if op == 'q':
                        stack.append(state)
                        tainted |= inside
                    elif op == 'Q':
                        state = stack.pop() if stack else Graphics(unsafe=True)
                        tainted |= inside
                    elif op == 'cm' and len(args) == 6:
                        values = tuple(float(value) for value in args)
                        if all(isfinite(value) for value in values):
                            state = replace(state, ctm=compose(state.ctm, values))
                        else:
                            state = replace(state, unsafe=True)
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
                            safe = (not tainted and len(candidates) == 1 and
                                    glyph.scope == tuple(marked) and glyph.isolated)
                            reason = glyph.reason
                            if not safe and not reason:
                                reason = 'unsupported text structure'
                            glyphs.append(replace(glyph, isolated=safe, reason=reason))
                        inside, candidates = False, []
                    elif op == 'Tf' and len(args) == 2:
                        state = replace(state, font=str(args[0]), size=float(args[1]))
                    elif op in STYLES and len(args) == 1:
                        state = replace(state, **{STYLES[op]: float(args[0])})
                    elif op == 'Tm' and inside and len(args) == 6:
                        matrix, explicit = tuple(float(value) for value in args), True
                    elif op in ('Td', 'TD', 'T*'):
                        explicit = False
                        tainted |= inside
                    elif op in ('rg', 'g', 'k'):
                        space = {'rg': '/DeviceRGB', 'g': '/DeviceGray', 'k': '/DeviceCMYK'}[op]
                        color = _color(args, space)
                        state = replace(state, fill=color, color_space=space,
                                        unsafe_color=color is None or state.unsafe_color)
                    elif op in ('RG', 'G', 'K'):
                        pass
                    elif op in ('cs', 'CS'):
                        space = str(args[0]) if len(args) == 1 else ''
                        device = space in DEVICE_SPACES
                        state = replace(state, color_space=space, unsafe_color=not device,
                                        fill=state.fill if device and space == state.color_space else None)
                    elif op in ('sc', 'scn', 'SC', 'SCN'):
                        color = _color(args, state.color_space)
                        state = replace(state, fill=color if color is not None else state.fill,
                                        unsafe_color=color is None or state.unsafe_color)
                    elif op in ('re',):
                        pass
                    elif op in ('W', 'W*'):
                        mark_id += 1
                        state = replace(state, clip=state.clip+(mark_id,))
                    elif op == 'n':
                        pass
                    elif op in PAINT:
                        paint_epoch += 1
                        tainted |= inside and bool(candidates)
                    elif op in SHOW:
                        ordinal += 1
                        objects += 1
                        font = fonts.get(state.font)
                        if not (inside and explicit and matrix and op == 'Tj' and len(args) == 1 and
                                isinstance(args[0], q.String) and font and 0 < state.size <= 5000):
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
                        if not isfinite(advance) or not all(isfinite(value) for value in shape):
                            explicit = False
                            tainted = True
                            continue
                        left, bottom, right, top = font.bounds
                        bounds = box(shape, (min(0, left)*state.size*horizontal, min(bottom, font.descender)*state.size,
                                             max(0.4*state.size, advance+(max(1, right)-1)*state.size*horizontal),
                                             max(top, font.ascender)*state.size))
                        reasons = []
                        if state.unsafe:
                            reasons.append('unsupported graphics state')
                        if state.clip:
                            reasons.append('clipped text')
                        if state.unsafe_color or state.color_space not in DEVICE_SPACES:
                            reasons.append('non-device color space')
                        if state.fill is None:
                            reasons.append('unknown fill color')
                        if state.mode != 0:
                            reasons.append('text render mode')
                        if not .05 <= horizontal <= 10:
                            reasons.append('horizontal scaling')
                        if state.rise or state.spacing or state.word_space:
                            reasons.append('text spacing')
                        if similarity(state.ctm) is None or text_transform(shape) is None:
                            reasons.append('unsupported transform')
                        candidates.append(Glyph(stream_number, ordinal, text, data, state.font, matrix,
                                                state.ctm, state.size, advance, horizontal, bounds, tuple(marked),
                                                not reasons, state.fill, bool(state.clip), '; '.join(reasons),
                                                paint_epoch))
                        explicit = False
                except (ValueError, TypeError, OverflowError, ZeroDivisionError):
                    state = replace(state, unsafe=True)
                    tainted |= inside
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
        # Replacement runs are appended to the page top layer, so content order no
        # longer matches reading order. Number stable visible runs by position.
        runs.sort(key=lambda run: (-run.placement[1], run.placement[0]))
        runs = [replace(run, id=number) for number, run in enumerate(runs)]
        return PageIndex(runs, objects, forms, perf_counter()-start)


def append(runs, glyphs, fonts, spaces):
    if not ''.join(g.text for g in glyphs).strip():
        return
    reason = next((g.reason for g in glyphs if g.reason), '') if not all(g.isolated for g in glyphs) else ''
    runs.append(TextRun(len(runs), tuple(glyphs), fonts[glyphs[0].font_key],
                        union(g.bounds for g in glyphs), tuple(spaces), reason))
