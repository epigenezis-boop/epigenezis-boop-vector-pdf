"""Capture measured timings using PDFium tiles and the QPDF editor."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import statistics
from tempfile import TemporaryDirectory
from time import perf_counter

import psutil

from fixtures import drawing
from sample import stamp_settings
from vector_pdf.object_index import index_page
from vector_pdf.render import RenderEngine, Tile
from vector_pdf.writer import Edit, write_copy


def ms(start):
    return round((perf_counter()-start)*1000, 2)


def measure(path, output, selection, original, replacement):
    renderer = RenderEngine()
    key = path.name
    with ThreadPoolExecutor(max_workers=1) as worker:
        sheets, opened = worker.submit(renderer.open, key, path).result()
        tx = int((selection[0]+selection[2]) / 2 / 512)
        y = (selection[1]+selection[3])/2
        ty = int((sheets[0].height-y)/512)
        request = Tile(0, 1., tx, ty)
        cold = worker.submit(renderer.render, key, request).result()[-1]
        warm = [worker.submit(renderer.render, key, request).result()[-1] for _ in range(5)]
        worker.submit(renderer.close, key).result()

    ix = index_page(path, 0)
    lookups = []
    for _ in range(100):
        start = perf_counter()
        runs = ix.area(selection)
        lookups.append(ms(start))
    text = next(r for r in runs if r.text == original)
    edit = replace(Edit.from_run(0, text), text=replacement)
    start = perf_counter()
    write_copy(path, output, [edit])
    saved = ms(start)
    with ThreadPoolExecutor(max_workers=1) as worker:
        worker.submit(renderer.attach, key, output).result()
        refreshed = worker.submit(renderer.render, key, request).result()[-1]
        worker.submit(renderer.close, key).result()
    renderer.shutdown()
    return {
        'size_mb': round(path.stat().st_size/1e6,3),
        'pages': len(sheets),
        'open_ms': round(opened,2),
        'first_tile_ms': round(cold,2),
        'warm_pdfium_tile_median_ms': round(statistics.median(warm),2),
        'index_ms': round(ix.elapsed*1000,2),
        'text_show_operators': ix.objects,
        'indexed_selection_median_ms': statistics.median(lookups),
        'save_ms': saved,
        'snapshot_first_dirty_tile_ms': round(refreshed,2),
        'end_rss_mib': round(psutil.Process().memory_info().rss/1048576,2),
    }


if __name__ == '__main__':
    source = Path(os.environ['VECTOR_TEST_PDF'])
    with TemporaryDirectory() as tmp:
        work = Path(tmp)
        fixture = stamp_settings()
        report = {'Inventor': measure(source, work / 'edited.pdf', fixture['box'], fixture['old'], fixture['new'])}
        for count, large in ((100_000, False), (1_000_000, True)):
            source = drawing(work / f'{count}.pdf', count, large)
            report[str(count)] = measure(source, work / 'edited.pdf', (152, 86, 209, 100), 'Иванов', 'Ёжиков')
        print(json.dumps(report, ensure_ascii=False, indent=2))
