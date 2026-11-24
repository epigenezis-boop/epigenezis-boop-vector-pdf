# Результаты тестов VECTOR PDF 0.3.0

Дата: 24.11.2026. Платформа: Windows 11 10.0.22621 x64; CPython 3.12.10;
Qt/PySide6 6.10.3; pikepdf 10.18.2 / QPDF 12.4.2; pypdfium2 5.15.2 /
PDFium 159.0.8149.0; fontTools 4.68.0; rtree 1.4.1; numpy 2.5.5; pytest 9.2.0.

Исходный baseline: `47238fc`; рабочая ветка:
`deepseek/vector-pdf-0.3-hardening`.

## Итоговая регрессия

```powershell
$env:PYTHONPATH='.'
$env:QT_QPA_PLATFORM='offscreen'
$env:VECTORPDF_NO_RECOVERY='1'
.venv\Scripts\python.exe -m pytest -q
```

```
182 passed, 1 skipped, 3 warnings in 16.20s
```

Единственный skip — `test_inventor_acceptance`: он запускается только при
наличии реального чертежа в `VECTOR_TEST_PDF`/`VECTOR_TEST_STAMP`. Этот тест
был запущен отдельно на предоставленном чертеже — см. ниже.

Три warning — deprecated конструктор `QMouseEvent` в тесте Canvas, не ошибки
приложения.

## Реальный чертёж Inventor 2023

Файл: 9 листов, 18,0 МБ, Autodesk Inventor Professional 2023. Использована
временная копия; исходный файл не изменялся и в репозиторий не входит.

Запуск:

```powershell
# VECTOR_TEST_PDF = путь к чертежу
# VECTOR_TEST_STAMP = {"box":[...],"extract":[...],"old":"Размеры","new":"Тест"}
.venv\Scripts\python.exe -m pytest -q tests/test_editor.py::test_inventor_acceptance -s
```

Результат:

```
1 passed in 23.65s
```

Проверено:

- индекс всех 9 страниц; на первом листе 910 runs, 909 editable (один text
  render mode остаётся read-only);
- замена «Размеры» → «Тест» исходным встроенным GOSTCommonItalic;
- `QPDF check_pdf_syntax()` — пустой список;
- PDFium переоткрывает 9 страниц; новый текст извлекается, старый отсутствует;
- растровое сравнение всех 9 страниц при 0,4×: изменённые пиксели только в
  области правки; дополнительное сравнение области при 4×;
- исходный PDF побайтово не изменён; untouched pages имеют те же сырые потоки;
- `MediaBox`/`CropBox`/`Rotate` не изменились.

UI-прогон на том же файле (offscreen Qt):

| Операция | Результат |
| --- | ---: |
| Открытие и первый лист | 1866 мс |
| Apply (полный цикл) | 125 мс (metrics.edit 66 мс) |
| Undo | 94 мс |
| Redo | 118 мс |
| Save copy | 668 мс |
| QPDF full check (вне UI) | 8911 мс |

## Core / regression tests

- `tests/test_core_hardening.py` — 29 passed: subset glyphs, multi-Tj,
  direct/nested/empty signatures, clip/colour, CMap/FontBBox, partial ToUnicode,
  cell merge, long text/size, local font robustness, output validation.
- `tests/test_qa_regressions.py` — 19 passed: независимые воспроизведения
  аудита, включая растровую проверку глифов, наложений и clip; QPDF/PDFium
  output validity.
- `tests/test_editor.py` — 4 passed (без реального файла) + 1 passed с реальным
  файлом; legacy-тест `/Perms` обновлён на настоящую signature/empty-Perms
  семантику.
- `tests/test_qa_acceptance.py` — 28 passed: updater failure injection,
  offscreen UI busy/save/draft/recovery, real snapshot export, tile order,
  DejaVu preview, read-only byte copy, save-over-open-tab rejection.
- `tests/test_document_state.py`, `tests/test_ui_state.py` — snapshot,
  journal/recovery, busy guards, Save/Discard/Cancel, long text/size,
  per-page view, ready token.
- `tests/test_updater.py`, `tests/test_updater_hardening.py` — 76 passed:
  RFC 8032, canonical manifest, bad signature/hash, full file set, path safety,
  health check, dispatch timeout/rollback, safe mode/reset, retention, package
  manifest/determinism.

## Portable Windows x64

Собран `VECTOR-PDF-0.3.0-win-x64.zip` (63,8 МБ, 3141 файл в manifest).
Локально использован bootstrap `VectorPDF.exe` 0.2.1 с добавленными через
`rcedit` VERSIONINFO 0.3.0.0 и PE-манифестом; исходник нового лаунчера и CI
сборка находятся в `tools/` и `.github/`.

Проверено:

- `updater.check_installed(bundle, '0.3.0')` — PASS, все 3141 hash/size;
- `runtime\python.exe` с offscreen Qt импортирует `vector_pdf.ui` и создаёт
  `MainWindow`; `VERSION 0.3.0`;
- `VectorPDF.exe` запускает `pythonw.exe`, окно с заголовком `VECTOR PDF`;
- `Get-AuthenticodeSignature` — **NotSigned** (сертификата нет);
- реальный `VectorPDF.exe` 0.2.1 bootstrap не содержит нового поведения
  лаунчера; компиляция нового C-лаунчера **NOT RUN** (MSVC отсутствует).

## Updater failure injection

Покрыто unit-тестами: отсутствующие python312.zip/PySide6/ui/assets, лишние и
недостающие файлы, неверный размер/hash, tampered manifest/signature/archive,
пустой checksum, malformed JSON, partial/broken version, interrupted incoming,
retention, health-check failure, dead child, dispatch timeout/rollback,
ready-token success, safe mode/reset, package manifest/determinism.

Production Ed25519 public key не установлен: placeholder fail-closed. Живой
GitHub release, реальная 60 МБ загрузка и network failure injection на
инфраструктуре GitHub **NOT RUN**.

## Что не проверено

- Физическая Windows 11 100/125/150/200%, 1366×768, 1920×1080, 4K.
- Authenticode/SmartScreen, подпись EXE сертификатом проекта.
- Компиляция нового лаунчера MSVC и запуск CI workflow на GitHub runner.
- Живой GitHub update, независимый канал подписи.
- Длительный soak, антивирус/OneDrive, UNC/сетевые пути.
- Fuzzing PDFium/QPDF/fontTools и hostile PDF.
- Другие реальные CAD-файлы, кроме предоставленного Inventor 2023.

Эти пункты отмечены как **NOT RUN**, а не PASS. Подробная матрица —
`AUDIT_MATRIX.md`; независимый отчёт — `QA_REPORT.md`.
