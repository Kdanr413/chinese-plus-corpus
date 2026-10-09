param([string]$Python = "python")
$ErrorActionPreference = "Stop"
$projectDirectory = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "../.."))
Push-Location -LiteralPath $projectDirectory
try {
    & $Python -m corpus_platform serve
    $portalExitCode = $LASTEXITCODE
} finally { Pop-Location }
exit $portalExitCode
