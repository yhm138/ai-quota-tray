"""QuotaTray launcher.

A standalone script rather than `python quota_tray\\__main__.py`, because the
latter runs a package module as a top-level script and its relative imports
blow up. Here the project root goes on sys.path first, so the app starts no
matter what working directory the registry hands it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from quota_tray.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
