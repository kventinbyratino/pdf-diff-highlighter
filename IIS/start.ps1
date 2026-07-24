param(
  [string]$NssmExe = "C:\Tools\nssm\nssm.exe",
  [string]$ServiceName = "pdf-diff-highlighter"
)

$ErrorActionPreference = 'Stop'

& $NssmExe start $ServiceName
Write-Host "Started: $ServiceName"
