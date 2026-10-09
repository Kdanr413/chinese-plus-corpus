[CmdletBinding()]
param([ValidateSet('builder','search','check','topics','init-db')][string]$Mode='builder', [string]$BindHost='127.0.0.1', [int]$Port=8001, [string]$ReleasesPath='data/releases')
$ErrorActionPreference='Stop'
Set-Location -LiteralPath $PSScriptRoot
$runtime=Join-Path $PSScriptRoot 'runtime'
$python=Join-Path $runtime 'python.exe'
$env:PYTHONNOUSERSITE='1'
$env:PYTHONUTF8='1'
$env:SENTENCE_TRANSFORMERS_HOME=Join-Path $PSScriptRoot 'models'
$env:CORPUS_PROJECT_ROOT=$PSScriptRoot
if (Test-Path Env:PYTHONHOME) { Remove-Item Env:PYTHONHOME }
if (Test-Path Env:PYTHONPATH) { Remove-Item Env:PYTHONPATH }
if (-not (Test-Path -LiteralPath $python)) {
  $archive=Join-Path $PSScriptRoot 'runtime-win64.zip'
  if (-not (Test-Path -LiteralPath $archive)) { throw 'Missing runtime-win64.zip. Download the complete Windows release package.' }
  Write-Host 'Preparing portable Python runtime. Please wait...'
  Expand-Archive -LiteralPath $archive -DestinationPath $runtime
}
$env:PATH="$runtime;$runtime\Library\bin;$runtime\DLLs;$runtime\Scripts;"+$env:PATH
if (-not (Test-Path -LiteralPath (Join-Path $runtime '.ready'))) {
  & $python (Join-Path $runtime 'Scripts\conda-unpack-script.py')
  if ($LASTEXITCODE -ne 0) { throw 'Runtime relocation failed.' }
  New-Item -ItemType File -Path (Join-Path $runtime '.ready') | Out-Null
}
if ($Mode -eq 'search') {
  & $python -m corpus_delivery.search --releases $ReleasesPath --host $BindHost --port $Port --open-browser
} elseif ($Mode -eq 'check') {
  & $python -m corpus_delivery.catalog $ReleasesPath
} elseif ($Mode -eq 'topics') {
  & $python -m pip install -r requirements-topics.txt
  if ($LASTEXITCODE -ne 0) { throw 'Topic dependencies installation failed. Check your Internet connection.' }
  & $python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-zh-v1.5', cache_folder='models')"
} elseif ($Mode -eq 'init-db') {
  & $python -m corpus_registry init-db
} else {
  if (-not (Test-Path -LiteralPath '.env')) {
    Write-Host 'Copy .env.example to .env and configure your own PostgreSQL settings; see README.md.'
  }
  & $python -m corpus_delivery.builder
}
if ($LASTEXITCODE -ne 0) { throw "Operation failed with exit code $LASTEXITCODE" }
