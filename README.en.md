<h1 align="center">QuotaTray</h1>

<p align="center">
  Every AI coding quota you pay for, one click away in the Windows 11 tray:<br>
  <b>Claude</b> · <b>Codex</b> · <b>Antigravity</b> · <b>Gemini CLI</b> · <b>TRAE</b> · <b>Doubao</b> · <b>DeepSeek</b><br>
</p>

<p align="center">
  <a href="https://github.com/yhm138/ai-quota-tray/actions/workflows/build.yml"><img alt="build" src="https://github.com/yhm138/ai-quota-tray/actions/workflows/build.yml/badge.svg"></a>
  <a href="https://github.com/yhm138/ai-quota-tray/actions/workflows/test.yml"><img alt="tests" src="https://github.com/yhm138/ai-quota-tray/actions/workflows/test.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="license" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <a href="https://github.com/yhm138/ai-quota-tray/releases/latest"><img alt="release" src="https://img.shields.io/github/v/release/yhm138/ai-quota-tray?include_prereleases"></a>
</p>

<div align="center">
<table>
  <tr>
    <th>Subscriptions</th>
    <th>Pay as you go</th>
  </tr>
  <tr>
    <td align="center" valign="top">
      <img src="docs/panel-subscriptions.png" alt="Subscriptions tab: Claude, Codex, Antigravity and Doubao cards with usage bars, reset times and plan details" width="320"><br>
      <sub>Usage windows, reset times, plan and account details</sub>
    </td>
    <td align="center" valign="top">
      <img src="docs/panel-payg.png" alt="Pay-as-you-go tab: the DeepSeek API balance with a Copy button for the key" width="320"><br>
      <sub>DeepSeek API balance; the key is one click from the clipboard</sub>
    </td>
  </tr>
</table>
</div>


**English** · [中文](README.md)

## Features

- View Claude, Codex, Antigravity, TRAE and Doubao usage, plus DeepSeek API balances, in one panel.
- Track quota windows, reset countdowns, plans, credits and multiple accounts, with reminders for unused one-time resets.
- Reuse credentials from tools already signed in on your machine; no separate QuotaTray sign-in.
- Start with Windows, refresh from the tray menu and update in one click. The Gemini CLI card is off by default.

## Quick start

1. Download **`QuotaTray-<version>-csharp-windows-anycpu.exe`** from the [latest release](https://github.com/yhm138/ai-quota-tray/releases/latest). The C# edition is about 250 KB and uses .NET Framework 4.8 included with Windows 10 (1903+) / 11, supporting x64 and ARM64.
2. Put it in a permanent folder, such as `C:\Tools\QuotaTray\`, then double-click it. The first launch records this path for run-at-login.
3. Click the notification-area icon to open the panel; check the taskbar's `^` menu if it is hidden. Sign in to the corresponding tools first, and keep Antigravity open to read its quota.

The Python edition comes as a single-file exe or portable zip and retains extra Claude cookie and Codex SQLite fallbacks. Both editions share settings in `%APPDATA%\QuotaTray`; only one instance runs at a time.

Release binaries are unsigned and may be flagged by antivirus software. Compare downloads with the SHA256 file from the same release. See the [user guide](docs/user-guide.en.md#install) for download choices, verification and source installation.

## Documentation

| Guide | Contents |
|---|---|
| [User guide](docs/user-guide.en.md) | Installation, plans and resets, updates, configuration, troubleshooting, privacy and limitations |
| [Data sources](docs/providers.en.md) | Credential locations, quota endpoints, fallback order and provider-specific settings |
| [Development guide](docs/development.en.md) | Prerequisites, repository layout, edition differences, scripts, tests and releases |
| [Changelog](CHANGELOG.md) | Changes by version |

## License

MIT — see [LICENSE](LICENSE).
