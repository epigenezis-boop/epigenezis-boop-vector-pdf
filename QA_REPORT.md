# Independent QA report — VECTOR PDF 0.3.0

Role: independent QA/regression (Agent D), integrated by the technical lead.
Date: 24 Nov 2026.
Baseline audited: VECTOR PDF 0.2.1, commit `47238fc`.
Integrated branch: `deepseek/vector-pdf-0.3-hardening`.
Verification was performed against the integrated working tree. This report is
committed by the integration lead after the one QA-found defect was fixed and
covered by a regression.

**Signoff verdict: PASS for the tested scope.** The automated suite, updater,
output validity and the real-drawing edit pass. QA found one real UI
cache-invalidation defect; the lead fixed it and added a regression, and QA's
reproducer now reports `invalidated >= 1`. Physical HiDPI, Authenticode, a live
GitHub release and long soak remain NOT RUN. This is not an unconditional
release PASS.

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

Final full-suite run after the cache-invalidation fix:

```
183 passed, 1 skipped, 3 warnings in 10.48s
```

The single skip is the env-gated real-Inventor test
(`tests/test_editor.py:67`), which requires private `VECTOR_TEST_PDF` and
`VECTOR_TEST_STAMP` values. The lead separately reported that env-gated test as
`1 passed in 23.92s`; that exact invocation was not reproducible here because
the private stamp coordinates are not in the QA environment. Independent QA
repeated the real-drawing edit with a separate external script; see below.

The D-only suite (core regressions plus QA acceptance) on the integrated tree:

```
47 passed in 5.48s
```

The independent baseline suite (same tests against 0.2.1) produced:

```
35 failed, 5 passed, 7 skipped in 9.18s
```

Those baseline failures are the confirmed audit defects; `QA_BASELINE.md`
contains the full table and raw excerpts. The skips were APIs absent on 0.2.1,
not passes.

An earlier Phase-2 attempt hung because the old D draft tests patched only
`QMessageBox.question`, while the integrated UI calls `window.ask` →
`QMessageBox.exec`. The D tests were corrected to patch `window.ask` /
`window.message` with bounded recorders. This was a test-harness correction, not
a production change. No real modal dialogue remains in the QA tests.

## Defect found during independent verification — now fixed

### FIXED — edited tiles were not invalidated in the canvas cache

- **Location:** `vector_pdf/ui.py` in `Workspace.render_ready`.
- **Cause found by QA:** `if page not in self.sheets: continue` compared an
  integer page number with the list of `Sheet` objects. `0 in self.sheets` is
  false, so the cache invalidation block was skipped.
- **Observed before fix:** after Apply, `document.metrics.invalidated == 0`,
  cache keys/images were unchanged, and the canvas could keep showing old text
  until zoom/page/cache eviction. Saved output was correct.
- **Fix applied by lead:** bounds check
  `if not 0 <= page < len(self.sheets): continue`.
- **Regression:** `tests/test_ui_state.py::test_render_ready_invalidates_edited_tiles`
  asserts `metrics.invalidated >= 1` and removal of the overlapping tile.
- **Re-test:** the QA reproducer now reports `invalidated = 1`; the real-drawing
  UI flow reports `invalidated = 1` after Apply.
- **Impact of original defect:** on-screen result after Apply/Undo/Redo could be
  stale. It was not a saved-PDF corruption issue.

## Independent real-drawing verification

Drawing: 9 pages, 18.0 MB, Autodesk Inventor Professional 2023, supplied by the
project owner. It remains outside the repository and was used only through
temporary copies. No user drawing, page raster or user text fixture is
committed.

Procedure and result from the independent script:

1. Indexed all 9 pages. Page 1: 910 text runs, 909 editable; one run is
   read-only because of a non-default text render mode
   (`clipped text; text render mode`). Other pages: all runs editable.
   Per-page timings: 2.918 s, 1.009 s, 0.279 s, 0.701 s, 0.223 s, 0.640 s,
   1.398 s, 0.677 s, 0.040 s; total 7.887 s.
2. Replaced `Размеры` (run id 8, position `(2851.20, 2344.80)`, 13.68 pt,
   original embedded GOSTCommonItalic subset) with `Тест`; no font substitution.
3. `write_copy(..., verify='full', balanced_pages={0})` completed in 8.893 s.
   The source temp copy and the original file kept their bytes and stamps; the
   original SHA-256 stayed
   `137433306fe7224af012b5323d67a192a8b269b46ba0d51b4c47adf30d85d752`.
4. QPDF `check_pdf_syntax()` returned `[]`; PDFium reopened 9 pages with
   unchanged page sizes.
5. Bounded text extraction around the edit contains `Тест` and no longer
   contains `Размеры`. Pages 1–8 kept byte-identical raw content streams.
6. Page 0 raster comparison at 1.5×: 786 changed pixels, changed bounds
   `[2851.33, 2342.00, 2905.33, 2354.67]`, edit bounds
   `[2849.04, 2341.83, 2909.64, 2358.52]`, **0 changed pixels outside the
   edit**. (The report intentionally does not claim an all-9-page independent
   raster pass; untouched pages were verified byte-for-byte instead.)

UI offscreen runs on the same drawing:

- independent QA run before the cache fix: open 1.730 s; first tiles 3.245 s;
  page-0 index 2.904 s; Apply 2.871 s (`metrics.edit` 2652.2 ms); Undo 0.977 s;
  Redo 3.633 s; Save copy 3.351 s; `metrics.invalidated == 0` (the defect above).
- lead clean run after the cache fix, using the real UI query/index path:
  open 1870 ms; UI index 3041 ms; Apply 324 ms (`metrics.edit` 61 ms);
  `invalidated = 1`; Save copy 846 ms (`metrics.saved` 822 ms).
- The difference between the two timing sets is load/cache scope; both were
  measured offscreen. The report does not claim a single universal latency.
- RSS in the independent run: 92.4 MiB before open → 211.2 after open →
  241.7 after index → 238.8 after Apply → 136.8 after save. Offscreen FPS
  samples of 0.8/0.3 are **not** a 60 FPS or physical-monitor acceptance result.
- PDFium reopened the saved copy with 9 pages; bounded text verification
  passed; `window.error` list empty.

Full QPDF syntax/deep validation of this 18 MB file is expensive (about 8.9 s
in the full `verify='full'` write). The application uses a bounded fast path for
large CAD streams and reserves the deep QPDF check for small files/QA; this is
documented in `BENCHMARKS.md` and `KNOWN_LIMITATIONS.md`.

## Defect matrix — P0

Legend: **PASS** = executed evidence; **PARTIAL** = tested scope passed and a
named residual is NOT RUN (not a full PASS); **FAIL** = reproduced defect;
**NOT RUN** = no evidence. No item is marked PASS from code inspection alone.

| # | Audit item | QA status | Evidence / residual |
|---|---|---|---|
| 1 | Update health-check/rollback/safe mode/full manifest | **PASS** (ephemeral signed key); production key/live GitHub **NOT RUN** | `test_qa_acceptance` signed bad-signature, bad-checksum, partial repair, dead-child dispatch; `test_updater_hardening` full suite. |
| 2 | Embedded-font glyph availability | **PASS** | `test_subset_font_missing_glyph_still_renders_new_character`; real drawing original-font edit. |
| 3 | Multi-`Tj` positions | **PASS** | `test_split_tj_replacement_does_not_overlap` (old 2.09 pt overlap now absent). |
| 4 | Recursive signatures/direct/nested/empty | **PASS** | Direct/nested/array rejection and empty `/FT /Sig`/`/Perms` acceptance tests. |
| 5 | Clip/colour/z-order policy | **PASS** | Clip/colour regressions; real drawing clipped run editable with warning; existing z-order/core tests. |
| 6 | Busy page/mode/add guards | **PASS** | QA busy-page, add-before-sheet; `test_ui_state`. |
| 7 | Safe Save suffix/overwrite | **PASS** | QA append/confirm; open-tab rejection; bytes preserved on cancel. |
| 8 | Long text/original size | **PASS** | 2000-char and 0.1/4500 pt tests. |
| 9 | Source-change export | **PASS** | Snapshot basis test; changed source exports successfully. |
| 10 | Save/Discard/Cancel, draft, journal, recovery, cleanup | **PASS** | Draft preservation, three-way close, journal, `scan_recovery`/dead `.live`, stale cleanup, clean close. |
| 11 | Output validation and `.bak` | **PASS** | QPDF/PDFium/geometry/untouched streams/raster; `.bak`/atomic rejection tests; real drawing full check. |
| 12 | Windows/HiDPI/logs/crash/About | **PARTIAL** | Logs/About/offscreen tests PASS; physical 100/125/150/200%, 1366×768, 4K **NOT RUN**. |
| 13 | Source/tests/tools/launcher/CI/SBOM | **PARTIAL** | Repository tests/tools/lock present and suite runs; CI runner and fresh MSVC launcher build **NOT RUN**. |
| 14 | Signing/launcher manifest/Qt notices/LGPL offer | **PARTIAL** | Launcher manifest/version/notices present per repo; Authenticode certificate and production update key **NOT RUN/NOT INSTALLED**. |
| 15 | Async exception safety/closed guard | **PASS** | Full UI suite includes failed-callback/read-only restoration tests; no hangs in final run. |

## Defect matrix — P1

| # | Audit item | QA status | Evidence / residual |
|---|---|---|---|
| 15 | Cell merge threshold; colour/clip/resource in `gap` | **PASS** | 5/5.5/6 pt cells separate; word-gap merge allowed. |
| 16 | Per-font isolation, CMap/FontBBox limits, partial ToUnicode | **PASS** | CMap overflow/FontBBox and cp1252 fallback tests. |
| 17 | Cache reverse map/current font/bounds; lazy fontTools | **PASS** (functional) | Bounds/fallback tests; exhaustive keystroke/performance `NOT RUN`. |
| 18 | Preview font/colour/bounds/warning | **PARTIAL** | DejaVu registration PASS; preview colour/clip was not independently raster-checked. |
| 19 | Tile order and cache statistics | **PASS** | Paint-order test passes; cache-invalidation defect found by QA was fixed, and `test_render_ready_invalidates_edited_tiles` now asserts `metrics.invalidated >= 1`. |
| 20 | Per-page zoom/rotation, status, actual size | **PARTIAL** | Per-page zoom/rotation test PASS; actual-size/UserUnit `NOT RUN`. |
| 21 | Global memory budget, thumbnail retry/LRU | **PARTIAL** | Code/tests for trim/retry; no memory soak or global thumbnail-manager measurement. |
| 22 | Save-over-open-tab, friendly errors, last dir/title | **PASS** | QA test requires unchanged destination and friendly message/ValueError, no raw `PermissionError`. |
| 23 | Mode sync, disabled states, QSS | **PASS** | UI state suite, offscreen theme/action checks. |
| 24 | Localization/a11y | **PARTIAL** | RU/EN core labels; screen-reader/keyboard-only acceptance `NOT RUN`. |
| 25 | Offscreen Qt/QtTest, headless CI, bundle trim | **PARTIAL** | Offscreen suite PASS in dev venv; portable ZIP/CI runner `NOT RUN`. |
| 26 | Plain labels; temp ACL/cleanup | **PARTIAL** | Plain-text labels and stale temp cleanup PASS; restricted ACL `NOT RUN`. |
| 27 | Update consent/progress/cancel/retention/cleanup/retry | **PARTIAL** | Failure injection/retention/cleanup PASS; cancelable progress dialog `NOT RUN`. |
| 28 | Support matrix, limitations, changelog, release notes | **PARTIAL** | Documents are present; release-note accuracy/content was not independently validated. |
| 29 | Cache committed edit resources; local font fallback | **PASS** (functional) | Core fallback tests; performance not benchmarked. |
| 30 | Region marquee, Delete state, duplicates, guards | **PARTIAL** | UI state tests PASS; 8.3/hardlink alias was not separately exercised. |
| 31 | Thumbnail LRU/DPR/retry, global budget | **PARTIAL** | Retry/LRU paths exist; DPR and memory measurements `NOT RUN`. |

## Output-validity evidence

For every automated save in the suite:

- result reopens with pikepdf/QPDF and PDFium;
- page count and page boxes/rotation are unchanged;
- real signatures are absent; empty signature fields do not block;
- untouched page content streams are compared byte-for-byte;
- edited text is re-extracted from the output;
- existing destination bytes survive a rejected save;
- `.bak` is created for an overwritten destination;
- raster comparisons cover glyph ink, not just extracted text (subset glyph,
  multi-`Tj`, clip, colour, real drawing).

## Updater failure injection

`tests/test_updater_hardening.py` (76 updater tests with `test_updater.py`)
covers:

- RFC 8032 Ed25519 vectors and placeholder fail-closed;
- canonical manifest, bad signature/hash/manifest/version/archive;
- missing `python312.zip`/PySide6/ui/assets, extra/missing files, size/hash
  mismatch, traversal/ADS/reserved names/symlinks/duplicates;
- partial/broken version quarantine and reinstall, interrupted incoming cleanup,
  retention, lock;
- staged health-check success/failure, dispatch dead child/timeout, readiness
  token, rollback to base/previous, rehash/tampered manifest rejection;
- safe mode/reset, package manifest and deterministic archive.

The production private key and a live GitHub release are **not** present; the
repository key is a deliberately rejected placeholder. Automatic updates are
off by default. QA used an ephemeral test Ed25519 keypair only in tests; no
secret is committed.

## NOT RUN / not claimed as PASS

- Physical Windows 11 100/125/150/200%, 1366×768, 1920×1080, 4K and 60 FPS.
- Authenticode signing, SmartScreen, certificate chain.
- MSVC compilation of the launcher and execution of GitHub workflows.
- Live GitHub update, real 60 MB network transfer, independent signature
  channel.
- Hours-long soak, antivirus/OneDrive, network/UNC paths.
- Native PDF parser fuzzing and hostile-PDF resource exhaustion.
- Other real CAD files and Inventor features not exercised on the provided
  drawing (OCG layers, complex tables, other fonts).
- All-9-page independent raster comparison; the independent real-drawing run
  did page-0 raster plus byte-identical untouched streams.

These items are recorded as **NOT RUN**, not as passes.

## Files and repository hygiene

- QA edited only `tests/test_qa_acceptance.py`, `tests/test_qa_regressions.py`
  and this `QA_REPORT.md`; no production, tools, CI or documentation files were
  edited by QA. No commit was made by QA.
- The external 18 MB drawing is not in the repository, not copied into a
  fixture and not included in any portable archive. No page raster or user text
  fixture is committed.
- No private update key or secret is committed. The test key is an ephemeral
  derived test key already present in `tests/test_updater_hardening.py`; the
  production update public key remains a fail-closed placeholder.
