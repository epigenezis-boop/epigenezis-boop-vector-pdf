from random import Random
from pathlib import Path

import pikepdf as q

from vector_pdf.fonts import fallback
from vector_pdf.geometry import translate_angle
from vector_pdf.writer import Edit, new_run


def drawing(path: Path, primitives=0, large=False):
    """Generate original synthetic paths, Cyrillic, and two placements of a nested Form."""
    size = (3370, 2384) if large else (595, 842)
    with q.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=size)
        sample = 'Конструктор Иванов Ёжиков Поворот Форма'
        font, measured, _ = fallback('Sans', sample, pdf)
        encode = measured.encode
        form = pdf.make_stream(b'1 w 3 2 m 90 2 l S\n' +
             new_run(Edit(0, -1, 'Форма', 3, 8, 11, 0), '/F0', encode('Форма'), translate_angle(3, 8, 0), 11))
        form.Type, form.Subtype = q.Name.XObject, q.Name.Form
        form.BBox = q.Array([0, 0, 100, 33])
        form.Resources = q.Dictionary(Font=q.Dictionary(F0=font))
        outer = pdf.make_stream(b'/Inner Do')
        outer.Type, outer.Subtype = q.Name.XObject, q.Name.Form
        outer.BBox = q.Array([0, 0, 100, 33])
        outer.Resources = q.Dictionary(XObject=q.Dictionary(Inner=form))
        page.Resources = q.Dictionary(Font=q.Dictionary(F0=font), XObject=q.Dictionary(Shared=outer))
        rand = Random(752)
        geometry = bytearray(b'.25 w\n')
        for i in range(primitives):
            x, y = rand.randint(15, size[0]-25), rand.randint(125, size[1]-25)
            geometry.extend(f'{x} {y} m {x+2} {y+2} l\n'.encode())
            if (i+1)%2048 == 0:
                geometry.extend(b'S\n')
        geometry.extend(b'S\n')
        geometry.extend(b'60 400 m 72 418 l 85 405 l h S\n')
        geometry.extend(b'q 1 0 0 1 202 70 cm /Shared Do Q\nq 1 0 0 1 311 70 cm /Shared Do Q\n')
        for text, x, y, angle in [('Конструктор', 27, 90, 0), ('Иванов', 156, 90, 0), ('Поворот', 28, 200, 25)]:
            geometry.extend(new_run(Edit(0, -1, text, x, y, 12, angle),
                '/F0', encode(text), translate_angle(x, y, angle), 12))
        page.Contents = pdf.make_stream(bytes(geometry))
        second = pdf.add_blank_page(page_size=(595, 842))
        second.Contents = page.Contents
        second.Resources = page.Resources
        pdf.save(path)
    return path
