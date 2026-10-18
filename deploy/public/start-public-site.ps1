param(
    [string]$Python = "python",
    [string]$Config = "data/public_site/settings.json",
    [string]$ListenAddress = "127.0.0.1",
    [int]$Port = 8002
)
$ErrorActionPreference = "Stop"
$projectDirectory = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "../.."))
Push-Location -LiteralPath $projectDirectory
try {
    & $Python -m corpus_platform public-site --config $Config --host $ListenAddress --port $Port
    if ($LASTEXITCODE -ne 0) { throw "Public site exited with code: $LASTEXITCODE" }
} finally {
    Pop-Location
}
