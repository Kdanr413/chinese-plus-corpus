[CmdletBinding()]
param([ValidateSet('builder','search','check','topics','init-db')][string]$Mode='builder',[string]$BindHost='127.0.0.1',[int]$Port=8001)
$ErrorActionPreference='Stop'
Set-Location -LiteralPath $PSScriptRoot
$runtime=Join-Path $PSScriptRoot 'runtime';$python=Join-Path $runtime 'python.exe'
$env:PYTHONNOUSERSITE='1';$env:PYTHONUTF8='1';$env:CORPUS_PROJECT_ROOT=$PSScriptRoot
if (Test-Path Env:PYTHONHOME) { Remove-Item Env:PYTHONHOME }
if (Test-Path Env:PYTHONPATH) { Remove-Item Env:PYTHONPATH }
if (-not (Test-Path -LiteralPath $python)) { Expand-Archive -LiteralPath (Join-Path $PSScriptRoot 'runtime-win64.zip') -DestinationPath $runtime }
$env:PATH="$runtime;$runtime\Library\bin;$runtime\DLLs;$runtime\Scripts;"+$env:PATH
if (-not (Test-Path -LiteralPath (Join-Path $runtime '.ready'))) {
 & $python (Join-Path $runtime 'Scripts\conda-unpack-script.py')
 if ($LASTEXITCODE -ne 0) { throw 'Runtime relocation failed' }
 New-Item -ItemType File -Path (Join-Path $runtime '.ready') | Out-Null
}
if ($Mode -eq 'builder') {
 if (-not (Test-Path -LiteralPath '.env')) { throw 'Copy .env.example to .env and configure PostgreSQL first; see README.md.' }
 & $python -m corpus_platform serve --setup
} elseif ($Mode -eq 'search') {
 & $python -m corpus_delivery.search --releases data/releases --host $BindHost --port $Port --open-browser
} elseif ($Mode -eq 'check') {
 & $python -m corpus_delivery.catalog data/releases
} elseif ($Mode -eq 'init-db') {
 & $python -m corpus_registry init-db
} else {
 & $python -m pip install -r requirements-topics.txt
 if ($LASTEXITCODE -ne 0) { throw 'Topic dependencies install failed' }
 & $python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-zh-v1.5')"
}
if ($LASTEXITCODE -ne 0) { throw "Operation failed: $LASTEXITCODE" }
