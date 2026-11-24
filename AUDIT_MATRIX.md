# Audit compliance matrix — VECTOR PDF 0.3

Source audit: `VECTOR-PDF-0.2.1-Audit-Report.md` (24 Nov 2026).
Baseline commit: `47238fc76453780586bdf00370816ac9ccba3edd`.
Branch: `deepseek/vector-pdf-0.3-hardening`.

Status vocabulary:

- **FIXED** — code changed and a regression test passes on the integrated 0.3 tree.
- **CONFIRMED** — reproduced on 0.2.1; fix pending or failed.
- **ALREADY FIXED** — not present in the baseline source.
- **READ-ONLY** — the safe policy deliberately refuses editing and explains why.
- **NOT RUN** — cannot be verified in this environment (hardware, certificate,
  live GitHub release, real Inventor drawing).
- **PARTIAL** — mitigation present, residual risk documented.

> This matrix is filled from executed tests and measurements. Code inspection
> alone does not count as PASS.

## P0 — mandatory

| # | Audit item | Baseline | 0.3 status | Evidence / test |
|---|---|---|---|---|
| 1 | Update health-check, rollback, safe mode, complete manifest/hash | CONFIRMED |  | `tests/test_updater_hardening.py` |
| 2 | Embedded-font glyph availability | CONFIRMED |  | `tests/test_core_hardening.py` |
| 3 | Multi-`Tj` replacement positions | CONFIRMED |  | core tests |
| 4 | Recursive digital-signature detection, direct/nested/empty | CONFIRMED |  | core tests |
| 5 | Clip, colour, z-order policy | CONFIRMED |  | core tests |
| 6 | Busy page/mode lock; add after real sheet | CONFIRMED |  | `tests/test_ui_state.py` |
| 7 | Safe PDF name, append `.pdf`, confirm actual target | CONFIRMED |  | UI tests |
| 8 | Preserve long text and original font size | CONFIRMED |  | UI tests |
| 9 | Export edits after source file changes (snapshot) | CONFIRMED |  | document tests |
| 10 | Save/Discard/Cancel, draft, autosave, journal, recovery, temp cleanup | CONFIRMED |  | UI/document tests |
| 11 | Output validation and `.bak` backup | CONFIRMED |  | core tests |
| 12 | Windows 11 / HiDPI, logs, crash handler, About/version | CONFIRMED |  | offscreen Qt tests; physical HiDPI NOT RUN |
| 13 | Reproducible build inputs, tests, tools, launcher source, CI, SBOM | CONFIRMED |  | CI config, package build |
| 14 | Code signing, launcher manifest/version, Qt notices/LGPL offer | CONFIRMED |  | notices added; signature NOT RUN without certificate |
| 15 | Exception-safe async callbacks, closed guard, exact state restore | CONFIRMED |  | UI tests |

## P1 — stabilisation

| # | Audit item | Baseline | 0.3 status | Evidence / test |
|---|---|---|---|---|
| 15 | Cell merge threshold and graphics state in `gap` | CONFIRMED |  | core tests |
| 16 | Per-font isolation, CMap/FontBBox limits, partial ToUnicode merge | CONFIRMED |  | core tests |
| 17 | Cache reverse map/current font/bounds; lazy fontTools | CONFIRMED |  | benchmarks |
| 18 | Preview font/colour/bounds/warning | CONFIRMED |  | UI tests |
| 19 | Tile paint order and cache statistics | CONFIRMED |  | UI tests |
| 20 | Per-page zoom/rotation, status after sheet/rotate | CONFIRMED |  | UI tests |
| 21 | Global memory budget, thumbnail retry/LRU | CONFIRMED |  | code + tests |
| 22 | Save-over-open-tab detection, friendly errors, last directory | CONFIRMED |  | UI tests |
| 23 | Mode sync after open, disabled states, QSS | CONFIRMED |  | UI tests |
| 24 | Localisation of tooltips/errors/dialogs, accessibility | PARTIAL |  | documented residual |
| 25 | Offscreen Qt, headless CI, bundle trim | PARTIAL |  | packaging/CI |
| 26 | Plain-text labels, temp ACL/cleanup | CONFIRMED |  | UI/document tests |
| 27 | Update consent/progress/cancel/retention/cleanup/retry | CONFIRMED |  | updater tests |
| 28 | Support matrix, limitations, changelog, release notes | CONFIRMED |  | docs |
| 29 | Cache committed edit resources; survive missing local font | CONFIRMED |  | core tests |
| 30 | Region marquee, Delete state, duplicates, close/attach guards | CONFIRMED |  | UI tests |
| 31 | Thumbnail LRU/DPR/retry and global budget | CONFIRMED |  | code + tests |

## Audit limitations carried into 0.3

These are explicitly **not** claimed as PASS:

- Real Inventor 2023 nine-page drawing: not present in the repository.
  Synthetic PDFs and the stored acceptance procedure are used.
- Physical 4K/HiDPI and 125/150/200% scaling on Windows 11: offscreen Qt
  tests and code review only.
- Code signing / Authenticode: no project certificate in this environment.
- Live GitHub release / real 50 MB update download: local failure-injection
  only.
- Hours-long soak, antivirus/OneDrive interaction, and native PDF fuzzing:
  not run.
