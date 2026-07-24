param(
  [Parameter(Mandatory = $true)]
  [string]$RepoPath,

  [string]$NssmExe = "C:\Tools\nssm\nssm.exe",
  [string]$ServiceName = "pdf-diff-highlighter"
)

$ErrorActionPreference = 'Stop'

Set-Location $RepoPath

git pull
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
& $NssmExe restart $ServiceName
Write-Host "Updated and restarted: $ServiceName"
