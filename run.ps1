[CmdletBinding()]
param(
    [ValidateSet('ui', 'api', 'public-site', 'legacy-api', 'status', 'doctor', 'planning', 'design', 'rights', 'collect', 'clean', 'linguistic', 'topics', 'topic-only', 'term-only', 'publish-topics', 'publish-terms', 'quality', 'release', 'syntax', 'maintenance', 'term-llm')]
    [string]$Stage = 'ui',
    [string]$Config = '',
    [string]$PythonExe = $env:CORPUS_PYTHON,
    [switch]$DryRun,
    [switch]$Collect,
    [switch]$NoRebuild,
    [switch]$NoBrowser,
    [int]$Limit = 100
)

$ErrorActionPreference = 'Stop'

if ($Stage -eq 'ui') {
    $Stage = 'api'
}

# The ordinary launcher opens the unified account/project platform.
# A running service is reused; an unrelated or legacy service is never killed.
$healthyService = $false
if ($Stage -in @('api', 'legacy-api')) {
    $apiPort = if ($env:API_PORT) { [int]$env:API_PORT } else { 8000 }
    $environmentFile = Join-Path $PSScriptRoot '.env'
    if (-not $env:API_PORT -and (Test-Path -LiteralPath $environmentFile)) {
        foreach ($settingLine in Get-Content -LiteralPath $environmentFile -Encoding UTF8) {
            if ($settingLine -match '^\s*API_PORT\s*=\s*["'']?(\d+)["'']?\s*(?:#.*)?$') {
                $apiPort = [int]$Matches[1]
            }
        }
    }
    $workbenchUrl = "http://127.0.0.1:$apiPort/"
    $healthUrl = "http://127.0.0.1:$apiPort/health"
    $expectedService = if ($Stage -eq 'api') { 'corpus-portal' } else { 'chinese-plus-corpus-api' }
    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3
        $healthyService = $health.service -eq $expectedService
    } catch { $healthyService = $false }
    if ($healthyService) {
        Write-Host "Corpus platform is already running: $workbenchUrl" -ForegroundColor Green
    } else {
        $listener = Get-NetTCPConnection -LocalPort $apiPort -State Listen -ErrorAction SilentlyContinue
        if ($listener) {
            throw "Port $apiPort belongs to another or legacy service. Finish its tasks and stop it before starting the unified platform, or choose another API_PORT."
        }
    }
}

if (-not $Config) {
    $Config = Join-Path $PSScriptRoot 'configs\pipeline.json'
}

$activeCondaPython = if ($env:CONDA_PREFIX) { Join-Path $env:CONDA_PREFIX 'python.exe' } else { $null }
$namedCondaPython = $null
$condaCommand = Get-Command conda -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1
if ($condaCommand) {
    try {
        $condaInfo = (& $condaCommand env list --json 2>$null | ConvertFrom-Json)
        $namedEnvironment = $condaInfo.envs |
            Where-Object { (Split-Path -Leaf $_) -eq 'corpus-platform' } |
            Select-Object -First 1
        if ($namedEnvironment) {
            $namedCondaPython = Join-Path $namedEnvironment 'python.exe'
        }
    }
    catch {
        $namedCondaPython = $null
    }
}
$pathPython = Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1
$knownEnvironmentPythons = @(
    (Join-Path $env:ProgramData 'anaconda3\envs\corpus-platform\python.exe'),
    (Join-Path $env:USERPROFILE 'anaconda3\envs\corpus-platform\python.exe'),
    (Join-Path $env:USERPROFILE 'miniconda3\envs\corpus-platform\python.exe')
) | Where-Object { Test-Path -LiteralPath $_ }
$pythonSiblingEnvironment = if ($pathPython) { Join-Path (Split-Path -Parent $pathPython) 'envs\corpus-platform\python.exe' } else { $null }
$candidates = @($PythonExe, $activeCondaPython, $namedCondaPython, $pythonSiblingEnvironment) + $knownEnvironmentPythons + @($pathPython) |
    Where-Object { $_ } |
    Select-Object -Unique

$selectedPython = $null
foreach ($candidate in $candidates) {
    try {
        & $candidate -c "import sys; sys.exit(0)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $selectedPython = $candidate
            break
        }
    }
    catch {
        continue
    }
}

if (-not $selectedPython) {
    throw 'No working Python 3 found. Activate conda environment corpus-platform, set CORPUS_PYTHON, or pass -PythonExe.'
}

if ($Stage -eq 'public-site') {
    Push-Location -LiteralPath $PSScriptRoot
    try {
        $siteArgs = @('-m', 'corpus_delivery.public_launcher')
        # Config has a pipeline default above; only an explicitly chosen site config is forwarded.
        if ($Config -and $Config -ne (Join-Path $PSScriptRoot 'configs\pipeline.json')) { $siteArgs += @('--config', $Config) }
        if ($NoBrowser) { $siteArgs += '--no-browser' }
        & $selectedPython @siteArgs
        exit $LASTEXITCODE
    } finally { Pop-Location }
}

if ($Stage -in @('api', 'legacy-api', 'status', 'doctor')) {
    Push-Location -LiteralPath $PSScriptRoot
    try {
        if ($Stage -in @('api', 'legacy-api')) {
            if ($Stage -eq 'api' -and $healthyService) {
                & $selectedPython -m corpus_platform portal setup
                if ($LASTEXITCODE -ne 0) { throw 'Account initialization failed. Review the message above.' }
            }
            if ($healthyService) {
                if (-not $NoBrowser) { Start-Process $workbenchUrl }
                Write-Host "Open $workbenchUrl and sign in to your own project." -ForegroundColor Green
                exit 0
            }
            $env:API_PORT = [string]$apiPort
            $env:API_OPEN_BROWSER = if ($NoBrowser) { '0' } else { '1' }
            if ($Stage -eq 'legacy-api') { & $selectedPython -m corpus_platform serve --legacy }
            else { & $selectedPython -m corpus_platform serve --setup }
        } else {
            & $selectedPython -m corpus_platform $Stage
        }
        if ($LASTEXITCODE -ne 0) { throw "Corpus platform command failed with exit code $LASTEXITCODE." }
    } finally { Pop-Location }
    exit 0
}

$arguments = @('-m', 'corpus_pipeline', $Stage, '--config', $Config, '--project-root', $PSScriptRoot)
if ($DryRun) {
    $arguments += '--dry-run'
}
if ($Collect) {
    $arguments += '--collect'
}
if ($NoRebuild) {
    $arguments += '--no-rebuild'
}
if ($Stage -eq 'term-llm') {
    $arguments += @('--limit', $Limit)
}

& $selectedPython @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Corpus pipeline failed with exit code $LASTEXITCODE."
}
