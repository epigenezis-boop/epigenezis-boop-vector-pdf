"""Independent QA regressions for the VECTOR PDF 0.2.1 audit findings.

Every test states the behaviour that a hardened 0.3 build must provide.  On the
0.2.1 baseline the documented defects are expected to fail; the baseline table
is recorded in ``QA_BASELINE.md``.

Only synthetic PDFs are used.  No user drawing or private data is referenced.
"""
from __future__ import annotations

from dataclasses import replace
from io import BytesIO
from math import ceil, floor
from pathlib import Path
import inspect

import numpy as np
import pikepdf as q
import pypdfium2 as pdfium
from fontTools import subset
from fontTools.ttLib import TTFont
import pytest

from vector_pdf.fonts import ASSETS, fallback
from vector_pdf.object_index import index_page, streams
from vector_pdf.writer import Edit, write_copy

PAGE = (300.0, 200.0)


# ---------------------------------------------------------------------------
# Synthetic PDF helpers


def _subset_font(text: str, source: str = 'DejaVuSans.ttf') -> bytes:
    font = TTFont(ASSETS / source, recalcTimestamp=False)
    options = subset.Options()
    options.recalc_timestamp = False
    options.drop_tables += ['FFTM']
    cutter = subset.Subsetter(options=options)
    cutter.populate(text=text)
    cutter.subset(font)
    payload = BytesIO()
    font.save(payload)
    font.close()
    return payload.getvalue()


def _simple_font(pdf, glyphs: str, tounicode: bytes | None = None, bbox=None):
    """Build a minimal simple TrueType font.

    ``glyphs`` controls the subset actually embedded in the font program.
    Widths, however, come from the full DejaVu source so that a missing glyph
    still has its realistic advance and a distinct render cell.
    """
    payload = _subset_font(glyphs)
    full = TTFont(ASSETS / 'DejaVuSans.ttf', recalcTimestamp=False)
    cmap = full.getBestCmap()
    scale = 1000 / full['head'].unitsPerEm
    widths = []
    for code in range(32, 256):
        char = bytes([code]).decode('cp1252', 'ignore')
        glyph = cmap.get(ord(char)) if len(char) == 1 else None
        widths.append(round(full['hmtx'][glyph][0] * scale) if glyph else 0)
    full.close()

    program = pdf.make_stream(payload, Length1=len(payload))
    descriptor = pdf.make_indirect(q.Dictionary(
        Type=q.Name.FontDescriptor, FontName=q.Name('/AAAAAA+DejaVuSans'),
        Flags=32, FontBBox=q.Array(bbox or [-100, -300, 1200, 1000]),
        ItalicAngle=0, Ascent=900, Descent=-300, CapHeight=700, StemV=80,
        FontFile2=program))
    font = pdf.make_indirect(q.Dictionary(
        Type=q.Name.Font, Subtype=q.Name.TrueType,
        BaseFont=q.Name('/AAAAAA+DejaVuSans'), FirstChar=32, LastChar=255,
        Widths=q.Array(widths), FontDescriptor=descriptor,
        Encoding=q.Name.WinAnsiEncoding))
    if tounicode is not None:
        font.ToUnicode = pdf.make_stream(tounicode)
    return font


def _tounicode_pairs(pairs) -> bytes:
    lines = [
        '/CIDInit /ProcSet findresource begin', '12 dict begin', 'begincmap',
        '/CMapName /QATest def', '/CMapType 2 def',
        '1 begincodespacerange', '<00> <FF>', 'endcodespacerange',
        f'{len(pairs)} beginbfchar',
    ]
    lines += [f'<{code:02X}> <{target}>' for code, target in pairs]
    lines += ['endbfchar', 'endcmap',
              'CMapName currentdict /CMap defineresource pop', 'end', 'end']
    return ('\n'.join(lines) + '\n').encode()


def _pdf_with_simple_text(path: Path, text: str, glyphs: str | None = None,
                          x: float = 50, y: float = 100, size: float = 12,
                          tounicode: bytes | None = None, bbox=None,
                          extra_content: bytes = b'', page_size=PAGE) -> Path:
    with q.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=page_size)
        font = _simple_font(pdf, glyphs or text, tounicode, bbox)
        page.Resources = q.Dictionary(Font=q.Dictionary(F0=font))
        page.Contents = pdf.make_stream(
            f'BT /F0 {size} Tf 1 0 0 1 {x} {y} Tm ({text}) Tj ET\n'.encode()
            + extra_content)
        pdf.save(path)
    return path


def _render(path: Path, page_number: int = 0, scale: float = 6.0):
    with pdfium.PdfDocument(path) as document:
        page = document[page_number]
        width, height = (int(ceil(value * scale)) for value in page.get_size())
        bitmap = pdfium.PdfBitmap.new_native(
            width, height, pdfium.raw.FPDFBitmap_BGR)
        try:
            bitmap.fill_rect((255, 255, 255, 255), 0, 0, width, height)
            pdfium.raw.FPDF_RenderPageBitmap(
                bitmap, page, 0, 0, width, height, 0,
                pdfium.raw.FPDF_ANNOT | pdfium.raw.FPDF_LCD_TEXT)
            return bitmap.to_numpy().copy()
        finally:
            bitmap.close()
            page.close()


def _ink(image, rect, scale: float) -> int:
    """Count non-white pixels in a PDF-space rectangle (origin bottom-left)."""
    left, bottom, right, top = rect
    height, width = image.shape[:2]
    x0 = max(0, floor(left * scale))
    x1 = min(width, ceil(right * scale))
    y0 = max(0, floor(height - top * scale))
    y1 = min(height, ceil(height - bottom * scale))
    patch = image[y0:y1, x0:x1, :3]
    return int(np.any(patch < 245, axis=2).sum())


def _text_and_boxes(path: Path, page_number: int = 0):
    with pdfium.PdfDocument(path) as document:
        page = document[page_number]
        textpage = page.get_textpage()
        try:
            text = textpage.get_text_bounded()
            boxes = [(textpage.get_text_range(i, 1), textpage.get_charbox(i))
                     for i in range(textpage.count_chars())]
            return text, boxes
        finally:
            textpage.close()
            page.close()


def _editable(run) -> bool:
    return bool(getattr(run, 'editable', True))


# ---------------------------------------------------------------------------
# 1. Embedded subset font must not silently lose a glyph.


def test_subset_font_missing_glyph_still_renders_new_character(tmp_path):
    source = _pdf_with_simple_text(tmp_path / 'subset.pdf', 'AB', glyphs='AB')
    runs = index_page(source, 0).runs
    assert [run.text for run in runs] == ['AB']

    run = runs[0]
    destination = tmp_path / 'subset-out.pdf'
    write_copy(source, destination, [replace(Edit.from_run(0, run), text='CB')])

    # Extraction alone is not evidence: PDFium reports "CB" even when C has no
    # glyph in the embedded program.  The C cell must contain real ink.
    text, boxes = _text_and_boxes(destination)
    assert 'CB' in text
    image = _render(destination, scale=10)
    # C occupies roughly 50..58.4 pt; stop at 56 pt so B is not counted.
    c_ink = _ink(image, (50, 99, 56, 111), 10)
    b_ink = _ink(image, (59, 99, 65.5, 111), 10)
    assert c_ink > 20, 'new C glyph is blank in the rendered PDF'
    assert b_ink > 20, 'control B glyph is missing'


# ---------------------------------------------------------------------------
# 2. A run split over several Tj/BT blocks must not overlap after replacement.


def _split_ab_pdf(path: Path) -> Path:
    with q.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=PAGE)
        font = _simple_font(pdf, 'ABW')
        page.Resources = q.Dictionary(Font=q.Dictionary(F0=font))
        page.Contents = pdf.make_stream(
            b'BT /F0 12 Tf 1 0 0 1 50 100 Tm (A) Tj ET\n'
            b'BT /F0 12 Tf 1 0 0 1 58.209 100 Tm (B) Tj ET\n')
        pdf.save(path)
    return path


def test_split_tj_replacement_does_not_overlap(tmp_path):
    source = _split_ab_pdf(tmp_path / 'split.pdf')
    runs = index_page(source, 0).runs
    assert [run.text for run in runs] == ['AB']
    run = runs[0]
    if not _editable(run):
        return  # A conservative read-only policy is also safe.

    destination = tmp_path / 'split-out.pdf'
    write_copy(source, destination, [replace(Edit.from_run(0, run), text='WB')])
    text, boxes = _text_and_boxes(destination)
    assert 'WB' in text.replace(' ', '')
    glyphs = [(char, box) for char, box in boxes if char in 'WB']
    assert len(glyphs) >= 2
    white = next(box for char, box in glyphs if char == 'W')
    black = next(box for char, box in glyphs if char == 'B')
    overlap = white[2] - black[0]
    assert overlap <= 0.25, (
        f'replaced glyph cells overlap by {overlap:.2f} pt: W={white}, B={black}')


# ---------------------------------------------------------------------------
# 3. Signatures: direct/nested/array must be detected; empty structures must not.


def _signature_dict():
    return q.Dictionary(Type=q.Name.Sig, ByteRange=q.Array([0, 10, 20, 30]),
                        Contents=q.String(b'not-a-real-signature'))


def _assert_signed_rejected(source: Path, destination: Path):
    destination.write_bytes(b'previous revision')
    with pytest.raises(ValueError, match='(?i)sign|read-only'):
        write_copy(source, destination, [])
    assert destination.read_bytes() == b'previous revision'


def test_direct_signature_dictionary_is_rejected(tmp_path):
    source = _pdf_with_simple_text(tmp_path / 'clean.pdf', 'AB')
    signed = tmp_path / 'direct-sig.pdf'
    with q.Pdf.open(source) as pdf:
        pdf.Root.MySig = _signature_dict()
        pdf.save(signed)
    _assert_signed_rejected(signed, tmp_path / 'out.pdf')


def test_nested_direct_signature_in_field_is_rejected(tmp_path):
    source = _pdf_with_simple_text(tmp_path / 'clean.pdf', 'AB')
    signed = tmp_path / 'nested-sig.pdf'
    with q.Pdf.open(source) as pdf:
        field = q.Dictionary(
            Type=q.Name.Annot, Subtype=q.Name.Widget, FT=q.Name.Sig,
            T=q.String('Signature1'), V=_signature_dict())
        pdf.Root.AcroForm = pdf.make_indirect(
            q.Dictionary(Fields=q.Array([field])))
        pdf.save(signed)
    _assert_signed_rejected(signed, tmp_path / 'out.pdf')


def test_direct_signature_in_array_is_rejected(tmp_path):
    source = _pdf_with_simple_text(tmp_path / 'clean.pdf', 'AB')
    signed = tmp_path / 'array-sig.pdf'
    with q.Pdf.open(source) as pdf:
        pdf.Root.MySigs = q.Array([_signature_dict()])
        pdf.save(signed)
    _assert_signed_rejected(signed, tmp_path / 'out.pdf')


def test_empty_signature_field_does_not_block_editing(tmp_path):
    source = _pdf_with_simple_text(tmp_path / 'clean.pdf', 'AB')
    field_pdf = tmp_path / 'empty-field.pdf'
    with q.Pdf.open(source) as pdf:
        field = pdf.make_indirect(q.Dictionary(
            Type=q.Name.Annot, Subtype=q.Name.Widget, FT=q.Name.Sig,
            T=q.String('Signature1')))
        pdf.Root.AcroForm = q.Dictionary(Fields=q.Array([field]))
        pdf.save(field_pdf)

    run = index_page(field_pdf, 0).runs[0]
    destination = tmp_path / 'edited.pdf'
    write_copy(field_pdf, destination,
               [replace(Edit.from_run(0, run), text='CD')])
    assert 'CD' in _text_and_boxes(destination)[0]
    with q.Pdf.open(destination) as pdf:
        assert pdf.check_pdf_syntax() == []


def test_empty_perms_does_not_block_editing(tmp_path):
    source = _pdf_with_simple_text(tmp_path / 'clean.pdf', 'AB')
    perms_pdf = tmp_path / 'empty-perms.pdf'
    with q.Pdf.open(source) as pdf:
        pdf.Root.Perms = q.Dictionary()
        pdf.save(perms_pdf)

    run = index_page(perms_pdf, 0).runs[0]
    destination = tmp_path / 'edited.pdf'
    write_copy(perms_pdf, destination,
               [replace(Edit.from_run(0, run), text='CD')])
    assert 'CD' in _text_and_boxes(destination)[0]


# ---------------------------------------------------------------------------
# 4. Clip context: moving text out of a clip must not make it silently invisible.


def test_moving_text_outside_clip_is_safe(tmp_path):
    source = tmp_path / 'clip.pdf'
    with q.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=PAGE)
        font = _simple_font(pdf, 'AB')
        page.Resources = q.Dictionary(Font=q.Dictionary(F0=font))
        page.Contents = pdf.make_stream(
            b'q\n10 10 100 20 re W n\n'
            b'BT /F0 12 Tf 1 0 0 1 20 15 Tm (AB) Tj ET\nQ\n')
        pdf.save(source)

    run = index_page(source, 0).runs[0]
    assert run.text == 'AB'
    if not _editable(run):
        return  # A read-only clip-aware policy is safe.

    destination = tmp_path / 'clip-out.pdf'
    write_copy(source, destination, [replace(Edit.from_run(0, run), y=80)])
    image = _render(destination, scale=5)
    new_ink = _ink(image, (18, 78, 48, 93), 5)
    assert new_ink > 20, (
        'moved text is outside the clip and produced no visible ink')


# ---------------------------------------------------------------------------
# 5. Fill colour must survive a move/replacement.


def test_replaced_text_keeps_original_fill_colour(tmp_path):
    source = tmp_path / 'colour.pdf'
    with q.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=PAGE)
        font = _simple_font(pdf, 'ABCD')
        page.Resources = q.Dictionary(Font=q.Dictionary(F0=font))
        page.Contents = pdf.make_stream(
            b'BT /F0 12 Tf 1 0 0 1 40 100 Tm 0 0 0 rg (AB) Tj 1 0 0 rg ET\n')
        pdf.save(source)

    run = index_page(source, 0).runs[0]
    if not _editable(run):
        return  # Read-only for untracked colour changes is safe.
    destination = tmp_path / 'colour-out.pdf'
    write_copy(source, destination,
               [replace(Edit.from_run(0, run), text='CD', y=60)])

    image = _render(destination, scale=6)
    # BGR raster: a black glyph has three similar channels.  The 0.2.1 bug
    # inherits the red fill set after the Tj inside the same BT block.
    left, bottom, right, top = (38, 58, 70, 75)
    height = image.shape[0]
    x0, x1 = int(left * 6), int(right * 6)
    y0, y1 = int(height - top * 6), int(height - bottom * 6)
    patch = image[y0:y1, x0:x1, :3]
    mask = np.any(patch < 230, axis=2)
    assert int(mask.sum()) > 30, 'replacement text did not render'
    mean = patch[mask].mean(axis=0)
    red_excess = float(mean[2] - max(mean[0], mean[1]))
    assert red_excess < 40, (
        f'replacement text changed colour, BGR mean={mean.tolist()}')


# ---------------------------------------------------------------------------
# 6. Adjacent table cells must remain separate runs.


def _cells_pdf(path: Path, gap: float) -> Path:
    with q.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(500, 200))
        font, metrics, _ = fallback('Sans', 'Part A Part B', pdf)
        page.Resources = q.Dictionary(Font=q.Dictionary(F0=font))
        size, x, y = 12, 60, 120
        first = metrics.encode('Part A')
        second = metrics.encode('Part B')
        advance = metrics.width(first) * size
        page.Contents = pdf.make_stream(
            f'BT /F0 {size} Tf 1 0 0 1 {x} {y} Tm <{first.hex()}> Tj ET\n'.encode()
            + f'BT /F0 {size} Tf 1 0 0 1 {x + advance + gap} {y} Tm '
              f'<{second.hex()}> Tj ET\n'.encode())
        pdf.save(path)
    return path


@pytest.mark.parametrize('gap', [5.0, 5.5, 6.0])
def test_adjacent_cells_with_five_to_six_point_gap_stay_separate(tmp_path, gap):
    source = _cells_pdf(tmp_path / f'cells-{gap}.pdf', gap)
    runs = index_page(source, 0).runs
    assert [run.text for run in runs] == ['Part A', 'Part B'], (
        f'a {gap} pt gap at 12 pt merged adjacent cells: '
        f'{[run.text for run in runs]!r}')


def test_typographic_word_gap_may_merge_with_a_space(tmp_path):
    with q.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(500, 200))
        font, metrics, _ = fallback('Sans', 'Alpha Beta', pdf)
        page.Resources = q.Dictionary(Font=q.Dictionary(F0=font))
        size, x, y = 12, 60, 120
        first = metrics.encode('Alpha')
        second = metrics.encode('Beta')
        advance = metrics.width(first) * size
        page.Contents = pdf.make_stream(
            f'BT /F0 {size} Tf 1 0 0 1 {x} {y} Tm <{first.hex()}> Tj ET\n'.encode()
            + f'BT /F0 {size} Tf 1 0 0 1 {x + advance + .33 * size} {y} Tm '
              f'<{second.hex()}> Tj ET\n'.encode())
        pdf.save(tmp_path / 'words.pdf')

    runs = index_page(tmp_path / 'words.pdf', 0).runs
    texts = [run.text for run in runs]
    # Either one run with an inserted space, or two separate words, is safe.
    assert texts in (['Alpha Beta'], ['Alpha', 'Beta'])


# ---------------------------------------------------------------------------
# 7. Partial ToUnicode must not erase the WinAnsi/cp1252 fallback.


def test_partial_tounicode_falls_back_to_cp1252(tmp_path):
    source = _pdf_with_simple_text(
        tmp_path / 'partial.pdf', 'Hello', glyphs='Hello',
        tounicode=_tounicode_pairs([(0x41, '0041'), (0x42, '0042')]))
    runs = index_page(source, 0).runs
    assert [run.text for run in runs] == ['Hello'], (
        'partial /ToUnicode erased valid cp1252 text')


# ---------------------------------------------------------------------------
# 8. One broken CMap or FontBBox must not erase the whole page.


def _two_font_pdf(path: Path, bad_font, content_bad: bytes) -> Path:
    with q.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=PAGE)
        good = _simple_font(pdf, 'OK')
        page.Resources = q.Dictionary(
            Font=q.Dictionary(FBad=bad_font, FGood=good))
        page.Contents = pdf.make_stream(
            content_bad + b'BT /FGood 12 Tf 1 0 0 1 50 80 Tm (OK) Tj ET\n')
        pdf.save(path)
    return path


def test_cmap_overflow_does_not_erase_valid_page_text(tmp_path):
    overflow = (b'/CIDInit /ProcSet findresource begin\n12 dict begin\n'
                b'begincmap\n/CMapName /Bad def\n/CMapType 2 def\n'
                b'1 begincodespacerange\n<00> <FF>\nendcodespacerange\n'
                b'1 beginbfrange\n<00> <02> <FFFF>\nendbfrange\n'
                b'endcmap\nCMapName currentdict /CMap defineresource pop\n'
                b'end\nend\n')
    source = tmp_path / 'overflow.pdf'
    with q.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=PAGE)
        good = _simple_font(pdf, 'OK')
        bad = _simple_font(pdf, 'AB', tounicode=overflow)
        page.Resources = q.Dictionary(Font=q.Dictionary(FBad=bad, FGood=good))
        page.Contents = pdf.make_stream(
            b'BT /FBad 12 Tf 1 0 0 1 50 100 Tm (AB) Tj ET\n'
            b'BT /FGood 12 Tf 1 0 0 1 50 80 Tm (OK) Tj ET\n')
        pdf.save(source)

    runs = index_page(source, 0).runs
    assert 'OK' in [run.text for run in runs], (
        'a CMap overflow erased valid text from the rest of the page')


def test_malformed_fontbbox_does_not_erase_valid_page_text(tmp_path):
    source = tmp_path / 'bbox.pdf'
    with q.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=PAGE)
        good = _simple_font(pdf, 'OK')
        bad = _simple_font(pdf, 'AB', bbox=(0, 0, 1000))
        page.Resources = q.Dictionary(Font=q.Dictionary(FBad=bad, FGood=good))
        page.Contents = pdf.make_stream(
            b'BT /FBad 12 Tf 1 0 0 1 50 100 Tm (AB) Tj ET\n'
            b'BT /FGood 12 Tf 1 0 0 1 50 80 Tm (OK) Tj ET\n')
        pdf.save(source)

    runs = index_page(source, 0).runs
    assert 'OK' in [run.text for run in runs], (
        'a malformed FontBBox erased valid text from the rest of the page')


# ---------------------------------------------------------------------------
# 9. Output validity and atomic rejection.


def _two_page_pdf(path: Path) -> Path:
    with q.Pdf.new() as pdf:
        first = pdf.add_blank_page(page_size=PAGE)
        font = _simple_font(pdf, 'ABPageTwo')
        first.Resources = q.Dictionary(Font=q.Dictionary(F0=font))
        first.Contents = pdf.make_stream(
            b'BT /F0 12 Tf 1 0 0 1 50 100 Tm (AB) Tj ET\n'
            b'0 0 m 100 100 l S\n')
        second = pdf.add_blank_page(page_size=(420, 300))
        second.Resources = q.Dictionary(Font=q.Dictionary(F0=font))
        second.Contents = pdf.make_stream(
            b'BT /F0 12 Tf 1 0 0 1 60 240 Tm (Page Two) Tj ET\n')
        pdf.save(path)
    return path


def test_write_copy_exposes_verification_contract():
    parameters = inspect.signature(write_copy).parameters
    if 'verify' not in parameters:
        pytest.skip('write_copy(verify=...) is absent on this tree')
    assert parameters['verify'].default in (True, 'auto', 'full'), \
        'write_copy must verify output by default'
    assert 'original_source' in parameters, \
        'write_copy must accept the immutable original-source snapshot'


def test_saved_copy_is_valid_and_untouched_pages_are_byte_identical(tmp_path):
    source = _two_page_pdf(tmp_path / 'source.pdf')
    run = next(run for run in index_page(source, 0).runs if run.text == 'AB')
    destination = tmp_path / 'output.pdf'
    write_copy(source, destination, [replace(Edit.from_run(0, run), text='CD')])

    with q.Pdf.open(destination) as output:
        assert output.check_pdf_syntax() == []
        assert len(output.pages) == 2

    with q.Pdf.open(source) as before, q.Pdf.open(destination) as after:
        assert len(before.pages) == len(after.pages) == 2
        for index in range(2):
            assert tuple(before.pages[index].MediaBox) == tuple(after.pages[index].MediaBox)
            assert tuple(before.pages[index].get('/CropBox', before.pages[index].MediaBox)) == \
                   tuple(after.pages[index].get('/CropBox', after.pages[index].MediaBox))
        assert streams(before.pages[1])[0].read_raw_bytes() == \
               streams(after.pages[1])[0].read_raw_bytes()

    with pdfium.PdfDocument(destination) as document:
        assert len(document) == 2
        assert tuple(document.get_page_size(0)) == pytest.approx(PAGE)
        assert tuple(document.get_page_size(1)) == pytest.approx((420, 300))
    assert 'CD' in _text_and_boxes(destination, 0)[0]


def test_rejected_edit_preserves_existing_destination(tmp_path):
    source = _two_page_pdf(tmp_path / 'source.pdf')
    destination = tmp_path / 'existing.pdf'
    destination.write_bytes(b'existing destination must survive')
    bad = Edit(0, -1, 'x' * 100_001, 50, 100, 12, 0, family='Sans')
    with pytest.raises(ValueError):
        write_copy(source, destination, [bad])
    assert destination.read_bytes() == b'existing destination must survive'
