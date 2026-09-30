"""QuotaTray launcher.

A standalone script rather than `python quota_tray\\__main__.py`, because the
latter runs a package module as a top-level script and its relative imports
blow up. Here the project root goes on sys.path first, so the app starts no
matter what working directory the registry hands it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from quota_tray.app import main  # noqa: E402
except BaseException:  # noqa: BLE001
    # A broken build (a module left out, say) fails here, before the app's own
    # crash handler exists. Write crash.log; show a box unless unattended.
    import traceback

    report = traceback.format_exc()
    folder = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "QuotaTray")
    try:
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "crash.log"), "a", encoding="utf-8") as fh:
            fh.write("--- import failure\n" + report + "\n")
    except OSError:
        pass
    if sys.platform == "win32" and not os.environ.get("QUOTATRAY_NO_OPEN"):
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, "QuotaTray could not start:\n\n"
                                         + report.strip().splitlines()[-1], "QuotaTray", 0x10)
    sys.exit(3)

if __name__ == "__main__":
    sys.exit(main())
