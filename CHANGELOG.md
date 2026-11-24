# Changelog

## 0.3.0 — hardening release (24 Nov 2026)

This release does not add product features. It fixes defects confirmed by the
independent 0.2.1 audit and adds regression coverage. See `AUDIT_MATRIX.md`
for the item-by-item PASS / FAIL / NOT RUN evidence.

### PDF core

- Real glyph availability checks for embedded simple and Type0/CID fonts.
  Native replacement is refused when a glyph cannot be verified; a safe
  embedded fallback is used instead of writing a blank glyph.
- Native replacement is restricted to one `Tj` operand. Split `Tj`/`BT` runs
  are cleared and replaced by one self-contained text run, so absolute `Tm`
  positions cannot overlap.
- Replaced/moved text is appended as an isolated top-layer run with an
  explicit fill colour. Clipped source text keeps its clip for native edits;
  moved/reformatted text is visible outside the original clip. The UI reports
  the clipping condition.
- Text in an unsupported colour space, `gs`, non-default render mode or unsafe
  matrix is read-only with a reason.
- Signature detection recursively walks the reachable PDF object graph,
  including direct and nested signature dictionaries. Empty `/FT /Sig` fields
  and empty `/Perms` no longer block editing; real signatures still do.
- CMap parsing and FontBBox handling are bounded and exception-safe. One
  malformed font or stream no longer removes all text from a page. Partial
  `/ToUnicode` is merged over the WinAnsi fallback.
- Adjacent table cells are no longer merged over a large gap; merging uses the
  font's real space width and matching graphics state.
- Saves are validated with QPDF, PDFium text extraction, page geometry,
  signature checks, and untouched-stream comparison. A `.bak` copy of an
  existing destination is preserved. Deep QPDF tokenization is bounded on very
  large CAD streams; the fast path still performs the structural/output checks.
- Embedded font parsing is cached across page indexes.

### UI and document state

- Page/mode changes are blocked while an edit is being applied; page lists and
  actions are disabled during busy work; selections are bound to their page.
- Adding text requires the real sheet size; area queries clear stale matches.
- Save As appends `.pdf` instead of replacing an arbitrary extension, confirms
  the actual target, rejects targets open in another tab, and remembers the
  last directory.
- Long source text and out-of-range font sizes (0.1/4500 pt tested) are
  preserved; new input has a generous explicit limit rather than silent
  truncation.
- Closing a modified tab offers Save copy / Discard / Cancel; uncommitted
  field drafts are detected on page, mode, tab and application close; Save copy
  includes and commits the draft.
- A private source snapshot keeps edits exportable if the source changes or is
  deleted on disk. The original file is never overwritten.
- Recovery journal, crash/logging hooks, About/version dialog, startup temp
  cleanup, plain-text labels/dialogs, scrollable panels and a 780×480 minimum
  window were added.
- Automatic update installation is off by default; safe mode disables update
  actions.
- Tile painting goes from low to high resolution; cache stats use `peek()`;
  global tile budget and thumbnail LRU/retry improve memory behaviour.
- Preview registers DejaVu/Serif/Mono/local fonts and uses the selected colour
  and bounds.

### Updater and release

- Four exact release assets: archive, `.sha256`, `manifest.json`, and detached
  Ed25519 `manifest.json.sig`. The signature is verified before any manifest
  field is trusted; the full file list, sizes, paths and hashes are checked.
- Installed builds are re-verified before launch; a staged runtime health
  check runs before activation; a readiness token confirms the new window
  started. Failures roll back to `previous` or the portable base.
- Safe mode, reset-update, lock/stale-lock handling, retention, interrupted
  install cleanup and diagnostic update log.
- Launcher source with PE manifest, VERSIONINFO 0.3.0.0, long-path/DPI
  awareness, AppUserModelID, bounded command line and `/GS` build.
- Deterministic ZIP, bundle manifest, CycloneDX SBOM, requirements/tests/tools
  included in the portable package, qoffscreen/QtTest included for CI.
- CI test workflow on push/PR and release workflow with manifest verification,
  fail-closed signing, package smoke test and offscreen Qt smoke.
- Qt third-party notices, LGPL Corresponding Source offer, osifont notice,
  SECURITY.md and this changelog.

### Verification

- Unit/regression/Qt/updater tests pass on the integrated tree; see
  `TEST_RESULTS.md` and `QA_REPORT.md`.
- Real Inventor 2023 drawing (9 pages, 18 MB, supplied by the project owner)
  was edited on a temporary copy: QPDF clean, PDFium reopen, text replaced,
  raster diff limited to the edit bounds, source unchanged.
- Benchmark comparison is in `BENCHMARKS.md`. Deep QPDF check on the 18 MB
  real drawing takes ~8.9 s; the application uses the bounded fast path for
  edits and final save, keeping UI apply ~125 ms and save ~0.7 s.
- Physical 4K/HiDPI acceptance, Authenticode certificate signing, live GitHub
  release and native-parser fuzzing remain **NOT RUN** in this environment.
