import sys
import traceback

try:
    from .updater import dispatch
    if dispatch():
        raise SystemExit(0)
    from .ui import main
    raise SystemExit(main())
except Exception:
    if sys.platform == 'win32' and sys.stderr is None:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, traceback.format_exc(), 'VECTOR PDF', 0x10)
        raise SystemExit(1)
    raise
