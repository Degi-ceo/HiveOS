[CmdletBinding()]
param(
    [string]$Source = "",
    [string]$Evidence = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$hive = Join-Path $repoRoot ".venv\Scripts\hive.exe"

if (-not (Test-Path -LiteralPath $hive -PathType Leaf)) {
    throw "Hive CLI was not found at $hive. Create the project virtual environment first."
}

if (-not $Source) {
    $Source = Join-Path $repoRoot "data\hive.sqlite"
}
if (-not $Evidence) {
    $Evidence = Join-Path $repoRoot "data\shadow\shadow-evidence.sqlite"
}

$sourcePath = (Resolve-Path -LiteralPath $Source -ErrorAction Stop).Path
$evidencePath = [System.IO.Path]::GetFullPath($Evidence)
if ([string]::Equals($sourcePath, $evidencePath, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Source and evidence databases must be different paths."
}

& $hive state verify --path $sourcePath
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& $hive shadow --source $sourcePath --evidence $evidencePath
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& $hive state verify --path $sourcePath
exit $LASTEXITCODE
