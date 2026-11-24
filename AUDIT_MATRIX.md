# Audit compliance matrix — VECTOR PDF 0.3

Source audit: `VECTOR-PDF-0.2.1-Audit-Report.md` (24 Nov 2026).
Baseline commit: `47238fc76453780586bdf00370816ac9ccba3edd`.
Integrated branch: `deepseek/vector-pdf-0.3-hardening`.

Status vocabulary:

- **FIXED** — code changed and executed regression/QA evidence on the integrated tree.
- **PARTIAL** — mitigation present, but a named residual is not fully verified.
- **NOT RUN** — cannot be verified in this environment (hardware, certificate,
  live GitHub release, real user drawing, long soak).
- **FAIL** — reproduced and not fixed.

> This matrix is based on executed tests, real-file acceptance and measured
> output. Code inspection alone is not marked PASS. The original audit's
> Linux/synthetic numbers are not used as absolute performance evidence.

## P0 — mandatory

| # | Audit item | Status | Evidence / residual |
|---|---|---|---|
| 1 | Health-check + rollback + safe mode + complete manifest/hash | **FIXED** | Signed Ed25519 manifest, full file list/SHA-256, staged health check, readiness token, `previous` rollback, safe mode/reset, lock/retention. 76 updater tests pass. Production signing key is not installed (see #14). |
| 2 | Embedded-font glyph availability | **FIXED** | cmap/CID→GID checks; blank-C regression renders real ink with fallback; real Inventor GOST subset edited. `tests/test_core_hardening.py`, `test_qa_regressions.py`. |
| 3 | Multi-`Tj` replacement positions | **FIXED** | Native only for one `Tj`; split run cleared and one top-layer run appended. QA overlap regression (2.09 pt) now passes. |
| 4 | Recursive signature detection, direct/nested/empty | **FIXED** | Reachable-graph walk; real signature values block; empty `/FT /Sig`/`/Perms` allowed. Core + QA tests. |
| 5 | Clip, colour, z-order policy | **FIXED** | Clipped text editable with warning; native keeps clip, moved/reformatted text is top-layer with explicit fill; unknown colour spaces read-only. Real 18 MB drawing has 909/910 editable runs and raster diff only inside edit. |
| 6 | Busy page/mode lock; add after real sheet | **FIXED** | UI busy guards, disabled page list/actions, sheet-loaded guard. `test_ui_state.py`, QA U1/U2. |
| 7 | Safe PDF name, append `.pdf`, confirm actual target | **FIXED** | `collision.docx → collision.docx.pdf`; app-level exists/open-tab confirmation; destination bytes preserved on cancel. UI/QA tests. |
| 8 | Preserve long text and original font size | **FIXED** | Dynamic `maxLength`/spin range, writer accepts 100k chars and 0.01–5000 pt originals; 2000 chars/0.1/4500 pt tests pass. |
| 9 | Export edits after source file changes | **FIXED** | Private snapshot basis used for index/save; source change/delete test exports successfully; original bytes unchanged. |
| 10 | Save/Discard/Cancel, draft, journal, recovery, temp cleanup | **FIXED** | Three-way close/quit flow, `has_draft`, Save includes/commits draft, journal/.live scan, startup cleanup, clean-close deletion. UI/document/QA tests. |
| 11 | Output validation and `.bak` | **FIXED** | QPDF warnings, page count/geometry, signatures, untouched raw streams, PDFium text; `.bak` for existing targets. Deep QPDF is bounded on huge streams; full check passes on real 18 MB drawing. |
| 12 | Windows 11 / HiDPI, logs, crash handler, About/version | **PARTIAL** | About/version, rotating log, excepthook/Qt handler/faulthandler, 780×480 minimum, scrollable panels and single status line implemented; offscreen tests pass. Physical 100/125/150/200%, 4K and 1366×768 acceptance **NOT RUN**. |
| 13 | Reproducible source, tests, tools, launcher source, CI, SBOM | **PARTIAL** | `requirements*.txt`, tests, tools, launcher C/RC/manifest, deterministic package, SBOM and Windows test/release workflows are in the repository. CI runners and a fresh MSVC launcher build were **NOT RUN** locally; local ZIP uses the 0.2.1 bootstrap EXE with added VERSIONINFO/manifest via `rcedit`. |
| 14 | Code signing, launcher manifest/version, Qt notices/LGPL offer | **PARTIAL** | Launcher manifest/VERSIONINFO 0.3.0.0 and `/GS` source are present; patched local EXE reports 0.3.0.0 but is **NotSigned**. Qt third-party notices, LGPL source offer and osifont notice added. Authenticode certificate **NOT RUN**. Production update public key remains a fail-closed placeholder. |
| 15 | Exception-safe async callbacks, closed guard, exact state restore | **FIXED** | `Bridge.finish` guard, try/finally in callbacks, closed/stale-token checks, exact read-only/busy restoration. UI tests; no hangs in non-draft acceptance. |

## P1 — stabilisation

| # | Audit item | Status | Evidence / residual |
|---|---|---|---|
| 15 | Cell merge threshold; colour/clip/resource in `gap` | **FIXED** | 5/5.5/6 pt cells separate; .33 em word gap can merge; graphics state compared. Core/QA tests. |
| 16 | Per-font isolation, CMap/FontBBox limits, partial ToUnicode | **FIXED** | Bad CMap/FontBBox no longer erase valid page text; cp1252 base merged with ToUnicode; caps on size/ranges/entries. Core/QA tests. |
| 17 | Cache reverse map/current font/bounds; lazy fontTools | **PARTIAL** | Reverse maps, glyph checks and embedded program parsing cached; `Edit.bounds` has safe fallback. Full `current_font/bounds` memoization and lazy fontTools on every UI keystroke not exhaustively benchmarked. |
| 18 | Preview font/colour/bounds/warning | **FIXED** | DejaVu Sans/Serif/Mono and local fonts registered; preview uses selected/original fill, bounds for all edits, clipping warning. Offscreen test. |
| 19 | Tile order and cache statistics | **FIXED** | Paint low→high level; `peek()` for demand; hit/miss counters no longer inflated. UI test. |
| 20 | Per-page zoom/rotation, status after sheet/rotate, actual size | **PARTIAL** | Per-page view state/restore, status refresh and fit clamp implemented. Actual-size/scale-bar/UserUnit not added. |
| 21 | Global memory budget, thumbnail retry/LRU | **PARTIAL** | 256 MiB global tile ceiling, inactive trim, thumbnail LRU/retry; indexes/undo still unbounded. |
| 22 | Save-over-open-tab, friendly errors, last directory/title | **FIXED** | Destination conflict pre-check, friendly messages, last directory; tab shows export name. QA/UI tests. |
| 23 | Mode sync after open, disabled states, QSS | **FIXED** | `sync_mode`, checked action sync, busy/read-only action states; scoped empty/primary QSS. UI tests. |
| 24 | Localisation of tooltips/errors/dialogs, accessibility | **PARTIAL** | RU/EN core labels and plain dialogs; some Qt-standard buttons/tooltips remain English; screen-reader acceptance not run. |
| 25 | Offscreen Qt/QtTest, headless CI, bundle trim | **PARTIAL** | Package includes `qoffscreen.dll` and QtTest; offscreen suite passes locally. CI workflow not executed; bundle still carries tests/tools by design. |
| 26 | Plain labels, temp ACL/cleanup | **PARTIAL** | All dynamic labels/dialogs use PlainText; startup cleanup of `vector-pdf-*`/`.vector-*`; ACL restriction not implemented, temp files remain same-user readable. |
| 27 | Update consent/progress/cancel/retention/cleanup/retry | **PARTIAL** | Auto-install off by default, retention/cleanup/retry/lock; no dedicated cancelable progress dialog, only status messages. |
| 28 | Support matrix, limitations, changelog, release notes | **FIXED** | `README.md`, `KNOWN_LIMITATIONS.md`, `CHANGELOG.md`, `SECURITY.md`, this matrix and `BENCHMARKS.md`. |
| 29 | Cache committed edit resources; survive missing local font | **FIXED** | Committed edits use original font bytes or built-in fallback; local font disappearance falls back to Sans instead of blocking save/undo. Core test. |
| 30 | Region marquee, Delete state, duplicates, close/attach guards | **FIXED** | Region starts marquee before move, Delete state updates, `samefile` duplicate check, close/attach guards. UI tests. |
| 31 | Thumbnail LRU/DPR/retry and global budget | **PARTIAL** | Per-tab thumbnail LRU with retry and global tile budget; DPR-specific icon variants and a global thumbnail manager are not complete. |

## Items requiring external context

| Item | Status |
|---|---|
| Real Inventor 2023 9-page drawing | **PASS for a controlled edit** — temp copy, QPDF clean, PDFium 9 pages, text replaced, raster diff only in edit box, source unchanged. Other features/pages/soak remain limited. |
| Physical Windows 11 100/125/150/200%, 1366×768/4K | **NOT RUN** — no such hardware station. |
| Authenticode certificate / SmartScreen | **NOT RUN** — no project certificate. |
| Live GitHub release and 60 MB download | **NOT RUN** — local failure-injection only. |
| Native PDF parser fuzzing / hours-long soak / antivirus/OneDrive | **NOT RUN** — documented residual risk. |
| Production Ed25519 update key | **NOT INSTALLED** — placeholder fails closed; owner must run `tools/update_keygen.py`. |
