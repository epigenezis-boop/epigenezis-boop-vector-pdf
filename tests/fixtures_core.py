from io import BytesIO
from math import ceil
from pathlib import Path

import pikepdf as q
import pypdfium2 as pdfium
from fontTools import subset
from fontTools.ttLib import TTFont

from vector_pdf.fonts import ASSETS, FALLBACKS, fallback
from vector_pdf.geometry import translate_angle
from vector_pdf.writer import Edit, new_run


def minimal_pdf(path, content=b'', size=(300, 200), resources=None):
    with q.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=size)
        page.Contents = pdf.make_stream(content)
        if resources is not None:
            page.Resources = resources
        pdf.save(path)
    return Path(path)


def subset_program(text, family='Sans'):
    font = TTFont(ASSETS / FALLBACKS[family], recalcTimestamp=False)
    options = subset.Options()
    options.recalc_timestamp = False
    options.drop_tables += ['FFTM']
    cut = subset.Subsetter(options=options)
    cut.populate(text=text)
    cut.subset(font)
    chars = font.getBestCmap()
    scale = 1000 / font['head'].unitsPerEm
    glyphs = {char: font.getGlyphID(chars[ord(char)]) for char in text}
    widths = {ord(char): round(font['hmtx'][chars[ord(char)]][0]*scale) for char in text}
    payload = BytesIO()
    font.save(payload)
    font.close()
    return payload.getvalue(), glyphs, widths


def cmap_stream(pairs, unit=1):
    lines = [f'<{code.to_bytes(unit, "big").hex()}> <{char.encode("utf-16-be").hex()}>'
             for code, char in sorted(pairs.items())]
    return ('/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n'
            '/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n'
            '/CMapName /VectorTest def\n/CMapType 2 def\n'
            f'1 begincodespacerange\n<{(0).to_bytes(unit, "big").hex()}> '
            f'<{(2**(8*unit)-1).to_bytes(unit, "big").hex()}>\nendcodespacerange\n'
            f'{len(lines)} beginbfchar\n' + '\n'.join(lines) + '\nendbfchar\n'
            'endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n').encode()


def simple_font(pdf, subset_text='AB', map_text=None, bbox=None, name='TestFont'):
    map_text = subset_text if map_text is None else map_text
    program, _, widths = subset_program(subset_text)
    metrics = [600]*256
    for code, value in widths.items():
        metrics[code] = value
    descriptor = pdf.make_indirect(q.Dictionary(
        Type=q.Name.FontDescriptor, FontName=q.Name('/AAAAAA+'+name), Flags=32,
        FontBBox=q.Array(bbox if bbox is not None else [-1000, -300, 3000, 1100]),
        ItalicAngle=0, Ascent=900, Descent=-250, CapHeight=900, StemV=80,
        FontFile2=pdf.make_stream(program)))
    pairs = {ord(char): char for char in map_text}
    font = pdf.make_indirect(q.Dictionary(
        Type=q.Name.Font, Subtype=q.Name.TrueType, BaseFont=q.Name('/AAAAAA+'+name),
        FirstChar=0, LastChar=255, Widths=q.Array(metrics), FontDescriptor=descriptor,
        Encoding=q.Name.WinAnsiEncoding, ToUnicode=pdf.make_stream(cmap_stream(pairs))))
    return font


def text_pdf(path, text, size=(300, 200), x=50, y=100, font_size=20):
    with q.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=size)
        font = simple_font(pdf, subset_text=text, map_text=text)
        page.Resources = q.Dictionary(Font=q.Dictionary(F0=font))
        data = text.encode('cp1252')
        page.Contents = pdf.make_stream(
            f'BT /F0 {font_size} Tf 1 0 0 1 {x} {y} Tm <{data.hex()}> Tj ET\n'.encode())
        pdf.save(path)
    return Path(path)


def fallback_pdf(path, text='AB', size=(300, 200), x=50, y=100, font_size=20,
                 prefix=b'', suffix=b'', color=None):
    with q.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=size)
        font, metrics, _ = fallback('Sans', text, pdf)
        page.Resources = q.Dictionary(Font=q.Dictionary(F0=font))
        body = new_run(Edit(0, -1, text, x, y, font_size, 0, color=color), '/F0',
                       metrics.encode(text), translate_angle(x, y, 0), font_size)
        page.Contents = pdf.make_stream(prefix + body + suffix)
        pdf.save(path)
    return Path(path)


def ink(path, bounds, scale=4, red=False):
    with pdfium.PdfDocument(str(path)) as document:
        page = document[0]
        width, height = page.get_size()
        bitmap = page.render(scale=scale, fill_color=(255, 255, 255, 255))
        try:
            pixels = bitmap.to_numpy()
        finally:
            bitmap.close()
            page.close()
    left, bottom, right, top = bounds
    x0, x1 = max(0, int(left*scale)), min(pixels.shape[1], ceil(right*scale))
    y0, y1 = max(0, int((height-top)*scale)), min(pixels.shape[0], ceil((height-bottom)*scale))
    if x1 <= x0 or y1 <= y0:
        return 0
    crop = pixels[y0:y1, x0:x1, :3]
    if red:
        # pypdfium2 returns BGR on Windows, so red is the third channel here.
        mask = (crop[:, :, 2] > 140) & (crop[:, :, 1] < 120) & (crop[:, :, 0] < 120)
    else:
        mask = crop.min(axis=2) < 128
    return int(mask.sum())


def char_boxes(path):
    with pdfium.PdfDocument(str(path)) as document:
        page = document[0]
        textpage = page.get_textpage()
        try:
            text = textpage.get_text_bounded()
            boxes = [(textpage.get_text_range(index, 1), textpage.get_charbox(index))
                     for index in range(textpage.count_chars())]
        finally:
            textpage.close()
            page.close()
    return text, boxes


def page_text(path, number=0):
    with pdfium.PdfDocument(str(path)) as document:
        page = document[number]
        textpage = page.get_textpage()
        try:
            return textpage.get_text_bounded()
        finally:
            textpage.close()
            page.close()
