"""Start VECTOR PDF, handing off to a verified update when one is active.

``dispatch`` must run before Qt is imported.  The updated process receives a
one-time ``--ready-token`` path and only becomes active after it confirms that
its window is up; otherwise the parent rolls back and starts the portable copy.
"""
import inspect
import os
import sys
import traceback


def _report_failure():
    try:
        from . import updater
        updater.log('Unhandled startup error: ' +
                    traceback.format_exc().replace('\n', ' | ')[:4000])
    except Exception:
        pass
    if sys.platform == 'win32' and sys.stderr is None:
        import ctypes
        try:
            from .updater import log_path
            where = f'\n\nLog: {log_path()}'
        except Exception:
            where = ''
        ctypes.windll.user32.MessageBoxW(
            None, (traceback.format_exc() + where)[:8000], 'VECTOR PDF', 0x10)
        raise SystemExit(1)
    raise


def _run():
    from . import updater
    original = list(sys.argv[1:])
    try:
        options = updater.parse_launch_arguments(original)
    except ValueError as error:
        updater.log(f'Ignoring invalid launch arguments: {error}')
        hidden = {'--safe-mode', '--reset-update'}
        paths = tuple(argument for argument in original
                      if argument not in hidden and
                      not argument.startswith('--ready-token'))
        options = updater.LaunchOptions(False, False, None, paths)
    if options.safe_mode:
        os.environ['VECTORPDF_SAFE_MODE'] = '1'
    # The UI must never see updater control switches as document names.
    sys.argv = [sys.argv[0], *options.paths]
    try:
        if updater.dispatch(argv=original):
            return 0
    except Exception as error:  # dispatch normally handles its own failures
        updater.log(f'Update dispatch failed unexpectedly: {error}')
    from .ui import main
    try:
        accepts_token = 'ready_token' in inspect.signature(main).parameters
    except (TypeError, ValueError):
        accepts_token = False
    if accepts_token:
        return main(ready_token=options.ready_token)
    # Compatibility with a UI that predates the readiness handshake.  The
    # production 0.3 UI accepts ready_token and reports readiness after the
    # window is shown; this fallback only keeps an older installed UI usable.
    if options.ready_token:
        updater.record_ready(options.ready_token)
    return main()


try:
    raise SystemExit(_run())
except SystemExit:
    raise
except Exception:
    _report_failure()
