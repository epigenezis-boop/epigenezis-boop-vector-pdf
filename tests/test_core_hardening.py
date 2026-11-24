from dataclasses import replace
import os
from pathlib import Path

import pikepdf as q
import pytest

from vector_pdf import fonts as font_module
from vector_pdf import writer
from vector_pdf.fonts import ASSETS, MAX_CMAP_BYTES, cmap, fallback
from vector_pdf.geometry import translate_angle
from vector_pdf.object_index import index_page, streams
from vector_pdf.writer import (Edit, has_signature, new_run, signature_state, validate,
                               validate_edit, write_copy)
from fixtures_core import (char_boxes, cmap_stream, fallback_pdf, ink, minimal_pdf,
                           page_text, simple_font)


def run_named(path, text, page=0):
    return next(run for run in index_page(path, page).runs if run.text == text)


def test_subset_font_missing_glyph_is_not_native(tmp_path):
    source = tmp_path/'subset.pdf'
    with q.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(300, 200))
        font = simple_font(pdf, subset_text='AB', map_text='ABC')
        page.Resources = q.Dictionary(Font=q.Dictionary(F0=font))
        page.Contents = pdf.make_stream(b'BT /F0 20 Tf 1 0 0 1 50 100 Tm <4142> Tj ET')
        pdf.save(source)
    run = run_named(source, 'AB')
    assert 'C' in run.font.mapping.values()
    assert run.font.encode('A') == b'A'
    assert run.font.encode('C') is None
    edit = replace(Edit.from_run(0, run), text='CB')
    assert edit.native_codes() is None
    destination = tmp_path/'subset-out.pdf'
    write_copy(source, destination, [edit])
    assert 'CB' in page_text(destination)
    assert ink(destination, edit.bounds()) > 0


def test_type0_missing_cid_glyph_is_not_native(tmp_path):
    source = tmp_path/'cid.pdf'
    with q.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(300, 200))
        font, metrics, _ = fallback('Sans', 'AB', pdf)
        pairs = {int.from_bytes(code, 'big'): char for code, char in metrics.mapping.items()}
        pairs[9999] = 'C'
        font.ToUnicode = pdf.make_stream(cmap_stream(pairs, unit=2))
        page.Resources = q.Dictionary(Font=q.Dictionary(F0=font))
        page.Contents = pdf.make_stream(new_run(
            Edit(0, -1, 'AB', 50, 100, 20, 0), '/F0', metrics.encode('AB'),
            translate_angle(50, 100, 0), 20))
        pdf.save(source)
    run = run_named(source, 'AB')
    assert run.font.encode('C') is None
    edit = replace(Edit.from_run(0, run), text='CB')
    assert edit.native_codes() is None
    destination = tmp_path/'cid-out.pdf'
    write_copy(source, destination, [edit])
    assert 'CB' in page_text(destination)
    assert ink(destination, edit.bounds()) > 0


def test_single_show_equal_length_edit_stays_native(tmp_path):
    source = tmp_path/'native.pdf'
    with q.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(300, 200))
        font = simple_font(pdf, subset_text='AB', map_text='AB')
        page.Resources = q.Dictionary(Font=q.Dictionary(F0=font))
        page.Contents = pdf.make_stream(b'BT /F0 20 Tf 1 0 0 1 50 100 Tm <41> Tj ET')
        pdf.save(source)
    run = run_named(source, 'A')
    edit = replace(Edit.from_run(0, run), text='B')
    assert edit.native_codes() == [b'B']
    destination = tmp_path/'native-out.pdf'
    write_copy(source, destination, [edit])
    assert page_text(destination).strip() == 'B'
    with q.Pdf.open(source) as before, q.Pdf.open(destination) as after:
        assert len(streams(before.pages[0])) == len(streams(after.pages[0]))
        assert '/VPFont1' not in after.pages[0].Resources.Font


def test_split_tj_replacement_has_no_overlap(tmp_path):
    source = tmp_path/'split.pdf'
    with q.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(300, 200))
        font, metrics, _ = fallback('Sans', 'AB', pdf)
        page.Resources = q.Dictionary(Font=q.Dictionary(F0=font))
        first, second = metrics.encode('A'), metrics.encode('B')
        x, size = 50, 20
        body = f'BT /F0 {size} Tf 1 0 0 1 {x} 100 Tm <{first.hex()}> Tj ET\n'.encode()
        body += (f'BT /F0 {size} Tf 1 0 0 1 {x+metrics.width(first)*size} 100 Tm '
                 f'<{second.hex()}> Tj ET\n').encode()
        page.Contents = pdf.make_stream(body)
        pdf.save(source)
    run = run_named(source, 'AB')
    assert len(run.glyphs) == 2
    edit = replace(Edit.from_run(0, run), text='WB')
    assert edit.native_codes() is None
    destination = tmp_path/'split-out.pdf'
    write_copy(source, destination, [edit])
    text, boxes = char_boxes(destination)
    assert text == 'WB'
    first_box, second_box = boxes[0][1], boxes[1][1]
    assert first_box[2] <= second_box[0] + .5
    assert second_box[0] - first_box[0] > 10
    assert second_box[0] > first_box[0]


def signed_pdf(path, kind):
    with q.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(300, 200))
        page.Contents = pdf.make_stream(b'')
        value = q.Dictionary(Type=q.Name.Sig, ByteRange=q.Array([0, 10, 20, 30]),
                             Contents=q.String(b'signature'))
        if kind == 'direct':
            pdf.Root.MySig = value
        elif kind == 'nested':
            pdf.Root.Nested = q.Dictionary(Inner=q.Dictionary(Signature=value))
        elif kind == 'array':
            pdf.Root.Signatures = q.Array([q.Dictionary(Type=q.Name.Sig), value])
        elif kind == 'indirect':
            pdf.Root.Indirect = pdf.make_indirect(value)
        elif kind == 'docmdp':
            pdf.Root.Perms = q.Dictionary(DocMDP=value)
        pdf.save(path)
    return Path(path)


@pytest.mark.parametrize('kind', ['direct', 'nested', 'array', 'indirect', 'docmdp'])
def test_signature_dictionaries_are_detected(tmp_path, kind):
    source = signed_pdf(tmp_path/f'{kind}.pdf', kind)
    with q.Pdf.open(source) as pdf:
        assert has_signature(pdf)
        assert signature_state(pdf) == 'signed'
        with pytest.raises(ValueError, match='Signed'):
            validate(pdf)
    destination = tmp_path/f'{kind}-out.pdf'
    with pytest.raises(ValueError, match='(?i)signature'):
        write_copy(source, destination, [])
    assert not destination.exists()


def test_empty_signature_field_and_empty_perms_are_not_signatures(tmp_path):
    source = tmp_path/'empty-field.pdf'
    with q.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(300, 200))
        page.Contents = pdf.make_stream(b'')
        field = pdf.make_indirect(q.Dictionary(
            Type=q.Name.Annot, Subtype=q.Name.Widget, FT=q.Name.Sig,
            T=q.String('Signature1'), Rect=q.Array([0, 0, 20, 10])))
        pdf.Root.AcroForm = q.Dictionary(Fields=q.Array([field]))
        pdf.Root.Perms = q.Dictionary()
        pdf.save(source)
    with q.Pdf.open(source) as pdf:
        assert not has_signature(pdf)
        assert signature_state(pdf) == 'empty'
        validate(pdf)
    destination = tmp_path/'empty-field-out.pdf'
    write_copy(source, destination, [Edit(0, -1, 'Allowed', 50, 50, 12, 0, family='Sans')])
    assert 'Allowed' in page_text(destination)
    with q.Pdf.open(destination) as pdf:
        assert not has_signature(pdf)


def clipped_pdf(path, content):
    with q.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(300, 200))
        font, metrics, _ = fallback('Sans', 'AB', pdf)
        page.Resources = q.Dictionary(Font=q.Dictionary(F0=font))
        body = content.replace(b'CODE', metrics.encode('AB').hex().encode())
        page.Contents = pdf.make_stream(body)
        pdf.save(path)
    return Path(path)


def test_clipped_text_is_editable_and_reported(tmp_path):
    source = clipped_pdf(tmp_path/'clipped.pdf',
                         b'q 10 10 200 40 re W n BT /F0 20 Tf 1 0 0 1 20 20 Tm <CODE> Tj ET Q')
    run = run_named(source, 'AB')
    assert run.editable
    assert run.glyphs[0].clipped
    assert 'clip' in run.reason
    destination = tmp_path/'clipped-out.pdf'
    edit = replace(Edit.from_run(0, run), text='CD', y=150)
    write_copy(source, destination, [edit])
    assert 'CD' in page_text(destination)


def test_paint_inside_text_block_is_read_only(tmp_path):
    source = clipped_pdf(
        tmp_path/'painted.pdf',
        b'BT /F0 20 Tf 1 0 0 1 20 20 Tm <CODE> Tj 0 0 1 rg 0 0 50 50 re f ET')
    run = run_named(source, 'AB')
    assert not run.editable
    assert run.reason


def test_non_device_color_space_is_read_only(tmp_path):
    source = clipped_pdf(
        tmp_path/'pattern.pdf',
        b'q /Pattern cs /P1 scn BT /F0 20 Tf 1 0 0 1 20 20 Tm <CODE> Tj ET Q')
    run = run_named(source, 'AB')
    assert not run.editable
    assert 'color' in run.reason


def test_moved_colored_text_keeps_fill_color(tmp_path):
    source = fallback_pdf(tmp_path/'red.pdf', 'AB', color=(1., 0., 0.))
    run = run_named(source, 'AB')
    assert run.glyphs[0].fill == pytest.approx((1., 0., 0.))
    edit = replace(Edit.from_run(0, run), x=70, y=50)
    assert edit.fill_color() == pytest.approx((1., 0., 0.))
    destination = tmp_path/'red-out.pdf'
    write_copy(source, destination, [edit])
    with q.Pdf.open(destination) as pdf:
        assert b'1 0 0 rg' in streams(pdf.pages[0])[-1].read_bytes()
    assert page_text(destination).count('AB') == 1
    assert ink(destination, edit.bounds(), red=True) > 0


def test_appended_replacement_is_above_opaque_shape(tmp_path):
    source = fallback_pdf(tmp_path/'covered.pdf', 'AB',
                          suffix=b'0 0 1 rg\n0 0 300 200 re f\n')
    run = run_named(source, 'AB')
    assert run.editable
    edit = replace(Edit.from_run(0, run), x=60, y=60)
    destination = tmp_path/'covered-out.pdf'
    write_copy(source, destination, [edit])
    assert ink(destination, edit.bounds()) > 0


def test_cmap_overflow_and_bad_bbox_do_not_kill_page(tmp_path):
    bad_cmap = ('/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n'
                '1 begincodespacerange\n<00> <ff>\nendcodespacerange\n'
                '1 beginbfrange\n<0000> <0002> <FFFF>\nendbfrange\n'
                'endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n').encode()
    source = tmp_path/'bad-font.pdf'
    with q.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(400, 250))
        bad = simple_font(pdf, subset_text='AB', map_text='AB', bbox=[0, 0, 1000], name='BadFont')
        bad.ToUnicode = pdf.make_stream(bad_cmap)
        good, good_metrics, _ = fallback('Sans', 'Good', pdf)
        page.Resources = q.Dictionary(Font=q.Dictionary(F1=bad, F0=good))
        body = b'BT /F1 20 Tf 1 0 0 1 50 180 Tm <41> Tj ET\n'
        body += new_run(Edit(0, -1, 'Good', 50, 100, 20, 0), '/F0',
                        good_metrics.encode('Good'), translate_angle(50, 100, 0), 20)
        page.Contents = pdf.make_stream(body)
        pdf.save(source)
    with q.Pdf.open(source) as pdf:
        from vector_pdf.fonts import read_font
        font = read_font(pdf.pages[0].Resources.Font.F1)
        assert len(font.bounds) == 4
    index = index_page(source, 0)
    assert any(run.text == 'Good' for run in index.runs)
    assert cmap(b'x'*(MAX_CMAP_BYTES+1)) == {}
    assert isinstance(cmap(bad_cmap), dict)


def test_partial_tounicode_keeps_winansi(tmp_path):
    source = tmp_path/'partial.pdf'
    with q.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(300, 200))
        font = simple_font(pdf, subset_text='A', map_text='A')
        page.Resources = q.Dictionary(Font=q.Dictionary(F0=font))
        page.Contents = pdf.make_stream(b'BT /F0 12 Tf 1 0 0 1 50 100 Tm (Hello) Tj ET')
        pdf.save(source)
    run = run_named(source, 'Hello')
    assert run.font.mapping[b'A'] == 'A'
    assert run.font.decode(b'Hello') == 'Hello'


def cell_merge_pdf(path):
    words = 'One Two Three Four Five Six Seven Eight'
    size = 12
    with q.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(400, 260))
        font, metrics, _ = fallback('Sans', words, pdf)
        page.Resources = q.Dictionary(Font=q.Dictionary(F0=font))
        body = bytearray()
        for y, gap, left, right in [(200, .33*size, 'One', 'Two'),
                                    (160, 3, 'Three', 'Four'),
                                    (120, 5, 'Five', 'Six'),
                                    (80, 6, 'Seven', 'Eight')]:
            x = 40
            for word in (left, right):
                code = metrics.encode(word)
                body.extend((f'BT /F0 {size} Tf 1 0 0 1 {x} {y} Tm '
                             f'<{code.hex()}> Tj ET\n').encode())
                x += size*(metrics.width(code)+gap/size)
        page.Contents = pdf.make_stream(bytes(body))
        pdf.save(path)
    return Path(path)


def test_cell_merge_uses_font_space_width(tmp_path):
    source = cell_merge_pdf(tmp_path/'cells.pdf')
    texts = [run.text for run in index_page(source, 0).runs]
    assert texts == ['One Two', 'Three Four', 'Five', 'Six', 'Seven', 'Eight']


def test_output_validation_backup_and_failure_atomicity(tmp_path, monkeypatch):
    source = minimal_pdf(tmp_path/'plain.pdf')
    destination = tmp_path/'copy.pdf'
    write_copy(source, destination, [])
    assert destination.exists() and not Path(str(destination)+'.bak').exists()
    original = destination.read_bytes()

    write_copy(source, destination, [])
    backup = Path(str(destination)+'.bak')
    assert backup.read_bytes() == original
    current = destination.read_bytes()

    def broken(self):
        return ['synthetic corruption']

    monkeypatch.setattr(q.Pdf, 'check_pdf_syntax', broken)
    with pytest.raises(ValueError, match='structural'):
        write_copy(source, destination, [])
    assert destination.read_bytes() == current
    assert backup.read_bytes() == original


def test_output_text_validation_preserves_destination(tmp_path, monkeypatch):
    source = minimal_pdf(tmp_path/'plain.pdf')
    destination = tmp_path/'copy.pdf'
    destination.write_bytes(b'previous bytes')
    monkeypatch.setattr(writer, '_pdfium_text', lambda document, number: '')
    edit = Edit(0, -1, 'Lost text', 50, 50, 12, 0, family='Sans')
    with pytest.raises(ValueError, match='not present'):
        write_copy(source, destination, [edit])
    assert destination.read_bytes() == b'previous bytes'
    assert not Path(str(destination)+'.bak').exists()


def test_unrenderable_edit_preserves_destination(tmp_path):
    source = minimal_pdf(tmp_path/'plain.pdf')
    destination = tmp_path/'copy.pdf'
    destination.write_bytes(b'previous bytes')
    edit = Edit(0, -1, '\U00010348', 50, 50, 12, 0, family='Sans')
    with pytest.raises(ValueError, match='contain'):
        write_copy(source, destination, [edit])
    assert destination.read_bytes() == b'previous bytes'


def test_original_source_cannot_be_the_destination(tmp_path):
    snapshot = minimal_pdf(tmp_path/'snapshot.pdf')
    source = minimal_pdf(tmp_path/'source.pdf')
    with pytest.raises(ValueError, match='original source'):
        write_copy(snapshot, source, [], original_source=source)
    assert source.read_bytes()


def test_bounds_fall_back_safely_for_unsupported_glyph(tmp_path):
    from math import isfinite
    source = fallback_pdf(tmp_path/'bounds.pdf', 'AB')
    run = run_named(source, 'AB')
    edit = replace(Edit.from_run(0, run), text='\U00010348')
    assert not edit.glyph_supported()
    assert all(isfinite(value) for value in edit.bounds())


def sized_text_pdf(path, text, size, page_size):
    with q.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=page_size)
        font, metrics, _ = fallback('Sans', text, pdf)
        page.Resources = q.Dictionary(Font=q.Dictionary(F0=font))
        page.Contents = pdf.make_stream(new_run(
            Edit(0, -1, text, 10, 100, size, 0), '/F0', metrics.encode(text),
            translate_angle(10, 100, 0), size))
        pdf.save(path)
    return Path(path)


def test_validate_edit_preserves_long_original_text(tmp_path):
    text = 'A'*2000
    source = sized_text_pdf(tmp_path/'long.pdf', text, 4, (6500, 200))
    run = run_named(source, text)
    edit = Edit.from_run(0, run)
    validate_edit(edit)
    destination = tmp_path/'long-out.pdf'
    write_copy(source, destination, [edit])
    assert len(run_named(destination, text).text) == 2000


@pytest.mark.parametrize('size,page_size', [(.1, (300, 200)), (4500, (7000, 6000))])
def test_validate_edit_accepts_original_extreme_sizes(tmp_path, size, page_size):
    source = sized_text_pdf(tmp_path/f'size-{size}.pdf', 'AB', size, page_size)
    run = run_named(source, 'AB')
    edit = Edit.from_run(0, run)
    assert edit.size == pytest.approx(size)
    validate_edit(edit)
    destination = tmp_path/f'size-{size}-out.pdf'
    write_copy(source, destination, [edit])
    assert run_named(destination, 'AB').text == 'AB'


def test_learn_fonts_skips_broken_local_font(tmp_path, monkeypatch):
    (tmp_path/'broken.ttf').write_bytes(b'not a font')
    (tmp_path/'good.ttf').write_bytes((ASSETS/'DejaVuSans.ttf').read_bytes())
    monkeypatch.setattr(font_module, 'font_directory', lambda: tmp_path)
    monkeypatch.setattr(font_module, 'FALLBACKS', dict(font_module.FALLBACKS))
    font_module.learn_fonts()
    local = {key for key in font_module.FALLBACKS if key.startswith('Local · ')}
    assert local == {'Local · good'}
    assert font_module.FALLBACKS['Local · good'] == tmp_path/'good.ttf'

    broken = tmp_path/'broken.ttf'
    font_module.FALLBACKS['Local · broken'] = broken
    measured, data = font_module.fallback('Local · broken', 'AB')
    assert measured.name == 'Sans'
    assert data == measured.encode('AB')
    assert 'Local · broken' not in font_module.FALLBACKS


@pytest.mark.skipif(os.name != 'nt', reason='LOCALAPPDATA is Windows-specific.')
def test_font_directory_falls_back_without_localappdata(monkeypatch):
    monkeypatch.delenv('LOCALAPPDATA', raising=False)
    assert font_module.font_directory() == Path.home()/'.vector-pdf'/'fonts'


def test_noop_edit_keeps_streams_and_font(tmp_path):
    source = fallback_pdf(tmp_path/'noop.pdf', 'AB')
    run = run_named(source, 'AB')
    destination = tmp_path/'noop-out.pdf'
    write_copy(source, destination, [Edit.from_run(0, run)])
    with q.Pdf.open(source) as before, q.Pdf.open(destination) as after:
        a = [stream.read_raw_bytes() for stream in streams(before.pages[0])]
        b = [stream.read_raw_bytes() for stream in streams(after.pages[0])]
        assert a == b
        assert '/VPFont1' not in after.pages[0].Resources.Font
