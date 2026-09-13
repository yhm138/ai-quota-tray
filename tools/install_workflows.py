"""Write .github/workflows/*.yml.

The workflow files are generated rather than shipped directly because remote
file tools refuse to write into .github/workflows (they can execute code in CI).
publish.bat runs this before the first commit; running it again is harmless and
only rewrites a file whose contents actually differ.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"

BUILD_YML = r"""name: build

# Tag a commit to publish a release:  git tag v1.0.0 && git push origin v1.0.0
on:
  push:
    tags: ["v*"]
  workflow_dispatch:

permissions:
  contents: write

jobs:
  windows:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          python -m pip install -r requirements.txt pyinstaller

      - name: Generate the application icon
        run: python tools/make_icon.py

      - name: Build the single-file executable
        run: >
          pyinstaller --noconfirm --clean --onefile --noconsole
          --name QuotaTray
          --icon assets/quotatray.ico
          --hidden-import pystray._win32
          --hidden-import PIL._tkinter_finder
          --distpath dist/onefile
          run.pyw

      - name: Build the folder distribution
        run: >
          pyinstaller --noconfirm --clean --onedir --noconsole
          --name QuotaTray
          --icon assets/quotatray.ico
          --hidden-import pystray._win32
          --hidden-import PIL._tkinter_finder
          --distpath dist/onedir
          run.pyw

      - name: Smoke-test both builds
        env:
          QUOTATRAY_NO_OPEN: "1"
        # --diagnose exits 0 even with nothing configured, so a non-zero exit
        # means the bundle itself is broken (a missing hidden import, say).
        # These are windowed (--noconsole) binaries, so PowerShell will not wait
        # for them on a plain call and $LASTEXITCODE would be meaningless.
        run: |
          $targets = @(
            "dist\onefile\QuotaTray.exe",
            "dist\onedir\QuotaTray\QuotaTray.exe"
          )
          foreach ($exe in $targets) {
            $p = Start-Process -FilePath $exe -ArgumentList "--diagnose" -Wait -PassThru
            if ($p.ExitCode -ne 0) { throw "$exe exited with $($p.ExitCode)" }
            Write-Host "$exe ran cleanly"
          }

      - name: Package
        run: |
          New-Item -ItemType Directory -Force -Path release | Out-Null
          Copy-Item dist\onefile\QuotaTray.exe release\QuotaTray.exe
          Compress-Archive -Path dist\onedir\QuotaTray\* -DestinationPath release\QuotaTray-portable.zip
          Get-FileHash release\QuotaTray.exe -Algorithm SHA256 |
            ForEach-Object { "$($_.Hash.ToLower())  QuotaTray.exe" } |
            Out-File -Encoding ascii release\SHA256SUMS.txt
          Get-FileHash release\QuotaTray-portable.zip -Algorithm SHA256 |
            ForEach-Object { "$($_.Hash.ToLower())  QuotaTray-portable.zip" } |
            Out-File -Encoding ascii -Append release\SHA256SUMS.txt
          Get-Content release\SHA256SUMS.txt

      - uses: actions/upload-artifact@v4
        with:
          name: QuotaTray-windows
          path: release/

      - name: Publish the release
        if: startsWith(github.ref, 'refs/tags/v')
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          gh release create "${{ github.ref_name }}" `
            release\QuotaTray.exe `
            release\QuotaTray-portable.zip `
            release\SHA256SUMS.txt `
            --title "QuotaTray ${{ github.ref_name }}" `
            --generate-notes
"""

TEST_YML = r"""name: tests

on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.10", "3.12"]
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}

      # pystray is a Windows-runtime dependency; the test suite does not import it.
      - name: Install test dependencies
        run: python -m pip install --quiet Pillow requests cryptography

      - name: Run the offline test suite
        run: python tests/test_providers.py

      - name: Byte-compile every module
        run: python -m compileall -q quota_tray tools run.pyw

      - name: Check the shipped code stays pure ASCII
        # Non-ASCII in .bat files shows up as mojibake in cmd.exe, so the source
        # and scripts are kept ASCII-only. Docs are exempt (README is bilingual).
        run: |
          set -e
          bad=0
          for f in $(git ls-files '*.py' '*.pyw' '*.bat' '*.yml'); do
            if LC_ALL=C grep -qP '[^\x00-\x7F]' "$f"; then
              echo "non-ASCII bytes in $f"
              bad=1
            fi
          done
          exit $bad
"""

FILES = {
    "build.yml": BUILD_YML,
    "test.yml": TEST_YML,
}


def main() -> int:
    WORKFLOWS.mkdir(parents=True, exist_ok=True)
    for name, content in FILES.items():
        path = WORKFLOWS / name
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current == content:
            print(f"unchanged: {path.relative_to(ROOT)}")
            continue
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        print(f"{'updated' if current else 'created'}: {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
