from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
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


def cmap(data):
    lookup = {}
    for kind, block in _BLOCKS.findall(data):
        if kind == b'beginbfchar':
            tokens = _HEX.findall(block)
            pairs = zip(tokens[::2], tokens[1::2])
            for a, b in pairs:
                lookup[bytes.fromhex(a.decode())] = bytes.fromhex(b.decode()).decode('utf-16-be')
        else:
            for a, b, dst in _RANGE.findall(block):
                first, last = int(a, 16), int(b, 16)
                if last < first or last-first > 65535:
                    continue
                targets = _HEX.findall(dst)
                for offset, cid in enumerate(range(first, last+1)):
                    if dst.startswith(b'['):
                        if offset >= len(targets):
                            break
                        value = bytes.fromhex(targets[offset].decode())
                    else:
                        raw = bytes.fromhex(targets[0].decode())
                        value = (int.from_bytes(raw, 'big')+offset).to_bytes(len(raw), 'big')
                    lookup[cid.to_bytes(len(a)//2, 'big')] = value.decode('utf-16-be')
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

    def decode(self, raw):
        if len(raw) % self.unit:
            return None
        chars = [self.mapping.get(raw[i:i+self.unit]) for i in range(0, len(raw), self.unit)]
        return ''.join(chars) if chars and all(c is not None for c in chars) else None

    def encode(self, text):
        reverse = {value: key for key, value in self.mapping.items() if len(value) == 1}
        if not self.embedded or any(c not in reverse or (self.unit == 1 and
            int.from_bytes(reverse[c], 'big') not in self.widths) for c in text):
            return None
        return b''.join(reverse[c] for c in text)

    def width(self, raw):
        return sum(self.widths.get(int.from_bytes(raw[i:i+self.unit], 'big'), self.default)
                   for i in range(0, len(raw), self.unit)) / 1000


def read_font(obj):
    name = str(obj.get('/BaseFont', '/PDF'))[1:].split('+')[-1]
    desc = obj
    if str(obj.get('/Subtype')) == '/Type0':
        if str(obj.get('/Encoding')) != '/Identity-H' or '/ToUnicode' not in obj:
            return None
        desc = obj.DescendantFonts[0]
        if str(desc.get('/Subtype')) != '/CIDFontType2':
            return None
        mapping = {k: v for k, v in cmap(obj.ToUnicode.read_bytes()).items() if len(k) == 2}
        widths = {}
        iterator = iter(desc.get('/W', []))
        for start, item in zip(iterator, iterator):
            if isinstance(item, q.Array):
                widths.update((int(start)+i, float(w)) for i, w in enumerate(item))
            else:
                value = float(next(iterator))
                widths.update((i, value) for i in range(int(start), min(65535, int(item))+1))
        default, unit = float(desc.get('/DW', 1000)), 2
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
            mapping = {k: v for k, v in cmap(obj.ToUnicode.read_bytes()).items() if len(k) == 1}
        widths = {int(obj.get('/FirstChar', 0))+i: float(w) for i, w in enumerate(obj.get('/Widths', []))}
        default, unit = 550., 1
    descriptor = desc.get('/FontDescriptor', q.Dictionary())
    embedded = any(k in descriptor for k in ('/FontFile', '/FontFile2', '/FontFile3'))
    bounds = tuple(float(x)/1000 for x in descriptor.get('/FontBBox', [-150, -350, 1250, 1100]))
    return Font(name, mapping, widths, default, unit, bounds, embedded,
                float(descriptor.get('/Descent', -250))/1000, float(descriptor.get('/Ascent', 900))/1000,
                float(descriptor.get('/ItalicAngle', 0)))


def fallback(family, text, pdf=None):
    font = TTFont(ASSETS / FALLBACKS[family], recalcTimestamp=False)
    real_name = font['name'].getDebugName(6) or 'VectorPDF'
    real_name = re.sub('[^a-zA-Z0-9-]', '', real_name) or 'VectorPDF'
    chars = font.getBestCmap()
    if any(ord(c) not in chars for c in text):
        raise ValueError('The selected replacement font does not contain all characters.')
    if pdf is not None:
        options = subset.Options()
        options.recalc_timestamp = False
        options.drop_tables += ['FFTM']
        cut = subset.Subsetter(options=options)
        cut.populate(text=text)
        cut.subset(font)
        chars = font.getBestCmap()
    head, hhea = font['head'], font['hhea']
    italic_angle = font['post'].italicAngle if 'post' in font else 0.
    scale = 1000 / head.unitsPerEm
    glyphs = {c: font.getGlyphID(chars[ord(c)]).to_bytes(2, 'big') for c in set(text)}
    widths = {int.from_bytes(code, 'big'): round(font['hmtx'][chars[ord(c)]][0]*scale)
              for c, code in glyphs.items()}
    font_bounds = [round(x*scale) for x in (head.xMin, head.yMin, head.xMax, head.yMax)]
    measured = Font(family, {code: char for char, code in glyphs.items()}, widths, 0, 2,
                    tuple(x/1000 for x in font_bounds), True, hhea.descent*scale/1000, hhea.ascent*scale/1000, italic_angle)
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
