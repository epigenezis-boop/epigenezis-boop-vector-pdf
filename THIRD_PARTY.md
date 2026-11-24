# Сторонние компоненты

Собственный код VECTOR PDF распространяется по MIT. Переносимый архив содержит штатные двоичные библиотеки и файлы лицензий. Динамически загружаемые библиотеки Qt лежат в `runtime/Lib/site-packages/PySide6`. Пользователь может заменить их совместимыми сборками. Условия GNU LGPL v3 допускают отладку изменений этих библиотек.

| Компонент | Лицензия и исходники |
| --- | --- |
| Qt Base 6.10.3, Qt for Python (PySide6 Essentials и Shiboken 6.10.3) | LGPL v3. В комплекте тексты GPL/LGPL. [Qt Base](https://github.com/qt/qtbase/tree/v6.10.3) · [Qt for Python](https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v6.10.3) |
| CPython 3.12.10 | PSF License и лицензии вложенных компонентов в комплекте интерпретатора. [Исходники CPython](https://www.python.org/downloads/release/python-31210/) |
| pypdfium2 5.15.2 и PDFium | Apache-2.0 либо BSD-3-Clause для обёртки, BSD-3-Clause и лицензии компонентов PDFium. Уведомления в `pypdfium2-*.dist-info/licenses`. [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) · [PDFium](https://pdfium.googlesource.com/pdfium/) |
| pikepdf 10.18.2 и QPDF | MPL-2.0 и Apache-2.0 соответственно. [pikepdf](https://github.com/pikepdf/pikepdf) · [QPDF](https://github.com/qpdf/qpdf) |
| fontTools 4.68.0, DejaVu | MIT для fontTools, Bitstream Vera и условия DejaVu для шрифтов. Текст лицензии шрифтов в `vector_pdf/assets`. [fontTools](https://github.com/fonttools/fonttools) · [DejaVu](https://dejavu-fonts.github.io/License.html) |
| Rtree 1.4.1, libspatialindex | MIT, уведомления в комплекте. [Rtree](https://github.com/Toblerity/rtree) · [libspatialindex](https://github.com/libspatialindex/libspatialindex) |
| psutil, Pillow, lxml, packaging | BSD-3-Clause, MIT-CMU, BSD-3-Clause, Apache-2.0/BSD-2-Clause соответственно. Лицензии в метаданных колёс. |

Microsoft Visual C++ Runtime поставляется в колёсах Qt и в штатном интерпретаторе Python в формах, выпущенных их поставщиками. [Microsoft Visual C++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)


В интерфейсе использованы Russo One и Exo 2 из выпуска Orbita. Оба распространяются по SIL Open Font License 1.1; тексты лицензий находятся рядом с файлами в `vector_pdf/assets`. [Orbita](https://github.com/epigenezis-boop/orbita-releases)

Штатный технический шрифт osifont распространяется по LGPL-3.0 с исключением для встраивания шрифтов. Его лицензия в `vector_pdf/assets/osifont-LICENSE.txt`. Импортированные пользователем шрифты хранятся в профиле и в выпуск не входят. [Исходники osifont](https://github.com/hikikomori82/osifont)
