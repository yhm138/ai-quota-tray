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
    [switch]$NoRestart,
    [switch]$DryRun              # resolve and download, change nothing
)

function Invoke-QuotaTrayUpdate {
    $ErrorActionPreference = "Stop"
    $ProgressPreference = "SilentlyContinue"   # 5.1's progress bar makes downloads crawl
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    } catch { }

    $logDir = if ($env:APPDATA) { Join-Path $env:APPDATA "QuotaTray" } else { Join-Path $HOME ".quota-tray" }
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $script:LogFile = Join-Path $logDir "update.log"

    function Log([string]$msg) {
        $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
        Write-Host "  $msg"
        try { Add-Content -Path $script:LogFile -Value $line -Encoding UTF8 } catch { }
    }

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
            if ((Split-Path -Leaf $q) -ieq "QuotaTray.exe") { $dir = Split-Path -Parent $q; break }
        }
        if ($dir) { Log "found install via run-at-login: $dir" }
    }
    if (-not $dir) {
        try {
            $procs = Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
                $_.Name -ieq "QuotaTray.exe" -or ($_.CommandLine -and $_.CommandLine -like "*run.pyw*")
            }
            foreach ($p in $procs) {
                if ($p.Name -ieq "QuotaTray.exe" -and $p.ExecutablePath) { $dir = Split-Path -Parent $p.ExecutablePath; break }
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

    if (Test-Path -LiteralPath (Join-Path $dir "run.pyw")) {
        $kind = "source"
    } elseif (Test-Path -LiteralPath (Join-Path $dir "QuotaTray.exe")) {
        if (Test-Path -LiteralPath (Join-Path $dir "_internal")) { $kind = "portable" } else { $kind = "exe" }
    } else {
        throw "$dir holds neither QuotaTray.exe nor run.pyw."
    }
    Log "install type: $kind"

    # ------------------------------------------------------------ pick the release
    $headers = @{ "User-Agent" = "QuotaTray-updater"; "Accept" = "application/vnd.github+json" }
    $api = if ($Tag) { "https://api.github.com/repos/$Repo/releases/tags/$Tag" } else { "https://api.github.com/repos/$Repo/releases/latest" }
    $rel = Invoke-RestMethod -Uri $api -Headers $headers -UseBasicParsing
    $newTag = $rel.tag_name
    Log "target release: $newTag"

    $work = Join-Path ([IO.Path]::GetTempPath()) ("QuotaTray-update-" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
    New-Item -ItemType Directory -Force -Path $work | Out-Null

    function Get-Asset([string]$name) {
        $asset = $rel.assets | Where-Object { $_.name -eq $name } | Select-Object -First 1
        if (-not $asset) { throw "release $newTag has no $name" }
        $out = Join-Path $work $name
        Log "downloading $name"
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $out -Headers $headers -UseBasicParsing
        return $out
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
        $assetName = if ($kind -eq "exe") { "QuotaTray.exe" } else { "QuotaTray-portable.zip" }
        $payload = Get-Asset $assetName
        Assert-Hash $payload (Get-Asset "SHA256SUMS.txt")
    } else {
        $zip = Join-Path $work "source.zip"
        Log "downloading source for $newTag"
        Invoke-WebRequest -Uri $rel.zipball_url -OutFile $zip -Headers $headers -UseBasicParsing
        $payload = $zip
    }

    if ($DryRun) {
        Log "dry run: would update $dir ($kind) to $newTag using $payload"
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
        return
    }

    # ------------------------------------------------------------ stop the running copy
    if ($WaitPid -gt 0) {
        Log "waiting for the tray app (pid $WaitPid) to exit"
        Wait-Process -Id $WaitPid -Timeout 30 -ErrorAction SilentlyContinue
    }
    $dirPattern = "*" + $dir.TrimEnd("\") + "*"
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        ($_.Name -ieq "QuotaTray.exe" -and $_.ExecutablePath -and $_.ExecutablePath -like $dirPattern) -or
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
            $target = Join-Path $dir "QuotaTray.exe"
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
        # Start it again even when the update failed, so the tray icon comes back.
        if (-not $NoRestart) {
            if ($kind -eq "source") {
                $pyw = Join-Path $dir ".venv\Scripts\pythonw.exe"
                if (-not (Test-Path -LiteralPath $pyw)) { $pyw = "pythonw.exe" }
                Start-Process -FilePath $pyw -ArgumentList ('"' + (Join-Path $dir "run.pyw") + '"') -WorkingDirectory $dir
            } else {
                Start-Process -FilePath (Join-Path $dir "QuotaTray.exe") -WorkingDirectory $dir
            }
            Log "QuotaTray restarted"
        }
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
    $msg = "update failed: $($_.Exception.Message)"
    Write-Host "  $msg" -ForegroundColor Red
    try { if ($script:LogFile) { Add-Content -Path $script:LogFile -Value ("{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg) } } catch { }
}
