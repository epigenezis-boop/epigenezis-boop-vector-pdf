# QA baseline — VECTOR PDF 0.3 hardening

**Role:** Agent D — independent QA/regression.
**Worktree:** `C:\Users\dshtrauh\.local\share\opencode\worktree\e56455\agent-d-qa`
**Branch:** `agent-d-qa`
**Baseline commit:** `47238fc` (`Match Orbita solar styling and add PDF text slant`)
**Date:** 2026-11-24
**Scope:** synthetic PDFs and offscreen Qt only. No user drawing, no private data, no production-code changes.

This is a **baseline** record. The failing tests state the safe behaviour required from
0.3; they are expected to fail on 0.2.1. They must not be weakened to make the
baseline green. `QA_REPORT.md` (Phase 2) will use the same tests against the
integrated 0.3 branch and add raster/output evidence.

---

## Environment

| Component | Value |
|---|---|
| OS | Windows 11 (`Windows-11-10.0.22621-SP0`), build 22621 |
| Python | CPython 3.12.10 (MSC v.1943 64-bit) |
| Qt / PySide6 | Qt 6.10.3 / PySide6 6.10.3 |
| pikepdf / QPDF | 10.18.2 / 12.4.2 |
| pypdfium2 / PDFium | 5.15.2 / 159.0.8149.0 (`sourcebuild-toolchained`) |
| fontTools | 4.68.0 |
| numpy | 2.5.5 |
| rtree / psutil / Pillow | 1.4.1 / 7.2.2 / 13.0.0 |
| pytest | 9.2.0 |
| Qt platform | `offscreen` (dev venv has `qoffscreen`; the 0.2.1 portable ZIP does not) |

The interpreter is the shared dev venv:
`C:\Users\dshtrauh\Projects\VECTOR-PDF\.venv\Scripts\python.exe`.

## Command

```powershell
cd C:\Users\dshtrauh\.local\share\opencode\worktree\e56455\agent-d-qa
$env:PYTHONPATH='.'
$env:QT_QPA_PLATFORM='offscreen'
C:\Users\dshtrauh\Projects\VECTOR-PDF\.venv\Scripts\python.exe -m pytest -q `
  tests/test_qa_regressions.py tests/test_qa_acceptance.py
```

Result on `47238fc`:

```
35 failed, 5 passed, 7 skipped in 9.18s
```

The five passes are output-validity/keep-working checks and two updater checks
that are not part of the confirmed defects (bad checksum rejection, clean
workspace removal). The seven skips are APIs that do not exist on the baseline;
they are recorded as **NOT RUN**, not as passes.

---

## Test files

| File | Purpose |
|---|---|
| `tests/test_qa_regressions.py` | Synthetic-PDF core regressions: glyph availability, multi-Tj widths, signatures, clip/colour, cell merging, partial ToUnicode, CMap/FontBBox robustness, output validity and atomic rejection. |
| `tests/test_qa_acceptance.py` | Updater manifest/health/rollback, offscreen UI state/save/draft/preview/tile-order/save-over-open-tab, journal/recovery probes. |

All PDF fixtures are generated in the test process with pikepdf + fontTools.
The embedded subset in the glyph test is deliberately cut to `A,B`; the
multi-Tj fixture has `A` and `B` in separate `BT/ET` blocks with absolute `Tm`;
the signature fixtures use direct dictionaries that never become indirect
objects; the clip fixture uses `q … re W n BT … ET Q`; the colour fixture sets
`1 0 0 rg` after `Tj` inside the same `BT`. These are the same mechanisms
described in the audit, not copies of any user file.

---

## Full baseline result table

| # | Test id | Requirement | Baseline |
|---|---|---|---|
| R1 | `test_subset_font_missing_glyph_still_renders_new_character` | Edit `AB → CB` must render a real `C`, not just extract text. | **FAIL — CONFIRMED.** Extracted text is `CB`; C-cell ink = 0. |
| R2 | `test_split_tj_replacement_does_not_overlap` | Split `Tj` run replaced by `WB` must not overlap. | **FAIL — CONFIRMED.** PDFium boxes `W=50.40..61.47`, `B=59.38..65.59`; overlap 2.09 pt. |
| R3 | `test_direct_signature_dictionary_is_rejected` | Direct `/Type /Sig` with `/ByteRange` must make the PDF read-only. | **FAIL — CONFIRMED.** `write_copy` accepts it and writes the edit. |
| R4 | `test_nested_direct_signature_in_field_is_rejected` | Direct signature value inside a direct AcroForm field must be found. | **FAIL — CONFIRMED.** `write_copy` accepts it. |
| R5 | `test_direct_signature_in_array_is_rejected` | Signature dictionary inside a direct array must be found. | **FAIL — CONFIRMED.** `write_copy` accepts it. |
| R6 | `test_empty_signature_field_does_not_block_editing` | Empty `/FT /Sig` field without `/V` must not block editing. | **FAIL — CONFIRMED.** `write_copy` raises “Signed PDFs are read-only”. |
| R7 | `test_empty_perms_does_not_block_editing` | Empty `/Perms` dictionary must not block editing. | **FAIL — CONFIRMED.** `write_copy` raises “Signed PDFs are read-only”. |
| R8 | `test_moving_text_outside_clip_is_safe` | Text moved out of `q … re W n … Q` must be read-only or visible. | **FAIL — CONFIRMED.** Move succeeds; ink at new position = 0. |
| R9 | `test_replaced_text_keeps_original_fill_colour` | Replacement/move must keep the original black fill. | **FAIL — CONFIRMED.** Rendered BGR mean `[28.2, 28.2, 255.0]` — red, inherited after `Tj`. |
| R10 | `test_adjacent_cells_with_five_to_six_point_gap_stay_separate[5.0]` | 5 pt gap at 12 pt must be separate runs. | **FAIL — CONFIRMED.** One run `"Part A Part B"`. |
| R11 | `…[5.5]` | 5.5 pt gap must be separate runs. | **FAIL — CONFIRMED.** One run. |
| R12 | `…[6.0]` | 6 pt gap must be separate runs. | **FAIL — CONFIRMED.** One run. |
| R13 | `test_typographic_word_gap_may_merge_with_a_space` | ~0.33 em word gap may merge with a space, or stay separate. | **PASS — NOT REPRODUCED.** Merged as `"Alpha Beta"` (acceptable). |
| R14 | `test_partial_tounicode_falls_back_to_cp1252` | Partial `/ToUnicode` must not erase WinAnsi text. | **FAIL — CONFIRMED.** `"Hello"` yields no runs. |
| R15 | `test_cmap_overflow_does_not_erase_valid_page_text` | `bfrange <00> <02> <FFFF>` must not kill the page index. | **FAIL — CONFIRMED.** `OverflowError: int too big to convert`; valid `"OK"` run lost. |
| R16 | `test_malformed_fontbbox_does_not_erase_valid_page_text` | 3-value `/FontBBox` must not kill the page index. | **FAIL — CONFIRMED.** `ValueError: not enough values to unpack (expected 4, got 3)`; valid `"OK"` run lost. |
| R17 | `test_saved_copy_is_valid_and_untouched_pages_are_byte_identical` | Output must pass QPDF syntax, PDFium reopen, geometry, unchanged-page bytes. | **PASS — NOT REPRODUCED** for the simple case. `write_copy(verify=…)` is absent on baseline; application-side verification remains NOT RUN. |
| R18 | `test_rejected_edit_preserves_existing_destination` | Rejected edit must not touch a pre-existing destination. | **PASS — NOT REPRODUCED.** Existing bytes preserved. |
| R19 | `test_write_copy_exposes_verification_contract` | `write_copy(..., verify=True, original_source=…)` must exist. | **SKIP — NOT RUN.** No `verify`/`original_source` parameters on baseline. |
| A1 | `test_five_file_update_stub_is_rejected` | Five-file stub without `python312.zip`/PySide6 must be rejected. | **FAIL — CONFIRMED.** `check_installed` accepts the five-file stub. |
| A2 | `test_complete_runtime_without_manifest_is_not_trusted` | Complete-looking runtime without a manifest/hashes must not be trusted. | **FAIL — CONFIRMED.** `check_installed` accepts it. |
| A3 | `test_manifest_with_bad_signature_is_rejected` | Signed manifest; bad signature rejected. | **SKIP — NOT RUN.** Signed-manifest API absent on baseline. |
| A4 | `test_partial_version_directory_is_repaired` | Half-populated `versions/<v>` must be quarantined/reinstalled. | **FAIL — CONFIRMED.** Partial directory is trusted; archive is never unpacked. |
| A5 | `test_dispatch_rejects_child_that_never_becomes_ready` | Dispatch must health-check and roll back a dead child. | **FAIL — CONFIRMED.** `dispatch()` returns `True`; active pointer stays `9.9.9`. |
| A6 | `test_bad_checksum_does_not_change_active_version` | Bad SHA-256 must not change `active`. | **PASS — NOT REPRODUCED.** Existing checksum path is fail-closed. |
| A7 | `test_ui_main_exposes_ready_token_handshake` | `ui.main(ready_token=None)` readiness handshake must exist. | **SKIP — NOT RUN.** No `ready_token` parameter on baseline. |
| S1 | `test_source_change_can_still_export_from_snapshot` | Source change must not make edits unexportable; immutable basis. | **SKIP — NOT RUN.** No `Document.ensure_basis`/`basis`/`source_stamp` on baseline. |
| U1 | `test_busy_page_switch_is_ignored` | Page switch while async save is busy must be ignored. | **FAIL — CONFIRMED.** `current` changes from 0 to 1 while `busy=True`. |
| U2 | `test_add_before_real_sheet_is_blocked` | `+ Text` before real `Sheet` must be blocked. | **FAIL — CONFIRMED.** An `Edit` is created from placeholder geometry. |
| U3 | `test_long_text_is_not_silently_truncated` | 2000-char text must not be silently truncated. | **FAIL — CONFIRMED.** Text truncated to 1500. |
| U4 | `test_extreme_size_is_not_silently_clamped[0.1]` | 0.1 pt must not be clamped. | **FAIL — CONFIRMED.** Clamps to 0.25 pt. |
| U5 | `test_extreme_size_is_not_silently_clamped[4500.0]` | 4500 pt must not be clamped. | **FAIL — CONFIRMED.** Clamps to 4000 pt. |
| U6 | `test_save_as_appends_pdf_instead_of_overwriting_other_pdf` | `report.docx` must become `report.docx.pdf`, not `report.pdf`. | **FAIL — CONFIRMED.** Existing `report.pdf` overwritten. |
| U7 | `test_save_as_confirms_existing_final_pdf` | Existing final target must be confirmed; Cancel must not overwrite. | **FAIL — CONFIRMED.** No confirmation; test times out waiting for a prompt. |
| U8 | `test_close_modified_tab_offers_save_discard_cancel` | Close must offer Save/Discard/Cancel. | **FAIL — CONFIRMED.** Dialogue is only `Yes|No`; no Save. |
| U9 | `test_uncommitted_draft_is_seen_by_confirm` | Uncommitted field draft must count as dirty. | **FAIL — CONFIRMED.** `confirm()` returns `True` and would close. |
| U10 | `test_uncommitted_draft_survives_page_switch` | Draft must survive page switch or block it. | **FAIL — CONFIRMED.** Page switches to 1; selection/text cleared. |
| U11 | `test_uncommitted_draft_survives_view_mode` | Draft must survive View mode or block it. | **FAIL — CONFIRMED.** Selection cleared; mode `view`. |
| U12 | `test_uncommitted_draft_blocks_tab_close` | Draft must block tab close. | **FAIL — CONFIRMED.** Tab closes without a prompt. |
| U13 | `test_tiles_are_painted_low_resolution_first` | Paint low-resolution tiles before high-resolution. | **FAIL — CONFIRMED.** Pixel is blue (low-res) where red (high-res) is available. |
| U14 | `test_dejavu_preview_fonts_are_registered` | DejaVu Sans/Serif/Mono registered for preview. | **FAIL — CONFIRMED.** Only `Exo 2`, `Russo One`, `osifont`. |
| U15 | `test_read_only_pdf_can_save_a_copy` | Read-only/signed PDF can still Save copy. | **FAIL — CONFIRMED.** Save action disabled when `read_only=True`. |
| U16 | `test_saving_over_pdf_open_in_another_tab_is_rejected` | Destination open in another tab rejected with friendly error. | **FAIL — CONFIRMED.** Raw `PermissionError` / `WinError 5`; no pre-check. |
| U17 | `test_journal_exists_after_successful_edit` | Journal/autosave file after successful edit. | **SKIP — NOT RUN.** No journal/autosave on baseline. |
| U18 | `test_recovery_is_offered_after_simulated_crash` | Recovery offered after crash. | **SKIP — NOT RUN.** No recovery API on baseline. |
| U19 | `test_stale_temp_workspace_is_cleaned_on_startup` | Stale `%TEMP%\vector-pdf-*` cleaned at startup. | **SKIP — NOT RUN.** No startup-cleanup API on baseline. |
| U20 | `test_clean_close_removes_workspace` | Clean close removes the temporary workspace. | **PASS — NOT REPRODUCED.** Workspace is removed. |

### Exact baseline failure excerpts

```
R1  AssertionError: new C glyph is blank in the rendered PDF   (c_ink = 0; extracted text == "CB")
R2  AssertionError: replaced glyph cells overlap by 2.09 pt
R3  Failed: DID NOT RAISE ValueError                (direct Root.MySig accepted)
R4  Failed: DID NOT RAISE ValueError                (nested direct field accepted)
R5  Failed: DID NOT RAISE ValueError                (direct array accepted)
R6  ValueError: Editing can invalidate signatures. Signed PDFs are read-only.
R7  ValueError: Editing can invalidate signatures. Signed PDFs are read-only.
R8  AssertionError: moved text ... produced no visible ink   (new_ink = 0)
R9  AssertionError: replacement text changed colour, BGR mean=[28.20, 28.20, 255.00]
R10 AssertionError: a 5.0 pt gap at 12 pt merged adjacent cells: ['Part A Part B']
R11 AssertionError: a 5.5 pt gap ... merged ...
R12 AssertionError: a 6.0 pt gap ... merged ...
R14 AssertionError: partial /ToUnicode erased valid cp1252 text   (runs == [])
R15 OverflowError: int too big to convert              (fonts.py cmap bfrange)
R16 ValueError: not enough values to unpack (expected 4, got 3)  (FontBBox)
A1  Failed: DID NOT RAISE ValueError                (five-file stub accepted)
A2  Failed: DID NOT RAISE ValueError                (runtime without manifest accepted)
A4  AssertionError: partial version directory was trusted instead of repaired
A5  AssertionError: dispatch accepted a dead child process   (returned True)
U1  AssertionError: page changed while an async save was busy   (current == 1)
U2  AssertionError: text was added using placeholder sheet geometry (Edit page=1)
U3  AssertionError: long text was silently truncated   (1500 != 2000)
U4  AssertionError: size 0.1 pt was silently clamped to 0.25
U5  AssertionError: size 4500.0 pt was silently clamped to 4000.0
U6  AssertionError: a non-.pdf name silently overwrote an existing PDF
U7  AssertionError: overwrite confirmation missing
U8  AssertionError: close dialogue lacks StandardButton.Save
U9  AssertionError: confirm ignored an uncommitted draft
U10 AssertionError: page switch discarded the uncommitted draft
U11 AssertionError: switching to View discarded the uncommitted draft
U12 AssertionError: tab closed with an uncommitted draft without a prompt
U13 AssertionError: low-resolution tile was painted over the high-resolution tile: #0000ff
U14 AssertionError: DejaVu Sans is not registered for preview
U15 AssertionError: Save copy is disabled for a read-only/signed PDF
U16 AssertionError: raw platform error instead of a friendly rejection: PermissionError(13, ...)
```

---

## Defect matrix — audit P0/P1

Statuses: **CONFIRMED/FAIL** = reproduced by a failing baseline test;
**NOT REPRODUCED** = baseline test passes (no defect in that scenario);
**NOT RUN** = no baseline API/hardware/release artifact, or out of this phase;
**NEEDS USER FILE** = cannot be checked without the real Inventor drawing.

### P0 (audit §9)

| # | Audit measure | Test id(s) | Baseline |
|---|---|---|---|
| 1 | Health-check + rollback + safe mode + full manifest/hash | A1, A2, A4, A5, A6; A3, A7 skipped | **CONFIRMED/FAIL** for manifest, partial version, dead-child handoff; signed manifest and `ui.main` handshake **NOT RUN**; safe mode **NOT RUN** |
| 2 | Embedded-font glyph availability | R1 | **CONFIRMED/FAIL** (blank C) |
| 3 | Forbid native multi-Tj or recompute positions | R2 | **CONFIRMED/FAIL** (2.09 pt overlap) |
| 4 | Recursive signature check + direct/nested/empty tests | R3–R7 | **CONFIRMED/FAIL** (direct/nested/array bypass; empty false positives) |
| 5 | Track clip/colour/z-order; read-only or warn | R8, R9; z-order not covered | **CONFIRMED/FAIL** for clip and colour; z-order **NOT RUN** |
| 6 | Block page switch/mode while busy; guard add on `sheet_loaded` | U1, U2 | **CONFIRMED/FAIL** |
| 7 | Save: append `.pdf`, confirm actual target, exists check | U6, U7 | **CONFIRMED/FAIL** |
| 8 | Preserve long text and out-of-range size | U3, U4, U5 | **CONFIRMED/FAIL** |
| 9 | Export current temp version on source change; watcher/snapshot | S1 (skipped) | **NOT RUN** (`Document.ensure_basis`/snapshot API absent) |
| 10 | Save/Discard/Cancel, `has_draft`, autosave/journal/recovery, temp cleanup | U8–U12, U17–U20 | **CONFIRMED/FAIL** for close/draft; journal/recovery/stale cleanup **NOT RUN**; clean workspace removal **NOT REPRODUCED (PASS)** |
| 11 | Output validation (QPDF, PDFium, text/stream diff) + `.bak` | R17, R18, R19 | **NOT REPRODUCED (PASS)** for simple output; `verify=`/`original_source=` contract **NOT RUN**; application-side validation **NOT RUN** |
| 12 | Windows 11 physical/HiDPI acceptance; logs/crash; version/About | — | **NOT RUN / NEEDS HARDWARE.** No 4K/HiDPI monitor, no physical acceptance, no About/logs test. |
| 13 | Source archive: requirements/lock, tests, tools, launcher source, CI, SBOM | — | **NOT RUN.** Repo has `tests/` and `tools/`, but the 0.2.1 release ZIP is not in this worktree and packaging/CI/SBOM cannot be verified. |
| 14 | Code signing + launcher manifest/version; Qt notices/LGPL offer | — | **NOT RUN.** No code-signing certificate, no launcher binary/source in this repo, no legal review. |
| 15 | try/finally in async callbacks; `closed` guard; exact flags in `edit_failed` | — | **NOT RUN.** Not exercised by phase-1 tests. |

### P1 (audit §9)

| # | Audit measure | Test id(s) | Baseline |
|---|---|---|---|
| 15 | Cell merge threshold/conditions; colour/clip/resource in `gap` | R10–R13 | **CONFIRMED/FAIL** for 5–6 pt cells; word-gap behaviour acceptable |
| 16 | Per-font try/except; CMap/FontBBox limits; partial ToUnicode merge | R14–R16 | **CONFIRMED/FAIL** |
| 17 | Cache reverse map/current font/bounds; lazy fontTools | — | **NOT RUN** (performance) |
| 18 | Preview: register DejaVu/local; colour/clip/bounds; glyph warning | U14; preview colour/clip not covered | **CONFIRMED/FAIL** for DejaVu; other preview aspects **NOT RUN** |
| 19 | Tile order by increasing level; cache stats/peek | U13 | **CONFIRMED/FAIL** |
| 20 | Per-page zoom/rotation, status after sheet/rotate, actual size | — | **NOT RUN** |
| 21 | Global memory budget, trim tabs, thumbnail retry/DPR | — | **NOT RUN** |
| 22 | Save-over-open-tab detection, friendly errors, last dir/title | U16; last-dir/title not covered | **CONFIRMED/FAIL** for save-over-open-tab; rest **NOT RUN** |
| 23 | Mode sync after `opened`, disabled states, QSS empty/primary/disabled | — | **NOT RUN** |
| 24 | Localization of tooltips/errors/dialogs; accessibility names | — | **NOT RUN** |
| 25 | `qoffscreen`/QtTest, headless CI; trim bundle/pycache | Partial: tests run offscreen in dev venv | **NOT RUN** for the portable ZIP (no `qoffscreen.dll` there) |
| 26 | Rich text plain; temp ACL/cleanup | — | **NOT RUN** (rich text); stale cleanup **NOT RUN** |
| 27 | Update UX: consent/progress/cancel/retention/cleanup/retry | — | **NOT RUN** |
| 28 | Docs: support matrix, known limitations, CHANGELOG, release notes | — | **NOT RUN** |
| 29 | Cache edit bytes/metrics/subsets; fallback when local font disappears; delete/undo | — | **NOT RUN** |
| 30 | Region marquee; Delete in preview; `samefile`/8.3; `open_file` worker; `close_tab`/`attach` guards | — | **NOT RUN** |
| 31 | Thumbnail LRU/DPR/retry/resume; global memory budget | — | **NOT RUN** |

---

## What cannot be verified in this phase

1. **Real Inventor drawing.** The audit and `TEST_RESULTS.md` refer to a private
   9-page drawing that is not in the archive. All tests here are synthetic.
   Real CAD fonts, Form XObjects, OCG layers, huge content streams and production
   table geometry remain **NEEDS USER FILE**.
2. **Physical Windows 11/HiDPI acceptance.** No 4K monitor, no 100/125/150/200%
   scaling station, no 1366×768 device. The offscreen platform tests logic only.
3. **Code signing, launcher manifest/version, Authenticode, SmartScreen.** No
   certificate and no `VectorPDF.exe` in this source worktree.
4. **Live GitHub/update network.** Updater tests use a fake release payload and
   monkeypatched transport; no live GitHub, no independent signature channel.
5. **Long-duration memory/FPS and native-parser fuzzing.** Not run.
6. **Qualified PDFs not claimed by the editor** (Td/TJ/Form/Type3/vertical text):
   read-only policy is not re-audited here.

---

## Notes on honest status

* A baseline **FAIL** is the intended outcome for audit defects; it is evidence
  that the regression test exercises the real defect, not evidence that the test
  is broken.
* A baseline **PASS** is recorded as **NOT REPRODUCED**, not as “0.3 fixed”.
* A **SKIP** is **NOT RUN**, never a pass. Skips exist only where the 0.2.1 API
  does not exist (signed manifest, recovery, journal, stale cleanup).
* Phase 2 must rerun these tests against the integrated 0.3 branch. No item may
  be marked PASS from code inspection; raster/pixel and QPDF/PDFium output
  evidence is required.
