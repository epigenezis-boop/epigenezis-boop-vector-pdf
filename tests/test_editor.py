from dataclasses import replace
import os
from pathlib import Path
import shutil
import subprocess

import numpy as np
import pikepdf as q
import pypdfium2 as pdfium
from pypdfium2 import raw
import pytest

from vector_pdf.geometry import Sheet, box
from vector_pdf.object_index import index_page, streams
from vector_pdf.writer import Edit, file_stamp, write_copy
from fixtures import drawing
from sample import stamp_settings


def stamp(path):
    ix = index_page(path, 0)
    settings = stamp_settings()
    hits = ix.area(tuple(settings['box']))
    return next(run for run in hits if run.text == settings['old'] and run.editable)


def fingerprint(value):
    if isinstance(value, q.Stream):
        return ('stream', value.read_raw_bytes(), fingerprint(value.stream_dict), str(value.get('/Filter', '')), fingerprint(value.get('/DecodeParms', None)))
    if isinstance(value, q.Dictionary):
        return tuple((str(key), fingerprint(item)) for key, item in sorted(value.items(), key=lambda x: str(x[0]))
                     if str(key) not in ('/Length', '/Filter', '/DecodeParms'))
    if isinstance(value, q.Array):
        return tuple(map(fingerprint, value))
    if isinstance(value, q.String):
        return bytes(value)
    return str(value)


def render(page, scale, clip=None):
    size = np.ceil(np.asarray(page.get_size())*scale).astype(int)
    x, y, w, h = clip or (0, 0, *map(int, size))
    with_bitmap = pdfium.PdfBitmap.new_native(w, h, raw.FPDFBitmap_BGR)
    try:
        with_bitmap.fill_rect((255, 255, 255, 255), 0, 0, w, h)
        raw.FPDF_RenderPageBitmap(with_bitmap, page, -x, -y, int(size[0]), int(size[1]), 0,
                                  raw.FPDF_ANNOT | raw.FPDF_LCD_TEXT)
        return with_bitmap.to_numpy().copy()
    finally:
        with_bitmap.close()


def assert_pixels(page_a, page_b, rect, scale, clip=None):
    a = render(page_a, scale, clip)
    b = render(page_b, scale, clip)
    changed = np.any(a != b, axis=2)
    if rect:
        l, t, r, bottom = rect
        x, y = clip[:2] if clip else (0, 0)
        left, top = max(0, int(l*scale)-x-2), max(0, int(t*scale)-y-2)
        right, low = int(np.ceil(r*scale))-x+2, int(np.ceil(bottom*scale))-y+2
        assert changed[top:low, left:right].any(), 'no rendered difference inside the edit'
        changed[top:low, left:right] = False
    assert not changed.any(), f'{changed.sum()} changed pixels outside the edit'


@pytest.mark.skipif(not (os.getenv('VECTOR_TEST_PDF') and os.getenv('VECTOR_TEST_STAMP')), reason='Set VECTOR_TEST_PDF to the attached drawing.')
def test_inventor_acceptance(tmp_path):
    source = Path(os.environ['VECTOR_TEST_PDF'])
    original_stamp = file_stamp(source)
    target = tmp_path / 'acceptance.pdf'
    run = stamp(source)
    settings = stamp_settings()
    edit = replace(Edit.from_run(0, run), text=settings['new'])
    assert edit.current_font() == 'Original'
    write_copy(source, target, [edit], original_stamp)
    assert file_stamp(source) == original_stamp
    with q.Pdf.open(source) as before, q.Pdf.open(target) as after:
        assert len(before.pages) == len(after.pages) == 9
        assert after.check_pdf_syntax() == []
        assert fingerprint(before.Root.OCProperties) == fingerprint(after.Root.OCProperties)
        for i, (a, b) in enumerate(zip(before.pages, after.pages)):
            assert fingerprint(a.MediaBox) == fingerprint(b.MediaBox)
            assert fingerprint(a.Resources) == fingerprint(b.Resources)
            if i == 0:
                # The 0.3 policy clears the selected Tj and appends one isolated
                # top-layer run, so the edited page may gain wrapper streams.
                # Raster comparison below verifies that no other pixels moved.
                continue
            sa, sb = streams(a), streams(b)
            assert len(sa) == len(sb)
            assert [item.read_raw_bytes() for item in sa] == [item.read_raw_bytes() for item in sb]
    with pdfium.PdfDocument(source) as before, pdfium.PdfDocument(target) as after:
        sheet = Sheet(*before[0].get_size(), before[0].get_bbox(), 0)
        a = box(sheet.to_screen, run.bounds)
        b = box(sheet.to_screen, edit.bounds())
        bounds = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
        for i in range(len(before)):
            p1, p2 = before[i], after[i]
            assert_pixels(p1, p2, bounds if i == 0 else None, .4)
            if i == 0:
                scale = 4
                x, y = (int(v*scale)-100 for v in bounds[:2])
                width, height = [int((bounds[k+2]-bounds[k])*scale)+200 for k in range(2)]
                assert_pixels(p1, p2, bounds, scale, (x, y, width, height))
                textpage = p2.get_textpage()
                try:
                    text = textpage.get_text_bounded()
                finally:
                    textpage.close()
                assert settings['new'] in text and settings['old'] not in text
            p1.close()
            p2.close()
    if shutil.which('pdftotext'):
        text = subprocess.check_output(['pdftotext', '-f', '1', '-l', '1', target, '-']).decode()
        assert settings['new'] in text


def test_rotated_fallback_move_delete_and_shared_form(tmp_path):
    source = drawing(tmp_path / 'source.pdf')
    index = index_page(source, 0)
    assert index.forms == 1
    assert all('Форма' not in run.text for run in index.runs)
    run = next(run for run in index.runs if run.text == 'Поворот')
    assert run.placement[-1] == pytest.approx(25)
    move = replace(Edit.from_run(0, run), text='Текст Ё', x=91, y=235, angle=55,
                   family='Mono', size=16, color=(.2, .1, .8))
    removed = next(run for run in index.runs if run.text == 'Иванов')
    add = Edit(0, -7, 'Новый текст', 160, 350, 14, 0, family='Sans')
    destination = tmp_path / 'edited.pdf'
    write_copy(source, destination, [move, replace(Edit.from_run(0, removed), text=''), add])
    with q.Pdf.open(source) as before, q.Pdf.open(destination) as after:
        assert after.check_pdf_syntax() == []
        assert streams(before.pages[1])[0].read_raw_bytes() == streams(after.pages[1])[0].read_raw_bytes()
        assert fingerprint(before.pages[0].Resources.XObject) == fingerprint(after.pages[0].Resources.XObject)
        assert '/VPFont1' not in after.pages[1].Resources.Font
    with pdfium.PdfDocument(destination) as document:
        text = document[0].get_textpage().get_text_bounded()
        assert 'Текст Ё' in text
        assert 'Новый текст' in text
        assert 'Поворот' not in text
        assert 'Иванов' not in text
        assert text.count('Форма') == 2


def test_source_protection_and_atomic_failure(tmp_path):
    path = drawing(tmp_path / 'source.pdf')
    with pytest.raises(ValueError, match='separate'):
        write_copy(path, path, [])
    signed = tmp_path / 'signed.pdf'
    with q.Pdf.open(path) as doc:
        signature = doc.make_indirect(q.Dictionary(
            Type=q.Name.Sig, ByteRange=q.Array([0, 10, 20, 10]),
            Contents=q.String(b'0'*64)))
        doc.Root.Perms = q.Dictionary(DocMDP=signature)
        doc.save(signed)
    output = tmp_path / 'untouched.pdf'
    output.write_bytes(b'previous revision')
    with pytest.raises(ValueError, match='Signed'):
        write_copy(signed, output, [])
    assert output.read_bytes() == b'previous revision'
    # An empty /Perms dictionary is not a signature; 0.3 must not false-positive.
    empty = tmp_path / 'empty-perms.pdf'
    with q.Pdf.open(path) as doc:
        doc.Root.Perms = q.Dictionary()
        doc.save(empty)
    empty_copy = tmp_path / 'empty-perms-copy.pdf'
    write_copy(empty, empty_copy, [])
    assert empty_copy.is_file() and empty_copy.stat().st_size > 0
    protected = tmp_path / 'locked.pdf'
    with q.Pdf.open(path) as doc:
        doc.save(protected, encryption=q.Encryption(user='', owner='owner-password'))
    with pytest.raises(ValueError, match='encrypted'):
        write_copy(protected, output, [])
    assert output.read_bytes() == b'previous revision'


def test_words_on_a_line_form_one_edit_but_other_columns_stay_separate(tmp_path):
    from vector_pdf.fonts import fallback
    source = tmp_path/'line.pdf'
    with q.Pdf.new() as pdf:
        page = pdf.add_blank_page()
        font, metrics, _ = fallback('ISO 3098', 'Редактируем всю строку Соседняя ячейка', pdf)
        page.Resources = q.Dictionary(Font=q.Dictionary(F0=font))
        x, y, size = 60, 110, 13
        tokens = [b'/F0 13 Tf\n']
        for word in ('Редактируем', 'всю', 'строку'):
            code = metrics.encode(word)
            tokens.append(f'BT 1 0 0 1 {x} {y} Tm <{code.hex()}> Tj ET\n'.encode())
            x += size * (metrics.width(code)+.33)
        code = metrics.encode('Соседняя ячейка')
        tokens.append(f'BT 1 0 0 1 {x+40} {y} Tm <{code.hex()}> Tj ET\n'.encode())
        page.Contents = pdf.make_stream(b''.join(tokens))
        pdf.save(source)
    runs = index_page(source, 0).runs
    assert [run.text for run in runs] == ['Редактируем всю строку', 'Соседняя ячейка']
    updated = tmp_path/'updated.pdf'
    edit = replace(Edit.from_run(0, runs[0]), text='Всю строку целиком', family='ISO 3098')
    write_copy(source, updated, [edit])
    assert [run.text for run in index_page(updated, 0).runs] == ['Всю строку целиком', 'Соседняя ячейка']


def test_iso_italic_is_editable_after_save_and_independent_of_rotation(tmp_path):
    source = drawing(tmp_path / 'source.pdf')
    path = tmp_path / 'italic.pdf'
    edit = Edit(0, -1, 'Наклон ISO 3098', 80, 260, 16, 30, family='ISO 3098', italic=True)
    write_copy(source, path, [edit])

    def inscription(file):
        return next(run for run in index_page(file, 0).runs if run.text == edit.text)

    run = inscription(path)
    assert run.editable
    assert run.placement == pytest.approx((80, 260, 16, 30))
    assert run.shear == pytest.approx(edit.shear(), abs=1e-6)
    assert run.shear > 0.2
    assert Edit.from_run(0, run).is_italic()

    straight = tmp_path / 'straight.pdf'
    write_copy(path, straight, [replace(Edit.from_run(0, run), italic=False)])
    reopened = inscription(straight)
    assert reopened.placement == pytest.approx(run.placement)
    assert abs(reopened.shear) < 1e-6
    with q.Pdf.open(source) as before, q.Pdf.open(straight) as after:
        assert after.check_pdf_syntax() == []
        assert streams(before.pages[1])[0].read_raw_bytes() == streams(after.pages[1])[0].read_raw_bytes()
