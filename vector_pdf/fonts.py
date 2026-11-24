from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
from math import isfinite
from pathlib import Path
import re
import os
from tempfile import NamedTemporaryFile

from fontTools import subset
from fontTools.ttLib import TTFont
import pikepdf as q

ASSETS = Path(__file__).with_name('assets')
FALLBACKS = {
    'ISO 3098': 'osifont.ttf',
    'Sans': 'DejaVuSans.ttf',
    'Serif': 'DejaVuSerif.ttf',
    'Mono': 'DejaVuSansMono.ttf',
}

def font_directory():
    if os.name == 'nt':
        return Path(os.environ['LOCALAPPDATA']) / 'VectorPDF' / 'fonts'
    return Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local' / 'share')) / 'vector-pdf' / 'fonts'


def learn_fonts():
    for path in sorted(font_directory().glob('*.ttf')):
        FALLBACKS['Local · '+path.stem] = path


def import_font(path):
    path = Path(path)
    if path.suffix.lower() != '.ttf' or path.stat().st_size > 25*1024**2:
        raise ValueError('Choose a TrueType .ttf file no larger than 25 MiB.')
    with TTFont(path) as font:
        if 'glyf' not in font or not font.getBestCmap():
            raise ValueError('The file needs TrueType outlines and a Unicode character map.')
        flags = font['OS/2'].fsType if 'OS/2' in font else 0
        if flags & (0x0002 | 0x0100 | 0x0200) or (flags & 0x0004 and not flags & 0x0008):
            raise ValueError('This font’s embedding permissions prohibit editable subsets.')
    directory = font_directory()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (path.stem+'.ttf')
    payload = path.read_bytes()
    if target.exists() and target.read_bytes() != payload:
        target = directory / (path.stem+'-'+sha256(payload).hexdigest()[:8]+'.ttf')
    with NamedTemporaryFile(dir=directory, delete=False) as out:
        temp = Path(out.name)
        out.write(payload)
    try:
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    key = 'Local · '+target.stem
    FALLBACKS[key] = target
    return key

_HEX = re.compile(rb'<([0-9A-Fa-f]+)>')
_BLOCKS = re.compile(rb'\b(beginbfchar|beginbfrange)\b(.*?)\bendbf(?:char|range)\b', re.S)
_RANGE = re.compile(rb'<([\da-fA-F]+)>\s*<([\da-fA-F]+)>\s*(<[^>]+>|\[[^\]]*\])')

MAX_CMAP_BYTES = 8*1024**2
MAX_CMAP_BLOCKS = 4096
MAX_CMAP_ENTRIES = 65536
MAX_PROGRAM_BYTES = 32*1024**2


def _hex_bytes(token):
    try:
        return bytes.fromhex(token.decode('ascii'))
    except (ValueError, UnicodeDecodeError):
        return None


def cmap(data):
    lookup = {}
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_CMAP_BYTES:
        return lookup
    blocks = 0
    for match in _BLOCKS.finditer(data):
        blocks += 1
        if blocks > MAX_CMAP_BLOCKS or len(lookup) >= MAX_CMAP_ENTRIES:
            break
        kind, block = match.group(1), match.group(2)
        if kind == b'beginbfchar':
            tokens = _HEX.findall(block)
            for index in range(0, len(tokens)-1, 2):
                if len(lookup) >= MAX_CMAP_ENTRIES:
                    break
                key, value = _hex_bytes(tokens[index]), _hex_bytes(tokens[index+1])
                if not key or not value or len(key) > 4 or len(value) > 4:
                    continue
                try:
                    lookup[key] = value.decode('utf-16-be')
                except (UnicodeDecodeError, ValueError, OverflowError):
                    continue
        else:
            ranges = 0
            for match in _RANGE.finditer(block):
                ranges += 1
                if ranges > MAX_CMAP_BLOCKS or len(lookup) >= MAX_CMAP_ENTRIES:
                    break
                a, b, dst = match.groups()
                try:
                    first, last = int(a, 16), int(b, 16)
                except ValueError:
                    continue
                count = last-first+1
                if count <= 0 or count > 65535:
                    continue
                width = len(a)//2
                if not 1 <= width <= 4:
                    continue
                targets = _HEX.findall(dst)
                if dst.startswith(b'['):
                    for offset in range(count):
                        if offset >= len(targets) or len(lookup) >= MAX_CMAP_ENTRIES:
                            break
                        value = _hex_bytes(targets[offset])
                        if not value or len(value) > 4:
                            continue
                        try:
                            lookup[(first+offset).to_bytes(width, 'big')] = value.decode('utf-16-be')
                        except (UnicodeDecodeError, ValueError, OverflowError):
                            continue
                else:
                    if not targets:
                        continue
                    raw = _hex_bytes(targets[0])
                    if not raw or len(raw) > 4:
                        continue
                    base = int.from_bytes(raw, 'big')
                    for offset in range(count):
                        if len(lookup) >= MAX_CMAP_ENTRIES:
                            break
                        try:
                            value = (base+offset).to_bytes(len(raw), 'big')
                            text = value.decode('utf-16-be')
                            key = (first+offset).to_bytes(width, 'big')
                        except (UnicodeDecodeError, ValueError, OverflowError):
                            continue
                        lookup[key] = text
    return lookup


@dataclass
class Font:
    name: str
    mapping: dict
    widths: dict
    default: float
    unit: int
    bounds: tuple
    embedded: bool
    descender: float = -0.25
    ascender: float = 0.9
    italic_angle: float = 0.
    space_width: float = .25
    trusted: bool = False
    unicode_cmap: frozenset | None = None
    num_glyphs: int | None = None
    cid_to_gid: bytes | None = None
    cid_identity: bool = False
    _reverse: dict | None = field(default=None, init=False, repr=False, compare=False)
    _available: dict | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self):
        self._reverse = None
        self._available = {}

    def decode(self, raw):
        if len(raw) % self.unit:
            return None
        chars = [self.mapping.get(raw[i:i+self.unit]) for i in range(0, len(raw), self.unit)]
        return ''.join(chars) if chars and all(c is not None for c in chars) else None

    def _reverse_map(self):
        if self._reverse is None:
            reverse = {}
            for code, value in self.mapping.items():
                if len(value) == 1 and len(code) == self.unit:
                    reverse[value] = code
            self._reverse = reverse
        return self._reverse

    def _glyph_available(self, code):
        if self.unit == 2:
            if self.num_glyphs is None:
                return False
            cid = int.from_bytes(code, 'big')
            if self.cid_identity:
                gid = cid
            elif self.cid_to_gid is not None:
                if cid < 0 or cid*2+2 > len(self.cid_to_gid):
                    return False
                gid = int.from_bytes(self.cid_to_gid[cid*2:cid*2+2], 'big')
            else:
                return False
            return 0 < gid < self.num_glyphs
        if self.unicode_cmap is None:
            return False
        value = self.mapping.get(code)
        if not value or len(value) != 1 or ord(value) not in self.unicode_cmap:
            return False
        try:
            return code.decode('cp1252') == value
        except UnicodeDecodeError:
            return False

    def encode(self, text):
        if not text:
            return b''
        if not (self.trusted or self.embedded):
            return None
        reverse = self._reverse_map()
        result = []
        for char in text:
            code = reverse.get(char)
            if code is None:
                return None
            if self.unit == 1:
                advance = self.widths.get(int.from_bytes(code, 'big'))
                if advance is None or advance <= 0:
                    return None
            if not self.trusted:
                checked = self._available.get(code)
                if checked is None:
                    checked = self._glyph_available(code)
                    self._available[code] = checked
                if not checked:
                    return None
            result.append(code)
        return b''.join(result)

    def width(self, raw):
        total = 0.
        for index in range(0, len(raw), self.unit):
            total += self.widths.get(int.from_bytes(raw[index:index+self.unit], 'big'), self.default)
        return total/1000


def _number(value, default):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if isfinite(result) else default


def _rectangle(descriptor):
    default = (-150., -350., 1250., 1100.)
    values = []
    try:
        values = [float(item) for item in descriptor.get('/FontBBox', default)]
    except (TypeError, ValueError, OverflowError):
        values = []
    if len(values) != 4 or any(not isfinite(item) for item in values):
        values = list(default)
    return tuple(max(-1e3, min(1e3, item/1000)) for item in values)


def _cmap_bytes(stream):
    try:
        if int(stream.get('/Length', 0) or 0) > MAX_CMAP_BYTES:
            return b''
        return stream.read_bytes()
    except Exception:
        return b''


def _font_program(descriptor):
    for key in ('/FontFile2', '/FontFile3', '/FontFile'):
        stream = descriptor.get(key)
        if stream is None:
            continue
        try:
            if int(stream.get('/Length', 0) or 0) > MAX_PROGRAM_BYTES:
                return None
            payload = stream.read_bytes()
        except Exception:
            return None
        return payload if len(payload) <= MAX_PROGRAM_BYTES else None
    return None


def _simple_cmap(program):
    font = None
    try:
        font = TTFont(BytesIO(program), lazy=False)
        chars = font.getBestCmap()
        return frozenset(chars) if chars else None
    except Exception:
        return None
    finally:
        if font is not None:
            try:
                font.close()
            except Exception:
                pass


def _cid_glyphs(program, descendant):
    font = None
    try:
        font = TTFont(BytesIO(program), lazy=False)
        if 'maxp' not in font:
            return None, None, False
        count = int(font['maxp'].numGlyphs)
        if count <= 0:
            return None, None, False
        source = descendant.get('/CIDToGIDMap')
        if source is None or (isinstance(source, q.Name) and str(source) == '/Identity'):
            return count, None, True
        if isinstance(source, q.Stream):
            if int(source.get('/Length', 0) or 0) > MAX_PROGRAM_BYTES:
                return None, None, False
            raw = source.read_bytes()
            if len(raw) > MAX_PROGRAM_BYTES:
                return None, None, False
            return count, raw, False
        return None, None, False
    except Exception:
        return None, None, False
    finally:
        if font is not None:
            try:
                font.close()
            except Exception:
                pass


def _space_width(mapping, widths, unit):
    code = next((key for key, value in mapping.items() if value == ' ' and len(key) == unit),
                (32).to_bytes(unit, 'big'))
    try:
        raw = widths.get(int.from_bytes(code, 'big'))
        value = float(raw)/1000 if raw is not None else .25
    except (TypeError, ValueError, OverflowError):
        return .25
    return value if isfinite(value) and 0 < value < 10 else .25


def read_font(obj):
    name = str(obj.get('/BaseFont', '/PDF'))[1:].split('+')[-1]
    desc = obj
    cid_identity = False
    num_glyphs = None
    cid_to_gid = None
    if str(obj.get('/Subtype')) == '/Type0':
        if str(obj.get('/Encoding')) != '/Identity-H' or '/ToUnicode' not in obj:
            return None
        desc = obj.DescendantFonts[0]
        if str(desc.get('/Subtype')) != '/CIDFontType2':
            return None
        mapping = {k: v for k, v in cmap(_cmap_bytes(obj.ToUnicode)).items() if len(k) == 2}
        widths = {}
        try:
            iterator = iter(desc.get('/W', []))
            for start, item in zip(iterator, iterator):
                try:
                    first = int(start)
                except (TypeError, ValueError, OverflowError):
                    continue
                if isinstance(item, q.Array):
                    for index, value in enumerate(item):
                        if len(widths) >= MAX_CMAP_ENTRIES:
                            break
                        widths[first+index] = _number(value, 0.)
                else:
                    try:
                        value = float(next(iterator))
                        last = int(item)
                    except (TypeError, ValueError, OverflowError, StopIteration):
                        continue
                    if last < first:
                        continue
                    for code in range(first, min(last, first+MAX_CMAP_ENTRIES)+1):
                        if len(widths) >= MAX_CMAP_ENTRIES:
                            break
                        widths[code] = value
        except (TypeError, ValueError, OverflowError):
            pass
        default, unit = _number(desc.get('/DW', 1000), 1000.), 2
    else:
        if str(obj.get('/Encoding')) != '/WinAnsiEncoding' or str(obj.get('/Subtype')) not in ('/TrueType', '/Type1'):
            return None
        mapping = {}
        for code in range(32, 256):
            try:
                mapping[bytes([code])] = bytes([code]).decode('cp1252')
            except UnicodeDecodeError:
                pass
        if '/ToUnicode' in obj:
            plus = {k: v for k, v in cmap(_cmap_bytes(obj.ToUnicode)).items() if len(k) == 1}
            mapping.update(plus)
        widths = {}
        try:
            first = int(obj.get('/FirstChar', 0))
            for index, value in enumerate(obj.get('/Widths', [])):
                if index >= MAX_CMAP_ENTRIES:
                    break
                widths[first+index] = _number(value, 0.)
        except (TypeError, ValueError, OverflowError):
            pass
        default, unit = 550., 1
    descriptor = desc.get('/FontDescriptor', q.Dictionary())
    if not isinstance(descriptor, q.Dictionary):
        descriptor = q.Dictionary()
    embedded = any(key in descriptor for key in ('/FontFile', '/FontFile2', '/FontFile3'))
    bounds = _rectangle(descriptor)
    unicode_cmap = None
    program = None
    if embedded:
        program = _font_program(descriptor)
        if program is not None:
            if unit == 2:
                num_glyphs, cid_to_gid, cid_identity = _cid_glyphs(program, desc)
            else:
                unicode_cmap = _simple_cmap(program)
    descender = max(-10., min(10., _number(descriptor.get('/Descent', -250), -250.)/1000))
    ascender = max(-10., min(10., _number(descriptor.get('/Ascent', 900), 900.)/1000))
    font = Font(name, mapping, widths, default, unit, bounds, embedded,
                descender, ascender, _number(descriptor.get('/ItalicAngle', 0), 0.))
    font.space_width = _space_width(mapping, widths, unit)
    if embedded and program is not None:
        font.unicode_cmap = unicode_cmap if unit == 1 else None
        font.num_glyphs = num_glyphs
        font.cid_to_gid = cid_to_gid
        font.cid_identity = cid_identity
    return font


def fallback(family, text, pdf=None):
    font = TTFont(ASSETS / FALLBACKS[family], recalcTimestamp=False)
    real_name = font['name'].getDebugName(6) or 'VectorPDF'
    real_name = re.sub('[^a-zA-Z0-9-]', '', real_name) or 'VectorPDF'
    chars = font.getBestCmap()
    if any(ord(c) not in chars for c in text):
        raise ValueError('The selected replacement font does not contain all characters.')
    head, hhea = font['head'], font['hhea']
    italic_angle = font['post'].italicAngle if 'post' in font else 0.
    scale = 1000 / head.unitsPerEm
    space_name = chars.get(32)
    space_width = font['hmtx'][space_name][0]*scale/1000 if space_name else .25
    if not 0 < space_width < 10:
        space_width = .25
    if pdf is not None:
        options = subset.Options()
        options.recalc_timestamp = False
        options.drop_tables += ['FFTM']
        cut = subset.Subsetter(options=options)
        cut.populate(text=text)
        cut.subset(font)
        chars = font.getBestCmap()
    glyphs = {c: font.getGlyphID(chars[ord(c)]).to_bytes(2, 'big') for c in set(text)}
    widths = {int.from_bytes(code, 'big'): round(font['hmtx'][chars[ord(c)]][0]*scale)
              for c, code in glyphs.items()}
    font_bounds = [round(x*scale) for x in (head.xMin, head.yMin, head.xMax, head.yMax)]
    measured = Font(family, {code: char for char, code in glyphs.items()}, widths, 0, 2,
                    tuple(x/1000 for x in font_bounds), True, hhea.descent*scale/1000, hhea.ascent*scale/1000,
                    italic_angle, space_width, True)
    encoded = b''.join(glyphs[c] for c in text)
    if pdf is None:
        font.close()
        return measured, encoded
    payload = BytesIO()
    font.save(payload)
    font.close()
    font_file = pdf.make_stream(payload.getvalue(), Length1=len(payload.getvalue()))
    mark = sha256((real_name+' '+''.join(sorted(glyphs))).encode('utf-8')).digest()[:6]
    prefix = ''.join(chr(65+x%26) for x in mark)
    n = q.Name('/'+prefix+'+'+real_name)
    descriptor = pdf.make_indirect(q.Dictionary(
        Type=q.Name.FontDescriptor, FontName=n, Flags=96 if italic_angle else 32, FontBBox=q.Array(font_bounds),
        ItalicAngle=float(italic_angle), Ascent=round(measured.ascender*1000), Descent=round(measured.descender*1000),
        CapHeight=round(measured.ascender*1000), StemV=80, FontFile2=font_file))
    entries = q.Array()
    for gid in sorted(widths):
        entries.extend([gid, q.Array([widths[gid]])])
    descendant = pdf.make_indirect(q.Dictionary(
        Type=q.Name.Font, Subtype=q.Name.CIDFontType2, BaseFont=n,
        CIDSystemInfo=q.Dictionary(Registry='Adobe', Ordering='Identity', Supplement=0),
        FontDescriptor=descriptor, W=entries, CIDToGIDMap=q.Name.Identity))
    pairs = [f'<{code.hex()}> <{c.encode("utf-16-be").hex()}>' for c, code in sorted(glyphs.items())]
    sections = [str(len(pairs[i:i+100]))+' beginbfchar\n'+'\n'.join(pairs[i:i+100])+'\nendbfchar'
                for i in range(0, len(pairs), 100)]
    unicode_map = pdf.make_stream(('/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n'
        '/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n'
        '/CMapName /VectorPDF def\n/CMapType 2 def\n1 begincodespacerange\n<0000> <ffff>\n'
        'endcodespacerange\n'+'\n'.join(sections)+'\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n').encode())
    resource = pdf.make_indirect(q.Dictionary(Type=q.Name.Font, Subtype=q.Name.Type0, BaseFont=n,
        Encoding=q.Name('/Identity-H'),
        DescendantFonts=q.Array([descendant]), ToUnicode=unicode_map))
    return resource, measured, encoded
