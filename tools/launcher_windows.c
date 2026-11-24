/* Minimal GUI bootstrap for the portable CPython runtime.
 *
 * It contains no update logic: it locates <application>\runtime\pythonw.exe,
 * verifies that the file exists, and starts `-m vector_pdf` with the original
 * command-line tail.  The original arguments are already a valid command-line
 * fragment, so they are appended verbatim; the executable path is passed as a
 * separate ShellExecuteW parameter and needs no manual quoting.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>

#define CAPACITY 32768
#define APP_ID L"VectorPDF.Desktop"

__declspec(dllimport) HRESULT __stdcall
SetCurrentProcessExplicitAppUserModelID(const wchar_t *application_id);

/* /GS support for a CRT-free image.  The cookie is process-wide and is only a
 * compiler-generated stack-canary guard; there are no local buffers here, but
 * keeping the definitions makes the /GS build self-contained. */
unsigned __int64 __security_cookie = 0x00002B992DDFA232ULL;

void __cdecl __security_check_cookie(unsigned __int64 cookie)
{
    if (cookie != __security_cookie)
        ExitProcess(3);
}

static wchar_t application[CAPACITY];
static wchar_t runtime[CAPACITY];
static wchar_t command[CAPACITY];
static wchar_t message[CAPACITY];

static void fail(const wchar_t *text)
{
    MessageBoxW(NULL, text, L"VECTOR PDF", MB_OK | MB_ICONERROR);
    ExitProcess(1);
}

static void fail_with_path(const wchar_t *prefix, const wchar_t *path)
{
    lstrcpynW(message, prefix, CAPACITY);
    int used = lstrlenW(message);
    int room = CAPACITY - used - 160;
    if (room > 1)
        lstrcpynW(message + used, path, room);
    lstrcatW(message,
             L"\n\nThe portable package is incomplete. Extract the complete "
             L"ZIP into a writable folder and try again.");
    fail(message);
}

void start(void)
{
    DWORD length = GetModuleFileNameW(NULL, application, CAPACITY);
    if (length == 0 || length >= CAPACITY - 32)
        fail(L"VECTOR PDF could not determine its installation folder.");

    DWORD slash = length;
    while (slash > 0 && application[slash - 1] != L'\\')
        --slash;
    if (slash == 0)
        fail(L"VECTOR PDF could not determine its installation folder.");
    application[slash] = 0;

    lstrcpynW(runtime, application, CAPACITY);
    lstrcatW(runtime, L"runtime\\pythonw.exe");
    DWORD attributes = GetFileAttributesW(runtime);
    if (attributes == INVALID_FILE_ATTRIBUTES ||
        (attributes & FILE_ATTRIBUTE_DIRECTORY))
        fail_with_path(L"VECTOR PDF could not find its Python runtime:\n\n",
                       runtime);

    const wchar_t *tail = GetCommandLineW();
    if (*tail == L'"') {
        ++tail;
        while (*tail && *tail != L'"')
            ++tail;
        if (*tail)
            ++tail;
    } else {
        while (*tail && *tail != L' ' && *tail != L'\t')
            ++tail;
    }
    const int tail_length = lstrlenW(tail);
    /* "-m vector_pdf" is 13 characters; the Windows limit is 32767 including
     * the terminator. */
    if (13 + tail_length >= 32767)
        fail(L"The command line is too long. Shorten the PDF path and try "
             L"again.");
    if (13 + tail_length + 1 > CAPACITY)
        fail(L"The command line is too long.");

    lstrcpyW(command, L"-m vector_pdf");
    lstrcatW(command, tail);

    SetCurrentProcessExplicitAppUserModelID(APP_ID);
    HINSTANCE result = ShellExecuteW(NULL, L"open", runtime, command, NULL,
                                     SW_SHOWNORMAL);
    if ((INT_PTR)result <= 32) {
        wsprintfW(message,
                  L"VECTOR PDF could not start its Python runtime.\n\n"
                  L"Windows error %d. Extract the complete ZIP and try again.",
                  (int)(INT_PTR)result);
        fail(message);
    }
    ExitProcess(0);
}
