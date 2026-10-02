# Development guide

[Home](../README.en.md) · [中文](development.md)

- [Development prerequisites](#development-prerequisites)
- [Repository layout](#repository-layout)
- [The C# edition](#the-c-edition)
- [The scripts](#the-scripts)
- [Tests and releases](#tests-and-releases)

## Development prerequisites

Run commands from the repository root unless a command changes directory.
The desktop app requires Windows; Linux and macOS can run the offline checks
and compile the C# application, but cannot validate its Windows tray or UI.

- Python 3.10+ (CI checks 3.10 and 3.12). Windows UI development also needs tcl/tk.
- .NET SDK 8.0.x for the cross-platform tests and .NET Framework 4.8 build.
- No provider credentials are needed for the offline tests.

For a Windows source install that starts the app and enables run-at-login, see
the [user guide](user-guide.en.md#option-2--from-source). For tests only:

```powershell
python -m venv python/.venv
python/.venv/Scripts/python.exe -m pip install -r python/requirements.txt cryptography
cd python
.venv/Scripts/python.exe tests/test_providers.py
.venv/Scripts/python.exe -m compileall -q quota_tray tools run.pyw
cd ..
```

On Linux or macOS, use `python3` and `python/.venv/bin/python`:

```bash
python3 -m venv python/.venv
python/.venv/bin/python -m pip install -r python/requirements.txt cryptography
(cd python && .venv/bin/python tests/test_providers.py)
(cd python && .venv/bin/python -m compileall -q quota_tray tools run.pyw)
```

`cryptography` is a test dependency used by CI. On Linux, the TLS tests also
need a writable `~/.quota-tray` directory for the copied CA bundle.

```bash
dotnet run --project csharp/tests/QuotaTray.Tests -c Release
dotnet build csharp/src/QuotaTray/QuotaTray.csproj -c Release -warnaserror:CS0104
```

The C# project restores the .NET Framework reference assemblies through NuGet,
so cross-compilation does not require a Windows runtime. Windows-only checks
and release packaging are defined in the [build workflow](../.github/workflows/build.yml).

## Repository layout

| Folder | What is in it |
|---|---|
| `python/` | The Python edition: `quota_tray/` (the app), `tests/`, `tools/`, `run.pyw` and its `.bat` scripts |
| `csharp/` | The C# edition: `src/QuotaTray/` (the app) and `tests/QuotaTray.Tests/` |
| `scripts/` | Maintainer scripts: publishing, releases, the workflow generator |
| `update.ps1` | The stand-alone updater (stays at the root: older versions download it from there) |
| `docs/` | Bilingual guides and panel screenshots |

## The C# edition

**A single 250 KB `.exe` you double-click to run.** The same app written in
C# for .NET Framework 4.8, which ships with Windows 10 (1903+) and 11, so
there is nothing to install and nothing to unpack: it starts instantly and
uses far less memory than the ~15 MB Python build.

It covers the same data: Claude (Claude Code logins, including WSL, and
Claude Desktop's own login), Codex (the live usage API with plan, credits and
resets, `codex app-server`, the session logs) and Antigravity, with the same
multi-account pages, reset reminder, one-click in-app update, diagnostics and
run-at-login. Not included: the claude.ai cookie fallbacks and the Codex SQLite
scan, which the Python edition keeps as last resorts.

Build it yourself with the .NET SDK (any OS can compile it):

```powershell
dotnet build csharp\src\QuotaTray\QuotaTray.csproj -c Release
dotnet run --project csharp\tests\QuotaTray.Tests     # offline checks
```

`QuotaTray.exe --diagnose` writes the source report, `--selftest` checks
AES-GCM, the tray icon and the panel on this machine.

On Windows, test scroll clipping with synthetic cards at 100%, 125% and 150%
scale. This renders offscreen without reading account settings:

```powershell
powershell.exe -NoProfile -STA -File csharp\tests\Run-PanelScrollRegression.ps1 -AssemblyPath csharp\src\QuotaTray\bin\Release\net48\QuotaTray.exe
```

## The scripts

Python edition scripts live in `python/`, maintainer scripts in `scripts/`.

| File | What it does |
|---|---|
| `install.bat` | Create the venv, install deps, start, enable run-at-login |
| `run.bat` | Start it manually |
| `diagnose.bat` | **Start here when something is wrong** — prints every source probe |
| `probe.bat` | Writes `_probe.txt` with proxy config, raw API payloads and discovery output (secrets redacted) |
| `preview.bat` | Draw the panel with fake data to check the UI renders |
| `claude_login.bat` | Start Claude Code with the proxy port forced to a known-good value |
| `build_exe.bat` | Build `dist\QuotaTray.exe` locally |
| `scripts\publish.bat` | Create the GitHub repository and push (one-time) |
| `scripts\release.bat` | Tag a version, which builds and publishes a release |
| `scripts\fix_push.bat` | Retry a failed push with the full error shown |
| `scripts\find_gh.bat` | Locate git/gh when a stale PATH hides them |
| `uninstall.bat` | Remove run-at-login, stop the process, optionally delete config |

## Tests and releases

The suite drives all three fallback chains with fabricated payloads, covering
HTTP 401 degradation, the percent conventions, Claude Desktop's encrypted login
cache, the Electron cookie store, plan / credits / reset parsing, the reset
reminder, cross-source merging, the IDE-not-running path, cache round-trips and
countdown formatting.

Run the checks in [Development prerequisites](#development-prerequisites) first.
From the repository root, to cut a release, bump `__version__` in `python/quota_tray/__init__.py` and
`<Version>` in `csharp/src/QuotaTray/QuotaTray.csproj` (the build checks both), then either
open **Actions → build → Run workflow** and enter the matching version (for
example `v1.2.1`), which creates the tag and the release, or push a tag:

```powershell
.\scripts\release.bat             # or: git tag v1.2.1 && git push origin v1.2.1
```

Either way the [build workflow](../.github/workflows/build.yml) attaches both
editions' executables, the portable zip and the SHA256 sums to the release.
