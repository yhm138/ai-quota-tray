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

# Publish a release either way:
#   - push a tag:          git tag v1.0.0 && git push origin v1.0.0
#   - or, without git:     Actions > build > Run workflow, version = v1.0.0
#     (the tag is created on the chosen branch's head commit)
on:
  push:
    tags: ["v*"]
  workflow_dispatch:
    inputs:
      version:
        description: "Release tag to create, e.g. v1.1.0 (empty = build only)"
        required: false
        default: ""

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

      - name: Check the version matches the code
        if: startsWith(inputs.version, 'v')
        env:
          WANT: ${{ inputs.version }}
        run: |
          $have = "v" + (python -c "import quota_tray; print(quota_tray.__version__)")
          if ($have -ne $env:WANT) { throw "asked to release $env:WANT but the code says $have" }

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          python -m pip install -r requirements.txt "pyinstaller>=6.11"

      - name: Generate the application icon
        run: python tools/make_icon.py

      - name: Build the single-file executable
        run: >
          pyinstaller --noconfirm --clean --onefile --noconsole --optimize 2
          --name QuotaTray
          --icon assets/quotatray.ico
          --hidden-import pystray._win32
          --exclude-module cryptography
          --exclude-module cffi
          --exclude-module curl_cffi
          --exclude-module PIL._avif
          --exclude-module PIL.AvifImagePlugin
          --exclude-module PIL._webp
          --exclude-module PIL.WebPImagePlugin
          --exclude-module PIL._imagingft
          --exclude-module PIL._imagingcms
          --exclude-module PIL.ImageCms
          --exclude-module PIL._imagingmath
          --exclude-module PIL._imagingmorph
          --exclude-module PIL.ImageTk
          --exclude-module PIL._tkinter_finder
          --exclude-module PIL.ImageQt
          --exclude-module setuptools
          --exclude-module pkg_resources
          --exclude-module unittest
          --exclude-module pydoc
          --exclude-module doctest
          --exclude-module pdb
          --exclude-module lib2to3
          --exclude-module xmlrpc
          --exclude-module tkinter.test
          --exclude-module test
          --distpath dist/onefile
          run.pyw

      - name: Build the folder distribution
        run: >
          pyinstaller --noconfirm --clean --onedir --noconsole --optimize 2
          --name QuotaTray
          --icon assets/quotatray.ico
          --hidden-import pystray._win32
          --exclude-module cryptography
          --exclude-module cffi
          --exclude-module curl_cffi
          --exclude-module PIL._avif
          --exclude-module PIL.AvifImagePlugin
          --exclude-module PIL._webp
          --exclude-module PIL.WebPImagePlugin
          --exclude-module PIL._imagingft
          --exclude-module PIL._imagingcms
          --exclude-module PIL.ImageCms
          --exclude-module PIL._imagingmath
          --exclude-module PIL._imagingmorph
          --exclude-module PIL.ImageTk
          --exclude-module PIL._tkinter_finder
          --exclude-module PIL.ImageQt
          --exclude-module setuptools
          --exclude-module pkg_resources
          --exclude-module unittest
          --exclude-module pydoc
          --exclude-module doctest
          --exclude-module pdb
          --exclude-module lib2to3
          --exclude-module xmlrpc
          --exclude-module tkinter.test
          --exclude-module test
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
          function Invoke-Checked($exe, $arg) {
            # A windowed exe that hits an error dialog would wait forever.
            $p = Start-Process -FilePath $exe -ArgumentList $arg -PassThru
            if (-not $p.WaitForExit(120000)) {
              Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
              Get-Content "$env:APPDATA\QuotaTray\crash.log" -ErrorAction SilentlyContinue
              throw "$exe $arg did not finish within 2 minutes"
            }
            if ($p.ExitCode -ne 0) {
              Get-Content "$env:APPDATA\QuotaTray\crash.log" -ErrorAction SilentlyContinue
              Get-Content "$env:APPDATA\QuotaTray\diagnostics.txt" -ErrorAction SilentlyContinue
              throw "$exe $arg exited with $($p.ExitCode)"
            }
          }
          foreach ($exe in $targets) {
            Invoke-Checked $exe "--diagnose"
            # Proves what the trimmed build keeps: AES-GCM via Windows CNG,
            # the tray icon as ICO, HTTPS certificates, Tk and SQLite.
            Invoke-Checked $exe "--selftest"
            Write-Host "$exe ran cleanly"
          }

      - name: Package
        env:
          REF: ${{ github.ref }}
          REF_NAME: ${{ github.ref_name }}
          INPUT_VERSION: ${{ inputs.version }}
          SHA: ${{ github.sha }}
        # Assets carry the version and architecture, e.g.
        # QuotaTray-v1.3.2-windows-x64.exe, so downloads can be told apart.
        run: |
          if ($env:REF -like "refs/tags/v*") { $ver = $env:REF_NAME }
          elseif ($env:INPUT_VERSION) { $ver = $env:INPUT_VERSION }
          else { $ver = "dev-" + $env:SHA.Substring(0, 7) }
          $machine = python -c "import platform; print(platform.machine())"
          $arch = @{ "AMD64" = "x64"; "x86_64" = "x64"; "ARM64" = "arm64"; "x86" = "x86" }[$machine]
          if (-not $arch) { $arch = $machine.ToLower() }
          $base = "QuotaTray-$ver-windows-$arch"
          New-Item -ItemType Directory -Force -Path release | Out-Null
          Copy-Item dist\onefile\QuotaTray.exe "release\$base.exe"
          Compress-Archive -Path dist\onedir\QuotaTray\* -DestinationPath "release\$base-portable.zip"
          $sums = "release\QuotaTray-$ver-SHA256SUMS.txt"
          Get-ChildItem release -File | Where-Object { $_.Extension -ne ".txt" } | ForEach-Object {
            "$((Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower())  $($_.Name)"
          } | Out-File -Encoding ascii $sums
          Get-ChildItem release | Format-Table Name, Length
          Get-Content $sums

      - uses: actions/upload-artifact@v4
        with:
          name: QuotaTray-windows
          path: release/

      - name: Publish the release
        if: startsWith(github.ref, 'refs/tags/v') || startsWith(inputs.version, 'v')
        env:
          GH_TOKEN: ${{ github.token }}
          TAG: ${{ startsWith(github.ref, 'refs/tags/v') && github.ref_name || inputs.version }}
        run: |
          gh release create "$env:TAG" (Get-ChildItem release -File).FullName `
            --target "${{ github.sha }}" `
            --title "QuotaTray $env:TAG" `
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
