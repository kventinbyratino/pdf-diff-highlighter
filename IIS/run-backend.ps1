param(
  [Parameter(Mandatory = $true)]
  [string]$RepoPath,

  [string]$Port = "8000"
)

$ErrorActionPreference = 'Stop'

Set-Location $RepoPath

$env:PORT = $Port
$env:APP_ENVIRONMENT = 'prod'
$env:RESULT_ARTIFACT_ROOT = 'C:\pdf-diff-data\artifacts'
$env:COMPARISON_JOB_ROOT = 'C:\pdf-diff-data\jobs'
$env:USAGE_METRICS_PATH = 'C:\pdf-diff-data\usage_metrics.json'

$venvPython = Join-Path $RepoPath '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
  throw "Missing virtualenv Python: $venvPython"
}

& $venvPython -m waitress --listen=127.0.0.1:$Port app:app
