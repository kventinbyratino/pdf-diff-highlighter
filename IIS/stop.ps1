param(
  [string]$NssmExe = "C:\Tools\nssm\nssm.exe",
  [string]$ServiceName = "pdf-diff-highlighter"
)

$ErrorActionPreference = 'Stop'

& $NssmExe stop $ServiceName
Write-Host "Stopped: $ServiceName"
