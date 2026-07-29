[CmdletBinding()]
param(
    [string]$PythonExe = $env:CORPUS_PYTHON,
    [string]$InputRoot = 'D:\production\corpus\processed\jsonl',
    [string]$OutputRoot = 'D:\production\corpus\processed\taxonomy_candidates',
    [string]$Config = 'D:\production\corpus\configs\topic_taxonomy_config.json',
    [int]$Clusters = 24,
    [switch]$UseLlm,
    [string]$LlmModel = $env:TOPIC_LLM_MODEL
)

$ErrorActionPreference = 'Stop'

function Test-PythonExecutable {
    param([string]$Candidate)
    if (-not $Candidate) {
        return $false
    }
    if (-not (Test-Path -LiteralPath $Candidate)) {
        return $false
    }
    try {
        & $Candidate -c "import sys; sys.exit(0)" 2>$null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

$candidates = @(
    $PythonExe,
    'D:\ProgramData\anaconda3\python.exe',
    (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
) | Where-Object { $_ } | Select-Object -Unique

$selectedPython = $null
foreach ($candidate in $candidates) {
    if (Test-PythonExecutable -Candidate $candidate) {
        $selectedPython = $candidate
        break
    }
}

if (-not $selectedPython) {
    throw 'No working Python found. Set CORPUS_PYTHON or pass -PythonExe with Python 3.8+.'
}

$scriptPath = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'build_topic_taxonomy.py'
$arguments = @(
    $scriptPath,
    '--input-root', $InputRoot,
    '--output-root', $OutputRoot,
    '--config', $Config,
    '--clusters', $Clusters
)

if ($UseLlm) {
    if (-not $env:TOPIC_LLM_API_KEY) {
        throw 'Set TOPIC_LLM_API_KEY before enabling -UseLlm.'
    }
    if (-not $LlmModel) {
        throw 'Set TOPIC_LLM_MODEL or pass -LlmModel before enabling -UseLlm.'
    }
    $arguments += @('--use-llm', '--llm-model', $LlmModel)
}

Write-Host "Python: $selectedPython"
Write-Host "Input:  $InputRoot"
Write-Host "Output: $OutputRoot"
& $selectedPython @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Topic taxonomy generation failed with exit code $LASTEXITCODE."
}
