param(
    [Parameter(Mandatory = $true)][string]$AssemblyPath,
    [string]$OutputDirectory
)

# Run using Windows PowerShell 5.1 -NoProfile -STA. This loads the specified
# net48 build in-process; it never launches the tray app or reads user settings.
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSEdition -ne 'Desktop') {
    throw 'Use Windows PowerShell 5.1 (powershell.exe -NoProfile -STA).'
}
$assemblyFile = (Resolve-Path -LiteralPath $AssemblyPath).Path
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path (Split-Path -Parent $assemblyFile) 'panel-scroll-test-output'
}
[Reflection.Assembly]::LoadFrom($assemblyFile) | Out-Null
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
# Add-Type on Windows PowerShell accepts DLL references but rejects an EXE
# codebase. The bytes are identical; the tested assembly was loaded above.
$referenceFile = Join-Path $OutputDirectory 'QuotaTray.dll'
Copy-Item -LiteralPath $assemblyFile -Destination $referenceFile -Force
Add-Type -Path (Join-Path $PSScriptRoot 'PanelScrollRegression.cs') -ReferencedAssemblies @(
    $referenceFile, 'System.Drawing.dll', 'System.Windows.Forms.dll', 'System.Core.dll'
)
exit [PanelScrollRegression]::Run($OutputDirectory)
