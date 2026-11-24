# Independent QA report — VECTOR PDF 0.3.0

Role: independent QA/regression (Agent D), integrated by the technical lead.
Date: 24 Nov 2026.
Baseline audited: VECTOR PDF 0.2.1, commit `47238fc`.
Integrated branch: `deepseek/vector-pdf-0.3-hardening`.

Environment: Windows 11 10.0.22621 x64; CPython 3.12.10; Qt/PySide6 6.10.3;
pikepdf 10.18.2 / QPDF 12.4.2; pypdfium2 5.15.2 / PDFium 159.0.8149.0;
fontTools 4.68.0; rtree 1.4.1; numpy 2.5.5; pytest 9.2.0. Qt platform for the
automated UI runs: `offscreen`.

## Commands and exact results

```powershell
$env:PYTHONPATH='.'
$env:QT_QPA_PLATFORM='offscreen'
$env:VECTORPDF_NO_RECOVERY='1'
.venv\Scripts\python.exe -m pytest -q
```

```
182 passed, 1 skipped, 3 warnings in 10.31s
```

The skip is the real-Inventor-only test when `VECTOR_TEST_PDF` is absent. It was
executed separately on the supplied drawing:

```
1 passed in 23.92s
```

The independent baseline suite (same tests against 0.2.1) produced:

```
35 failed, 5 passed, 7 skipped in 9.18s
```

Those baseline failures are the confirmed audit defects; `QA_BASELINE.md`
contains the full table and raw excerpts. The skips were APIs absent on 0.2.1,
not passes.

## Independent real-drawing verification

Drawing: 9 pages, 18.0 MB, Autodesk Inventor Professional 2023, supplied by the
project owner. It is stored outside the repository and was used only through a
temporary copy. No user drawing is committed or included in the portable ZIP.

Procedure and result:

1. Indexed all 9 pages. Page 1: 910 text runs, 909 editable; one text run is
   read-only because of a non-default text render mode. Other pages: all runs
   editable.
2. Replaced `Размеры` with `Тест` on page 1 using the original embedded
   GOSTCommonItalic subset (position and size unchanged), saved to a temp file.
3. QPDF `check_pdf_syntax()` returned an empty list.
4. PDFium reopened 9 pages. Extracted text contains the new string and no longer
   contains the old one.
5. Raster comparison of all 9 pages at 0.4× showed changed pixels only inside
   the edit bounds; a separate 4× check of the edit region passed. Untouched
   pages kept byte-identical raw content streams.
6. The source file's bytes and stamp were unchanged.
7. UI offscreen run: open 1866 ms; Apply 125 ms (`metrics.edit` 66 ms); Undo
   94 ms; Redo 118 ms; Save copy 668 ms. The UI run saved a valid copy and
   reopened its text successfully.

Full QPDF syntax check of this 18 MB file takes about 8.9 s. The application
uses a bounded fast path for large CAD streams (structure, geometry, signatures,
untouched bytes, PDFium text) and reserves the deep QPDF check for small files
and QA. This trade-off is documented in `BENCHMARKS.md` and
`KNOWN_LIMITATIONS.md`.

## Defect matrix

The detailed P0/P1 table is in `AUDIT_MATRIX.md`. Summary:

### P0

| # | Item | QA status |
|---|---|---|
| 1 | Update health-check/rollback/safe mode/full manifest | PASS by failure-injection tests; production signing key NOT RUN |
| 2 | Embedded-font glyph availability | PASS (core + QA raster) |
| 3 | Multi-`Tj` positions | PASS (overlap regression) |
| 4 | Recursive signatures/direct/nested/empty | PASS |
| 5 | Clip/colour/z-order | PASS for policy; real clipped text editable with warning, moved copy top-layer |
| 6 | Busy page/mode/add guards | PASS |
| 7 | Safe Save suffix/overwrite | PASS |
| 8 | Long text/original size | PASS |
| 9 | Source-change export | PASS (snapshot + real source change test) |
| 10 | Save/Discard/Cancel, draft, journal, recovery, cleanup | PASS |
| 11 | Output validation and `.bak` | PASS (QPDF/PDFium/raster/streams on real file); deep QPDF bounded for large files |
| 12 | Windows/HiDPI/logs/crash/About | PARTIAL — logs/About/offscreen tests PASS; physical HiDPI NOT RUN |
| 13 | Source/tests/tools/launcher/CI/SBOM | PARTIAL — repository artifacts + local package PASS; CI runner and MSVC launcher build NOT RUN |
| 14 | Signing/launcher manifest/Qt notices/LGPL offer | PARTIAL — manifest/version resource/notices present; Authenticode NOT RUN; production update key not installed |
| 15 | Async exception safety/closed guard | PASS |

### P1

PASS: cell merging, CMap/FontBBox robustness, partial ToUnicode, preview
font/colour/bounds, tile order/cache stats, save-over-open-tab, mode sync,
plain-text labels, local-font fallback, region/guards, documentation.

PARTIAL with documented residual: cache bounds (undo/index unbounded), global
thumbnail manager/DPR, actual-size/UserUnit, update progress/cancel dialog,
portable settings, full i18n/accessibility, temp ACL.

## Output-validity evidence

For every save in the test suite:

- result reopens with pikepdf/QPDF and PDFium;
- page count and page boxes/rotation are unchanged;
- real signatures are absent; empty signature fields do not block;
- untouched page content streams are compared byte-for-byte;
- edited text is re-extracted from the output;
- the existing destination survives a rejected save;
- `.bak` is created for an overwritten destination;
- raster comparisons cover glyph ink, not just extracted text (subset glyph,
  multi-`Tj`, clip, colour, real drawing).

## Updater failure injection

`tests/test_updater_hardening.py` (76 total updater tests with
`test_updater.py`) covers:

- RFC 8032 Ed25519 vectors and placeholder fail-closed;
- canonical manifest, bad signature/hash/manifest/version/archive;
- missing python312.zip/PySide6/ui/assets, extra/missing files, size/hash
  mismatch, traversal/ADS/reserved names/symlinks/duplicates;
- partial/broken version quarantine and reinstall, interrupted incoming cleanup,
  retention, lock;
- staged health-check success/failure, dispatch dead child/timeout, readiness
  token, rollback to base/previous, rehash/tampered manifest rejection;
- safe mode/reset, package manifest and deterministic archive.

The production private key and a live GitHub release are **not** present; the
repository key is a deliberately rejected placeholder. Automatic updates are
off by default.

## NOT RUN / not claimed as PASS

- Physical Windows 11 100/125/150/200%, 1366×768, 1920×1080, 4K and 60 FPS.
- Authenticode signing, SmartScreen, certificate chain.
- MSVC compilation of the new launcher and execution of GitHub workflows.
- Live GitHub update, real 60 MB network transfer, independent signature
  channel.
- Hours-long soak, antivirus/OneDrive, network/UNC paths.
- Native PDF parser fuzzing and hostile-PDF resource exhaustion.
- Other real CAD files and Inventor features not exercised on the provided
  drawing (OCG layers, complex tables, other fonts).

These items are recorded as **NOT RUN**, not as passes.
