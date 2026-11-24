from collections import defaultdict
from dataclasses import dataclass
from math import isfinite, radians, tan
import os
from pathlib import Path
import tempfile
import zlib

import pikepdf as q

from .fonts import FALLBACKS, fallback
from .geometry import box, compose, inverse, translate_angle, union
from .object_index import SHOW, streams

ITALIC_SHEAR = tan(radians(12))


@dataclass(frozen=True)
class Edit:
    page: int
    id: int
    text: str
    x: float
    y: float
    size: float
    angle: float
    color: tuple | None = None
    family: str = 'Original'
    original: object = None
    italic: bool | None = None

    @classmethod
    def from_run(cls, page, run):
        return cls(page, run.id, run.text, *run.placement, original=run)

    def native_codes(self):
        if (not self.original or self.family != 'Original' or self.color is not None or
                abs(self.shear()-self.original.shear) > .001):
            return None
        if any(abs(a-b) > .001 for a, b in zip((self.x, self.y, self.size, self.angle), self.original.placement)):
            return None
        if (len(self.text) != len(self.original.text) or self.original.spaces or
                any(g.font_key != self.original.glyphs[0].font_key for g in self.original.glyphs)):
            return None
        result = []
        position = 0
        for glyph in self.original.glyphs:
            txt = self.text[position:position+len(glyph.text)]
            code = glyph.data if txt == glyph.text else self.original.font.encode(txt)
            if code is None:
                return None
            result.append(code)
            position += len(glyph.text)
        return result

    def current_font(self):
        if self.family in FALLBACKS:
            return self.family
        if self.original and self.original.font.encode(self.text) is not None:
            return 'Original'
        return 'ISO 3098' if self.original and 'gost' in self.original.font.name.lower() else 'Sans'

    def native_italic(self):
        return (self.current_font() == 'Original' and
                bool(self.original.font.italic_angle))

    def shear(self):
        if self.italic is None and self.original:
            if abs(self.original.shear) > .001:
                return self.original.shear
            if self.original.font.italic_angle and not self.native_italic():
                return ITALIC_SHEAR
        return ITALIC_SHEAR if self.italic and not self.native_italic() else 0.

    def is_italic(self):
        return self.native_italic() or abs(self.shear()) > .001

    def matrix(self):
        return translate_angle(self.x, self.y, self.angle, shear=self.shear())

    def bounds(self):
        if not self.text:
            return self.original.bounds if self.original else (self.x, self.y, self.x, self.y)
        codes = self.native_codes()
        if codes is not None:
            font = self.original.font
            return union(text_bounds(font, code, g.size, compose(g.ctm, g.matrix), g.horizontal)
                         for code, g in zip(codes, self.original.glyphs))
        family = self.current_font()
        if family == 'Original':
            font, data = self.original.font, self.original.font.encode(self.text)
        else:
            font, data = fallback(family, self.text)
        horizontal = self.original.glyphs[0].horizontal if self.original else 1.
        return text_bounds(font, data, self.size, self.matrix(), horizontal)


def text_bounds(font, data, size, matrix, horizontal):
    l, b, r, t = font.bounds
    return box(matrix, (min(0, l)*size*horizontal, min(b, font.descender)*size,
                        (font.width(data)+max(.15, r-1))*size*horizontal,
                        max(t, font.ascender)*size))


@dataclass
class Location:
    position: int
    end: int
    data: bytes | None
    after: int | None = None


class Offsets(q._core.StreamParser):
    def __init__(self, targets):
        super().__init__()
        self.targets = set(targets)
        self.last = None
        self.ordinal = -1
        self.open = None
        self.output = {}

    def handle_object(self, obj, offset, length):
        if isinstance(obj, q.Operator):
            op = str(obj)
            if op in SHOW:
                self.ordinal += 1
                if self.ordinal in self.targets:
                    if op != 'Tj' or self.last is None:
                        raise ValueError('Unsupported text operand.')
                    self.output[self.ordinal] = self.open = Location(*self.last)
            elif op == 'ET' and self.open:
                self.open.after = offset + length
                self.open = None
        self.last = (offset, offset+length, bytes(obj)) if isinstance(obj, q.String) else None

    def handle_eof(self):
        pass


def number(value):
    if not isfinite(value) or abs(value) > 1e8:
        raise ValueError('Invalid numeric value.')
    return format(float(value), '.8f').rstrip('0').rstrip('.') or '0'


def op(numbers, operator):
    return (' '.join(map(number, numbers))+' '+operator+'\n').encode('ascii')


def text_operator(data):
    return b'<' + data.hex().encode() + b'>'


def new_run(edit, resource, data, matrix, font_size, added=False, horizontal=1.):
    color = edit.color if edit.color is not None else ((0., 0., 0.) if added else None)
    return (b'\nq\n' + (op(color, 'rg') if color is not None else b'') + b'BT\n'
            + resource.encode('ascii') + b' ' + op([font_size], 'Tf') +
            b'0 Tc 0 Tw '+op([horizontal*100], 'Tz')+b'0 Ts 0 Tr\n' + op(matrix, 'Tm') + text_operator(data) + b' Tj\nET\nQ\n')


def validate(pdf):
    if pdf.is_encrypted:
        raise ValueError('Editing encrypted PDFs is disabled.')
    if '/Perms' in pdf.Root or any(isinstance(o, q.Dictionary) and
        ('/ByteRange' in o or str(o.get('/FT', '')) == '/Sig') for o in pdf.objects):
        raise ValueError('Editing can invalidate signatures. Signed PDFs are read-only.')


def validate_edit(edit):
    if edit.original and not edit.original.editable:
        raise ValueError('This text object is read-only.')
    if (len(edit.text) > 1500 or any(not c.isprintable() for c in edit.text)
            or not .25 <= edit.size <= 4000 or not all(isfinite(x) for x in (edit.x, edit.y, edit.angle))):
        raise ValueError('Use a single printable line of at most 1500 characters and a valid size.')
    if edit.color is not None and (len(edit.color) != 3 or any(not 0 <= v <= 1 for v in edit.color)):
        raise ValueError('Invalid color.')
    if edit.family not in {*FALLBACKS, 'Original'}:
        raise ValueError('Unknown font.')


def bind_font(pdf, resource_fonts, family, edit):
    if family == 'Original':
        return edit.original.glyphs[0].font_key, edit.original.font.encode(edit.text)
    new_font, _, encoded = fallback(family, edit.text, pdf=pdf)
    token = 1
    while f'/VPFont{token}' in resource_fonts:
        token += 1
    key = f'/VPFont{token}'
    resource_fonts[key] = new_font
    return key, encoded


def rebuild(pdf, page, edits):
    content = streams(page)
    resource_copy = q.Dictionary({str(k): v for k, v in page.Resources.items()})
    resource_fonts = q.Dictionary({str(k): v for k, v in resource_copy.get('/Font', q.Dictionary()).items()})
    resources_changed = False
    plan = defaultdict(list)
    added = []
    for edit in edits:
        validate_edit(edit)
        if not edit.original:
            if not edit.text:
                continue
            family = edit.current_font()
            resource, data = bind_font(pdf, resource_fonts, family, edit)
            resources_changed = True
            added.append(new_run(edit, resource, data, edit.matrix(), edit.size, True))
            continue
        glyphs = edit.original.glyphs
        codes = edit.native_codes()
        extra = None
        if edit.text and codes is None:
            family = edit.current_font()
            resource, data = bind_font(pdf, resource_fonts, family, edit)
            resources_changed |= family != 'Original'
            first = glyphs[0]
            matrix = compose(inverse(first.ctm), edit.matrix())
            extra = new_run(edit, resource, data, matrix, edit.size, horizontal=first.horizontal)
        for i, glyph in enumerate(glyphs):
            code = codes[i] if codes is not None else b''
            if code != glyph.data or (i == 0 and extra):
                plan[glyph.stream].append((glyph, code, extra if i == 0 else None, glyphs if i == 0 and extra else ()))
    for index, jobs in plan.items():
        original = content[index]
        data = original.read_bytes()
        offsets = Offsets([g.ordinal for g, _, _, _ in jobs])
        q.Object._parse_stream(original, offsets)
        changes = []
        for glyph, code, addition, group in jobs:
            location = offsets.output.get(glyph.ordinal)
            if not location or location.data != glyph.data or location.after is None:
                raise ValueError('Text offsets changed. Reopen the source document.')
            if addition:
                # Consolidating a split run preserves order only if other paint operators
                # are absent between the text objects and the marked-content context agrees.
                for left, right in zip(group, group[1:]):
                    gap = data[offsets.output[left.ordinal].after:offsets.output[right.ordinal].position]
                    verify_gap(gap)
                changes.append((location.after, location.after, addition))
            changes.append((location.position, location.end, text_operator(code)))
        patched = splice(data, changes)
        if patched != data:
            target = pdf.make_stream(zlib.compress(patched), Filter=q.Name.FlateDecode)
            for key, value in original.stream_dict.items():
                if str(key) not in ('/Length', '/Filter', '/DecodeParms'):
                    target[str(key)] = value
            content[index] = target
    if added:
        require_balanced_page(page)
        # Isolate the original page's graphics state before appending new objects.
        content = [pdf.make_stream(b'q\n'), *content, pdf.make_stream(b'Q\n'+b''.join(added))]
    if plan or added:
        page.obj.Contents = content[0] if len(content) == 1 else q.Array(content)
    if resources_changed:
        resource_copy.Font = resource_fonts
        page.obj.Resources = resource_copy


def require_balanced_page(page):
    depth = {'q': 0, 'BT': 0, 'BMC': 0}
    closing = {'Q': 'q', 'ET': 'BT', 'EMC': 'BMC'}
    for _, operator in q.parse_content_stream(page, 'q Q BT ET BMC BDC EMC'):
        op = str(operator)
        if op in closing:
            key = closing[op]
            depth[key] -= 1
            if depth[key] < 0:
                break
        else:
            key = 'BMC' if op == 'BDC' else op
            depth[key] += 1
    if any(depth.values()):
        raise ValueError('The original page has unbalanced graphics or text scopes.')


_GAP_ALLOWED = {'Tf', 'BT', 'Tm', 'Tr', 'Tc', 'Tw', 'Tz', 'Ts'}


def verify_gap(data):
    # The next Tj's operand is excluded from the range. Every byte still
    # undergoes QPDF tokenization before the replacement is committed.
    with q.Pdf.new() as scratch:
        temporary = scratch.make_stream(data)
        if any(str(o.operator) not in _GAP_ALLOWED for o in q.parse_content_stream(temporary)):
            raise ValueError('Other PDF drawing operations are interleaved with this text.')


def splice(data, edits):
    output, position = [], 0
    for start, end, replacement in sorted(edits, key=lambda x: (x[0], x[1])):
        if start < position:
            raise ValueError('Overlapping PDF changes.')
        output.extend((data[position:start], replacement))
        position = end
    output.append(data[position:])
    return b''.join(output)


def file_stamp(path):
    info = Path(path).stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def write_copy(source, destination, edits, stamp=None):
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve() or (destination.exists() and os.path.samefile(source, destination)):
        raise ValueError('Choose a separate destination PDF.')
    if stamp is not None and file_stamp(source) != stamp:
        raise ValueError('The source file has changed on disk.')
    fd, tmp = tempfile.mkstemp(suffix='.pdf', prefix='.vector-', dir=destination.parent)
    os.close(fd)
    by_page = defaultdict(list)
    for edit in edits:
        by_page[edit.page].append(edit)
    try:
        with q.Pdf.open(source, attempt_recovery=False) as pdf:
            validate(pdf)
            for number, changes in by_page.items():
                rebuild(pdf, pdf.pages[number], changes)
            pdf.save(tmp, normalize_content=False, compress_streams=False, recompress_flate=False,
                     stream_decode_level=q.StreamDecodeLevel.none, fix_metadata_version=False)
        if stamp is not None and file_stamp(source) != stamp:
            raise ValueError('The source file changed while saving.')
        with open(tmp, 'rb+') as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, destination)
    finally:
        Path(tmp).unlink(missing_ok=True)
