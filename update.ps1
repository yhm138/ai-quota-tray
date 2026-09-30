# QuotaTray updater.
#
# Updates an existing QuotaTray install in place to the latest GitHub release,
# whichever way it was installed (QuotaTray.exe, the portable folder, or a
# source checkout), then starts it again. Run-at-login keeps working because
# the program stays where it is.
#
# Easiest way to run it (PowerShell, no admin needed):
#   irm https://raw.githubusercontent.com/yhm138/ai-quota-tray/main/update.ps1 | iex
#
# The tray menu's "Update to ..." item runs this same script with -WaitPid.
# Written for Windows PowerShell 5.1, which every Windows 10/11 machine has.

param(
    [string]$Repo = "yhm138/ai-quota-tray",
    [string]$Tag = "",           # empty = latest release
    [string]$InstallDir = "",    # empty = find it from run-at-login / running process
    [int]$WaitPid = 0,           # the tray app passes its own PID and exits
    [string]$ReadyFile = "",     # written once the download is verified; the tray app waits for it
    [string]$ExeName = "",       # file name of the installed exe (kept as is on update)
    [switch]$NoRestart,
    [switch]$DryRun              # resolve and download, change nothing
)

$script:LogDir = if ($env:APPDATA) { Join-Path $env:APPDATA "QuotaTray" } else { Join-Path $HOME ".quota-tray" }
New-Item -ItemType Directory -Force -Path $script:LogDir | Out-Null
$script:LogFile = Join-Path $script:LogDir "update.log"
$script:Install = $null      # @{ dir; kind } once found, so any failure can still restart
$script:Stopped = $false

function Log([string]$msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Host "  $msg"
    try { Add-Content -Path $script:LogFile -Value $line -Encoding UTF8 } catch { }
}

function Start-QuotaTray {
    # Bring the app back whenever it is gone because of us, whatever failed.
    $info = $script:Install
    if (-not $info) { return }
    if ($WaitPid -gt 0 -and (Get-Process -Id $WaitPid -ErrorAction SilentlyContinue)) {
        Log "the tray app is still running; nothing to restart"
        return
    }
    if ($WaitPid -le 0 -and -not $script:Stopped) { return }
    $dir = $info.dir
    if ($info.kind -eq "source") {
        $pyw = Join-Path $dir ".venv\Scripts\pythonw.exe"
        if (-not (Test-Path -LiteralPath $pyw)) { $pyw = "pythonw.exe" }
        Start-Process -FilePath $pyw -ArgumentList ('"' + (Join-Path $dir "run.pyw") + '"') -WorkingDirectory $dir
    } else {
        $exe = Join-Path $dir $info.exe
        if (-not (Test-Path -LiteralPath $exe) -and (Test-Path -LiteralPath "$exe.old")) {
            Move-Item -LiteralPath "$exe.old" -Destination $exe -Force
            Log "restored the previous QuotaTray.exe"
        }
        Start-Process -FilePath $exe -WorkingDirectory $dir
    }
    Log "QuotaTray restarted"
}

function Invoke-QuotaTrayUpdate {
    $ErrorActionPreference = "Stop"
    $ProgressPreference = "SilentlyContinue"   # 5.1's progress bar makes downloads crawl
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    } catch { }

    Log "QuotaTray updater starting (repo $Repo)"

    # ------------------------------------------------------------ find the install
    $kind = $null
    $dir = $InstallDir
    $runCmd = $null
    try {
        $runCmd = (Get-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name QuotaTray -ErrorAction Stop).QuotaTray
    } catch { }

    if (-not $dir -and $runCmd) {
        $quoted = [regex]::Matches($runCmd, '"([^"]+)"') | ForEach-Object { $_.Groups[1].Value }
        foreach ($q in $quoted) {
            if ($q -like "*.pyw") { $dir = Split-Path -Parent $q; break }
            if ((Split-Path -Leaf $q) -like "QuotaTray*.exe") { $dir = Split-Path -Parent $q; break }
        }
        if ($dir) { Log "found install via run-at-login: $dir" }
    }
    if (-not $dir) {
        try {
            $procs = Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
                $_.Name -like "QuotaTray*.exe" -or ($_.CommandLine -and $_.CommandLine -like "*run.pyw*")
            }
            foreach ($p in $procs) {
                if ($p.Name -like "QuotaTray*.exe" -and $p.ExecutablePath) {
                    $dir = Split-Path -Parent $p.ExecutablePath
                    if (-not $ExeName) { $ExeName = $p.Name }
                    break
                }
                $m = [regex]::Match($p.CommandLine, '"([^"]*run\.pyw)"')
                if ($m.Success) { $dir = Split-Path -Parent $m.Groups[1].Value; break }
            }
        } catch { }
        if ($dir) { Log "found install via running process: $dir" }
    }
    if (-not $dir) {
        throw "Could not find QuotaTray. Run this again with -InstallDir <folder that holds QuotaTray.exe or run.pyw>."
    }
    $dir = (Resolve-Path -LiteralPath $dir).Path

    # The exe may be called QuotaTray.exe or keep its download name
    # (QuotaTray-v1.3.2-windows-x64.exe); an update keeps whatever it is.
    if (-not $ExeName -or -not (Test-Path -LiteralPath (Join-Path $dir $ExeName))) {
        $found = Get-ChildItem -LiteralPath $dir -Filter "QuotaTray*.exe" -File -ErrorAction SilentlyContinue |
            Sort-Object @{ Expression = { $_.Name -ne "QuotaTray.exe" }; Ascending = $true },
                        @{ Expression = "LastWriteTime"; Descending = $true } | Select-Object -First 1
        $ExeName = if ($found) { $found.Name } else { "QuotaTray.exe" }
    }
    if (Test-Path -LiteralPath (Join-Path $dir "run.pyw")) {
        $kind = "source"
    } elseif (Test-Path -LiteralPath (Join-Path $dir $ExeName)) {
        if (Test-Path -LiteralPath (Join-Path $dir "_internal")) { $kind = "portable" } else { $kind = "exe" }
    } else {
        throw "$dir holds neither a QuotaTray*.exe nor run.pyw."
    }
    Log "install type: $kind ($ExeName)"
    $script:Install = @{ dir = $dir; kind = $kind; exe = $ExeName }

    # ------------------------------------------------------------ pick the release
    # With a tag (always, when the tray app runs this) nothing but plain
    # github.com downloads is needed. The API is only asked for "latest", and
    # the release page's redirect stands in when the API refuses (it allows
    # 60 anonymous calls an hour) or is blocked.
    $headers = @{ "User-Agent" = "QuotaTray-updater" }
    $newTag = $Tag
    if (-not $newTag) {
        try {
            $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -UseBasicParsing `
                -Headers @{ "User-Agent" = "QuotaTray-updater"; "Accept" = "application/vnd.github+json" }
            $newTag = $rel.tag_name
        } catch {
            Log "GitHub API unavailable ($($_.Exception.Message)); reading the release page instead"
            $req = [System.Net.HttpWebRequest]::Create("https://github.com/$Repo/releases/latest")
            $req.AllowAutoRedirect = $false
            $req.UserAgent = "QuotaTray-updater"
            $resp = $req.GetResponse()
            $location = $resp.Headers["Location"]
            $resp.Close()
            if ($location -match "/releases/tag/([^/?#]+)") { $newTag = $Matches[1] }
            else { throw "could not determine the latest release (got '$location')" }
        }
    }
    $download = "https://github.com/$Repo/releases/download/$newTag"
    Log "target release: $newTag"

    $work = Join-Path ([IO.Path]::GetTempPath()) ("QuotaTray-update-" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
    New-Item -ItemType Directory -Force -Path $work | Out-Null

    function Get-Asset([string[]]$names) {
        # Newest naming first; releases before v1.3.2 used the plain names.
        foreach ($name in $names) {
            $out = Join-Path $work $name
            try {
                Invoke-WebRequest -Uri "$download/$name" -OutFile $out -Headers $headers -UseBasicParsing
                Log "downloaded $name"
                return $out
            } catch {
                $last = $_.Exception.Message
            }
        }
        throw "release $newTag has none of: $($names -join ', ') ($last)"
    }

    function Assert-Hash([string]$file, [string]$sums) {
        $name = Split-Path -Leaf $file
        $line = Get-Content -LiteralPath $sums | Where-Object { $_ -match ("\s\*?" + [regex]::Escape($name) + "\s*$") } | Select-Object -First 1
        if (-not $line) { throw "SHA256SUMS.txt has no entry for $name" }
        $want = ($line -split "\s+")[0].ToLower()
        $got = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLower()
        if ($want -ne $got) { throw "checksum mismatch for $name (expected $want, got $got)" }
        Log "checksum OK for $name"
    }

    $payload = $null
    if ($kind -eq "exe" -or $kind -eq "portable") {
        $arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "x64" }
        $base = "QuotaTray-$newTag-windows-$arch"
        $names = if ($kind -eq "exe") { @("$base.exe", "QuotaTray.exe") } else { @("$base-portable.zip", "QuotaTray-portable.zip") }
        $payload = Get-Asset $names
        Assert-Hash $payload (Get-Asset @("QuotaTray-$newTag-SHA256SUMS.txt", "SHA256SUMS.txt"))
    } else {
        $zip = Join-Path $work "source.zip"
        Log "downloading source for $newTag"
        Invoke-WebRequest -Uri "https://github.com/$Repo/archive/refs/tags/$newTag.zip" -OutFile $zip -Headers $headers -UseBasicParsing
        $payload = $zip
    }

    if ($ReadyFile) {
        # Only now may the tray app close: the new version is on disk and verified.
        Set-Content -LiteralPath $ReadyFile -Value $newTag -Encoding ASCII
        Log "download verified; the tray app can close now"
    }

    if ($DryRun) {
        Log "dry run: would update $dir ($kind) to $newTag using $payload"
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
        return
    }

    # ------------------------------------------------------------ stop the running copy
    if ($WaitPid -gt 0) {
        Log "waiting for the tray app (pid $WaitPid) to exit"
        Wait-Process -Id $WaitPid -Timeout 60 -ErrorAction SilentlyContinue
    }
    $script:Stopped = $true
    $dirPattern = "*" + $dir.TrimEnd("\") + "*"
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        ($_.Name -like "QuotaTray*.exe" -and $_.ExecutablePath -and $_.ExecutablePath -like $dirPattern) -or
        ($_.CommandLine -and $_.CommandLine -like "*run.pyw*" -and $_.CommandLine -like $dirPattern)
    } | ForEach-Object {
        Log "stopping pid $($_.ProcessId) ($($_.Name))"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2

    # ------------------------------------------------------------ replace the files
    $updated = $false
    try {
        if ($kind -eq "exe") {
            $target = Join-Path $dir $ExeName
            $backup = "$target.old"
            Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
            for ($i = 0; $i -lt 10; $i++) {
                try { Move-Item -LiteralPath $target -Destination $backup -Force; break }
                catch { if ($i -eq 9) { throw }; Start-Sleep -Seconds 1 }
            }
            try {
                Copy-Item -LiteralPath $payload -Destination $target -Force
            } catch {
                Move-Item -LiteralPath $backup -Destination $target -Force
                throw
            }
            Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
        } elseif ($kind -eq "source" -and (Test-Path -LiteralPath (Join-Path $dir ".git")) -and
                  (Get-Command git -ErrorAction SilentlyContinue) -and
                  $(& git -C $dir pull --ff-only 2>&1 | Out-Null; $LASTEXITCODE -eq 0)) {
            Log "git checkout fast-forwarded"
            $py = Join-Path $dir ".venv\Scripts\python.exe"
            if (Test-Path -LiteralPath $py) {
                & $py -m pip install -r (Join-Path $dir "requirements.txt") -q --disable-pip-version-check | Out-Null
            }
        } else {
            $unpacked = Join-Path $work "unpacked"
            Expand-Archive -LiteralPath $payload -DestinationPath $unpacked -Force
            $src = $unpacked
            if ($kind -eq "source") {
                # GitHub source zips wrap everything in one owner-repo-sha folder.
                $inner = Get-ChildItem -LiteralPath $unpacked -Directory | Select-Object -First 1
                if ($inner) { $src = $inner.FullName }
            }
            & robocopy $src $dir /E /R:5 /W:1 /XD .git .venv venv /NFL /NDL /NJH /NJS /NP | Out-Null
            if ($LASTEXITCODE -ge 8) { throw "copying the new files failed (robocopy exit $LASTEXITCODE)" }
            if ($kind -eq "source") {
                $py = Join-Path $dir ".venv\Scripts\python.exe"
                if (Test-Path -LiteralPath $py) {
                    Log "updating Python dependencies"
                    & $py -m pip install -r (Join-Path $dir "requirements.txt") -q --disable-pip-version-check | Out-Null
                }
            }
        }
        $updated = $true
        Log "updated $dir to $newTag"
    } finally {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($updated) {
        Write-Host ""
        Write-Host "  QuotaTray is now $newTag." -ForegroundColor Green
    }
}

try {
    Invoke-QuotaTrayUpdate
} catch {
    Write-Host ""
    Log "update failed: $($_.Exception.Message)"
} finally {
    # Whatever happened above, never leave the user without the tray app.
    if (-not $NoRestart -and -not $DryRun) {
        try { Start-QuotaTray } catch { Log "restart failed: $($_.Exception.Message)" }
    }
    if ($ReadyFile) { Remove-Item -LiteralPath $ReadyFile -Force -ErrorAction SilentlyContinue }
}
