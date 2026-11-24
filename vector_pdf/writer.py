from collections import defaultdict
from dataclasses import dataclass
from math import isfinite, radians, tan
import os
from pathlib import Path
import shutil
import tempfile
import zlib

import pikepdf as q

from .fonts import FALLBACKS, fallback
from .geometry import box, compose, translate_angle, union
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
        glyphs = self.original.glyphs if self.original else ()
        if (not self.original or self.family != 'Original' or
                abs(self.shear()-self.original.shear) > .001):
            return None
        if any(abs(a-b) > .001 for a, b in zip((self.x, self.y, self.size, self.angle), self.original.placement)):
            return None
        if len(glyphs) != 1 or self.original.spaces:
            return None
        if any(g.font_key != glyphs[0].font_key for g in glyphs):
            return None
        if self.color is not None:
            fill = glyphs[0].fill
            if fill is None or any(abs(a-b) > .002 for a, b in zip(self.color, fill)):
                return None
        if len(self.text) != len(self.original.text):
            return None
        result = []
        position = 0
        for glyph in glyphs:
            txt = self.text[position:position+len(glyph.text)]
            code = glyph.data if txt == glyph.text else self.original.font.encode(txt)
            if code is None:
                return None
            result.append(code)
            position += len(glyph.text)
        if sum(len(code) for code in result) != sum(len(glyph.data) for glyph in glyphs):
            return None
        return result

    def fill_color(self):
        if self.color is not None:
            return tuple(self.color)
        if self.original and self.original.glyphs:
            fill = self.original.glyphs[0].fill
            if fill is not None:
                return tuple(fill)
        return (0., 0., 0.)

    def glyph_supported(self):
        if not self.text:
            return True
        try:
            family = self.current_font()
            if family == 'Original':
                return bool(self.original and self.original.font.encode(self.text) is not None)
            fallback(family, self.text)
            return True
        except Exception:
            return False

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
        try:
            result = self._bounds()
            if all(isfinite(value) for value in result):
                return result
        except Exception:
            pass
        width = sum(.55 if char != ' ' else .28 for char in self.text)*self.size
        return box(self.matrix(), (0., -.25*self.size, max(width, .1*self.size), .9*self.size))

    def _bounds(self):
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
            if data is None:
                raise ValueError('The original font cannot render this text.')
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
    color = edit.color if edit.color is not None else (edit.fill_color() if edit.original else (0., 0., 0.))
    return (b'\nq\n' + op(color, 'rg') + b'BT\n'
            + resource.encode('ascii') + b' ' + op([font_size], 'Tf') +
            b'0 Tc 0 Tw '+op([horizontal*100], 'Tz')+b'0 Ts 0 Tr\n' + op(matrix, 'Tm') + text_operator(data) + b' Tj\nET\nQ\n')


def _signature_value(obj):
    if not isinstance(obj, (q.Dictionary, q.Stream)):
        return False
    try:
        if '/ByteRange' in obj and '/Contents' in obj:
            return True
        if str(obj.get('/Type', '')) in ('/Sig', '/DocTimeStamp') and '/ByteRange' in obj:
            return True
    except Exception:
        pass
    return False


def _walk_signatures(obj, seen):
    if not isinstance(obj, (q.Dictionary, q.Stream, q.Array)):
        return False, False
    try:
        generation = obj.objgen
    except Exception:
        generation = (0, 0)
    marker = generation if generation != (0, 0) else ('direct', id(obj))
    if marker in seen:
        return False, False
    # Keep direct objects alive: their Python id can otherwise be reused and a
    # later object would be skipped as already visited.
    seen[marker] = obj
    if isinstance(obj, q.Array):
        signed = empty = False
        for value in obj:
            item_signed, item_empty = _walk_signatures(value, seen)
            signed |= item_signed
            empty |= item_empty
        return signed, empty
    if _signature_value(obj):
        return True, False
    signed = empty = False
    try:
        field_type = str(obj.get('/FT', ''))
    except Exception:
        field_type = ''
    if field_type == '/Sig':
        try:
            value = obj.get('/V')
        except Exception:
            value = None
        if value is None:
            empty = True
        else:
            try:
                if '/ByteRange' in value:
                    signed = True
                elif isinstance(value, (q.Dictionary, q.Stream)):
                    empty = '/ByteRange' not in value
                else:
                    empty = True
            except Exception:
                empty = True
    try:
        perms = obj.get('/Perms')
    except Exception:
        perms = None
    if isinstance(perms, (q.Dictionary, q.Stream)):
        try:
            if not perms:
                empty = True
        except Exception:
            pass
        try:
            docmdp = perms.get('/DocMDP')
            if docmdp is not None and '/ByteRange' in docmdp:
                signed = True
        except Exception:
            pass
    try:
        values = list(obj.values())
    except Exception:
        values = []
    for value in values:
        try:
            item_signed, item_empty = _walk_signatures(value, seen)
        except Exception:
            continue
        signed |= item_signed
        empty |= item_empty
    return signed, empty


def signature_state(pdf):
    try:
        signed, empty = _walk_signatures(pdf.Root, {})
    except Exception:
        return 'signed'
    if signed:
        return 'signed'
    return 'empty' if empty else 'none'


def has_signature(pdf):
    return signature_state(pdf) == 'signed'


def validate(pdf):
    if pdf.is_encrypted:
        raise ValueError('Editing encrypted PDFs is disabled.')
    if has_signature(pdf):
        raise ValueError('Editing can invalidate signatures. Signed PDFs are read-only.')


def validate_edit(edit):
    if edit.original and not edit.original.editable:
        raise ValueError('This text object is read-only.')
    unchanged_text = edit.original is not None and edit.text == edit.original.text
    unchanged_size = edit.original is not None and abs(edit.size-edit.original.placement[2]) < .0005
    too_long = len(edit.text) > 100_000 and not unchanged_text
    bad_size = not 0 < edit.size <= 5000 and not unchanged_size
    if (too_long or any(not c.isprintable() for c in edit.text) or bad_size or
            not all(isfinite(x) for x in (edit.x, edit.y, edit.angle))):
        raise ValueError('Use a single printable line of at most 100000 characters and a size from 0 to 5000 points.')
    if edit.color is not None and (len(edit.color) != 3 or any(not 0 <= v <= 1 for v in edit.color)):
        raise ValueError('Invalid color.')
    if edit.family not in {*FALLBACKS, 'Original'}:
        raise ValueError('Unknown font.')


def bind_font(pdf, resource_fonts, family, edit):
    if family == 'Original':
        encoded = edit.original.font.encode(edit.text)
        if encoded is None:
            raise ValueError('The original font cannot render all characters.')
        return edit.original.glyphs[0].font_key, encoded
    new_font, _, encoded = fallback(family, edit.text, pdf=pdf)
    token = 1
    while f'/VPFont{token}' in resource_fonts:
        token += 1
    key = f'/VPFont{token}'
    resource_fonts[key] = new_font
    return key, encoded


def rebuild(pdf, page, edits):
    content = streams(page)
    try:
        page_resources = page.Resources
    except Exception:
        page_resources = q.Dictionary()
    if not isinstance(page_resources, q.Dictionary):
        page_resources = q.Dictionary()
    resource_copy = q.Dictionary({str(k): v for k, v in page_resources.items()})
    font_object = resource_copy.get('/Font', q.Dictionary())
    if not isinstance(font_object, q.Dictionary):
        font_object = q.Dictionary()
    resource_fonts = q.Dictionary({str(k): v for k, v in font_object.items()})
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
        if codes is not None:
            for code, glyph in zip(codes, glyphs):
                if code != glyph.data:
                    plan[glyph.stream].append((glyph, code))
            continue
        # A replacement can move, resize or restyle the text. Clear every original
        # show operand and append one self-contained run outside the original CTM.
        for glyph in glyphs:
            plan[glyph.stream].append((glyph, b''))
        if not edit.text:
            continue
        family = edit.current_font()
        resource, data = bind_font(pdf, resource_fonts, family, edit)
        resources_changed |= family != 'Original'
        first = glyphs[0]
        added.append(new_run(edit, resource, data, edit.matrix(), edit.size, horizontal=first.horizontal))
    for index, jobs in plan.items():
        original = content[index]
        data = original.read_bytes()
        offsets = Offsets([g.ordinal for g, _ in jobs])
        q.Object._parse_stream(original, offsets)
        changes = []
        for glyph, code in jobs:
            location = offsets.output.get(glyph.ordinal)
            if not location or location.data != glyph.data or location.after is None:
                raise ValueError('Text offsets changed. Reopen the source document.')
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


def _inherited(page, key):
    obj = page.obj
    seen = set()
    while obj is not None:
        try:
            generation = obj.objgen
        except Exception:
            generation = (0, 0)
        marker = generation if generation != (0, 0) else id(obj)
        if marker in seen:
            break
        seen.add(marker)
        try:
            if key in obj:
                return obj.get(key)
        except Exception:
            return None
        try:
            obj = obj.get('/Parent')
        except Exception:
            return None
    return None


def _rectangle_value(value):
    try:
        values = tuple(float(item) for item in value)
    except (TypeError, ValueError, OverflowError):
        return None
    if len(values) != 4 or any(not isfinite(item) for item in values):
        return None
    return values


def _page_geometry(page):
    media = _rectangle_value(_inherited(page, '/MediaBox'))
    crop = _rectangle_value(_inherited(page, '/CropBox'))
    try:
        rotate = int(_inherited(page, '/Rotate') or 0) % 360
    except (TypeError, ValueError, OverflowError):
        rotate = 0
    return media, crop, rotate


def _raw_streams(page):
    try:
        return [stream.read_raw_bytes() for stream in streams(page)]
    except Exception:
        return None


def _normalized_text(value):
    return ''.join(value.split())


def _pdfium_text(document, number):
    page = document[number]
    try:
        textpage = page.get_textpage()
        try:
            return textpage.get_text_bounded()
        finally:
            textpage.close()
    finally:
        page.close()


def _validate_output(path, geometry, untouched, expected):
    try:
        with q.Pdf.open(path, attempt_recovery=False) as output:
            if has_signature(output):
                raise ValueError('The saved PDF would still contain a signature.')
            problems = [problem for problem in output.check_pdf_syntax()
                        if not str(problem).startswith('WARNING')]
            if problems:
                raise ValueError('The saved PDF failed structural validation: ' + '; '.join(map(str, problems[:3])))
            if len(output.pages) != len(geometry):
                raise ValueError('The saved PDF changed the page count.')
            for number, expected_geometry in enumerate(geometry):
                page = output.pages[number]
                if _page_geometry(page) != expected_geometry:
                    raise ValueError(f'The saved PDF changed the page {number+1} box or rotation.')
                if number in untouched:
                    before, after = untouched[number], _raw_streams(page)
                    if before is not None and after is not None and before != after:
                        raise ValueError(f'The saved PDF changed untouched content on page {number+1}.')
        if expected:
            import pypdfium2 as pdfium
            with pdfium.PdfDocument(str(path)) as document:
                for number, texts in expected.items():
                    actual = _normalized_text(_pdfium_text(document, number))
                    for text in texts:
                        needle = _normalized_text(text)
                        if needle and needle not in actual:
                            raise ValueError(f'Edited text is not present in the saved page {number+1}.')
    except ValueError:
        raise
    except Exception as error:
        raise ValueError(f'The saved PDF failed validation: {error}') from error


def _check_output(path):
    try:
        with q.Pdf.open(path, attempt_recovery=False) as output:
            if has_signature(output):
                raise ValueError('The saved PDF would still contain a signature.')
    except ValueError:
        raise
    except Exception as error:
        raise ValueError(f'The saved PDF could not be reopened: {error}') from error


def _same_file(first, second):
    first, second = Path(first), Path(second)
    try:
        if first.resolve() == second.resolve():
            return True
    except OSError:
        pass
    try:
        return first.exists() and second.exists() and os.path.samefile(first, second)
    except OSError:
        return False


def write_copy(source, destination, edits, stamp=None, original_source=None, verify=True):
    source, destination = Path(source), Path(destination)
    if _same_file(source, destination):
        raise ValueError('Choose a separate destination PDF.')
    if original_source is not None and _same_file(original_source, destination):
        raise ValueError('Choose a destination other than the original source PDF.')
    if destination.exists() and not destination.is_file():
        raise ValueError('The destination must be a PDF file.')
    if stamp is not None and file_stamp(source) != stamp:
        raise ValueError('The source file has changed on disk.')
    edits = list(edits)
    fd, tmp = tempfile.mkstemp(suffix='.pdf', prefix='.vector-', dir=destination.parent)
    os.close(fd)
    try:
        with q.Pdf.open(source, attempt_recovery=False) as pdf:
            validate(pdf)
            by_page = defaultdict(list)
            for edit in edits:
                if not 0 <= edit.page < len(pdf.pages):
                    raise ValueError('The edit refers to a page outside this PDF.')
                by_page[edit.page].append(edit)
            geometry = [_page_geometry(page) for page in pdf.pages]
            untouched = {number: _raw_streams(page) for number, page in enumerate(pdf.pages)
                         if number not in by_page}
            for number, changes in by_page.items():
                rebuild(pdf, pdf.pages[number], changes)
            pdf.save(tmp, normalize_content=False, compress_streams=False, recompress_flate=False,
                     stream_decode_level=q.StreamDecodeLevel.none, fix_metadata_version=False)
        if stamp is not None and file_stamp(source) != stamp:
            raise ValueError('The source file changed while saving.')
        if verify:
            expected = defaultdict(list)
            for edit in edits:
                if edit.text and edit.text.strip():
                    expected[edit.page].append(edit.text)
            _validate_output(tmp, geometry, untouched, expected)
        else:
            _check_output(tmp)
        if stamp is not None and file_stamp(source) != stamp:
            raise ValueError('The source file changed while saving.')
        with open(tmp, 'rb+') as handle:
            os.fsync(handle.fileno())
        if destination.exists():
            backup = Path(str(destination)+'.bak')
            if _same_file(backup, source) or (original_source is not None and _same_file(backup, original_source)):
                raise ValueError('The backup path would overwrite the source PDF.')
            try:
                shutil.copyfile(destination, backup)
            except OSError as error:
                raise ValueError(f'Could not create the backup copy: {error}') from error
        os.replace(tmp, destination)
    finally:
        Path(tmp).unlink(missing_ok=True)
