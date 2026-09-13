"""Generate assets/quotatray.ico for the PyInstaller build."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quota_tray.ui.icon import render  # noqa: E402

out = Path(__file__).resolve().parent.parent / "assets" / "quotatray.ico"
out.parent.mkdir(parents=True, exist_ok=True)
img = render([("claude", 45.0), ("codex", 72.0), ("antigravity", 20.0)])
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128)])
print("wrote", out)
