param(
  [Parameter(Mandatory = $true)]
  [string]$RepoPath,

  [string]$PythonExe = "python",
  [string]$NssmExe = "C:\Tools\nssm\nssm.exe",
  [string]$ServiceName = "pdf-diff-highlighter",
  [string]$Port = "8000"
)

$ErrorActionPreference = 'Stop'

Set-Location $RepoPath

if (-not (Test-Path ".\.venv")) {
  & $PythonExe -m venv .venv
}

& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip setuptools wheel
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
& ".\.venv\Scripts\python.exe" -m pip install waitress

New-Item -ItemType Directory -Force -Path "C:\pdf-diff-data\artifacts" | Out-Null
New-Item -ItemType Directory -Force -Path "C:\pdf-diff-data\jobs" | Out-Null
New-Item -ItemType Directory -Force -Path "C:\pdf-diff-data\logs" | Out-Null

$runScript = Join-Path $RepoPath 'IIS\run-backend.ps1'
if (-not (Test-Path $runScript)) {
  throw "Missing backend wrapper script: $runScript"
}

if (-not (Test-Path $NssmExe)) {
  throw "NSSM not found: $NssmExe"
}

& $NssmExe install $ServiceName "powershell.exe" "-ExecutionPolicy Bypass -File `"$runScript`" -Port $Port -RepoPath `"$RepoPath`""
& $NssmExe set $ServiceName AppDirectory $RepoPath
& $NssmExe set $ServiceName Start SERVICE_AUTO_START
& $NssmExe set $ServiceName AppStdout "C:\pdf-diff-data\logs\$ServiceName.out.log"
& $NssmExe set $ServiceName AppStderr "C:\pdf-diff-data\logs\$ServiceName.err.log"
& $NssmExe set $ServiceName AppRotateFiles 1
& $NssmExe start $ServiceName

Write-Host "Backend installed and started as service: $ServiceName"
Write-Host "Health check: http://127.0.0.1:$Port/health"
