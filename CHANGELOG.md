# Changelog

## 0.3.0 — hardening release (in progress)

This release does not add product features. It fixes defects confirmed by the
independent 0.2.1 audit.

### PDF core

- Added real glyph availability checks for embedded simple and Type0/CID
  fonts. Native replacement is refused when a glyph cannot be verified; a
  safe embedded fallback is used instead.
- Native replacement is limited to a single `Tj` operand. Split `Tj` runs are
  replaced by one self-contained text run so absolute positions cannot
  overlap.
- Replaced/moved text is appended as an isolated top-layer run with an
  explicit fill color, preventing inherited color changes and clipped or
  hidden output. Text inside an active clip or unsafe graphics state is
  read-only.
- Signature detection now recursively walks the reachable PDF object graph,
  including direct and nested signature dictionaries. Empty signature fields
  and empty `/Perms` no longer block editing.
- CMap parsing and FontBBox handling are bounded and exception-safe; one
  malformed font or stream no longer removes all text from a page.
- Partial `/ToUnicode` maps are merged with the WinAnsi fallback.
- Adjacent table cells are no longer merged over a large gap; merging uses
  the font's real space width and matching graphics state.
- Saves are validated with QPDF, PDFium text extraction, page geometry, and
  untouched-stream comparison. A `.bak` copy of an existing destination is
  preserved.

### UI and document state

- Page/mode changes are blocked while an edit is being applied; selections
  are bound to their originating page.
- Adding text requires the real sheet size.
- Save As appends `.pdf` instead of replacing an arbitrary extension,
  confirms the actual target, and rejects targets open in another tab.
- Long source text and out-of-range font sizes are preserved.
- Closing a modified tab offers Save copy / Discard / Cancel; uncommitted
  field drafts are detected on page, mode, tab, and application close.
- A private source snapshot keeps edits exportable if the source file changes
  on disk; the original file is still never overwritten.
- Recovery journal, crash-log, About/version dialog, and startup temp cleanup
  were added.
- Automatic update installation is off by default.
- HiDPI minimum window size, scrollable properties, plain-text labels, and
  bounded caches/thumbnails were improved.

### Updater and release

- Signed manifest (Ed25519 detached signature) with a complete file/hash list.
- Full installed-build verification, startup handshake, automatic rollback,
  safe mode, and reset-update recovery.
- Failure-injection tests for missing files, tampered manifest/archive,
  broken runtime, interrupted install, and rollback.
- Launcher source with PE manifest, version resource, long-path and DPI
  awareness, built with `/GS`.
- Reproducible build inputs, SBOM, Qt third-party notices, and an LGPL source
  offer document.

### Verification

See `TEST_RESULTS.md`, `QA_REPORT.md`, `AUDIT_MATRIX.md`, and
`BENCHMARKS.md` for measured results and explicit PASS / FAIL / NOT RUN
status. Items requiring a real Inventor drawing, a 4K/HiDPI monitor, a live
GitHub release, or a project code-signing certificate are not claimed as
passed.
