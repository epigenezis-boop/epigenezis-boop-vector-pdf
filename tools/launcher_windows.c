/* Location-independent GUI launcher for the embeddable CPython runtime. */
typedef unsigned short wchar_t;

__declspec(dllimport) unsigned long __stdcall GetModuleFileNameW(void *, wchar_t *, unsigned long);
__declspec(dllimport) wchar_t * __stdcall GetCommandLineW(void);
__declspec(dllimport) int __stdcall lstrlenW(const wchar_t *);
__declspec(dllimport) wchar_t * __stdcall lstrcpyW(wchar_t *, const wchar_t *);
__declspec(dllimport) wchar_t * __stdcall lstrcatW(wchar_t *, const wchar_t *);
__declspec(dllimport) void __stdcall ExitProcess(unsigned int);
__declspec(dllimport) int __stdcall MessageBoxW(void *, const wchar_t *, const wchar_t *, unsigned int);
__declspec(dllimport) void * __stdcall ShellExecuteW(void *, const wchar_t *, const wchar_t *,
                                                    const wchar_t *, const wchar_t *, int);

#define CAPACITY 32768
static wchar_t executable[CAPACITY];
static wchar_t command[CAPACITY];

static void fail(const wchar_t *message)
{
    MessageBoxW(0, message, L"VECTOR PDF", 0x10);
    ExitProcess(1);
}

void start(void)
{
    const unsigned long length = GetModuleFileNameW(0, executable, CAPACITY);
    if (!length || length >= CAPACITY - 32)
        fail(L"Unable to locate the portable runtime.");

    unsigned long slash = length;
    while (slash > 0 && executable[slash - 1] != L'\\')
        --slash;
    executable[slash] = 0;
    lstrcatW(executable, L"runtime\\pythonw.exe");

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
    if (lstrlenW(tail) + 32 >= CAPACITY)
        fail(L"The file path is too long.");

    lstrcpyW(command, L"-m vector_pdf");
    lstrcatW(command, tail);
    if ((__int64)ShellExecuteW(0, L"open", executable, command, 0, 1) <= 32)
        fail(L"Unable to start VECTOR PDF. Extract the complete ZIP before launching it.");
    ExitProcess(0);
}
