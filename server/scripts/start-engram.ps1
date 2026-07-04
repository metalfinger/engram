# Starts the Engram MCP server with a daily log file.
# Wire into Task Scheduler (ONLOGON):
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Users\Admin\Documents\code\engram\server\scripts\start-engram.ps1
$ErrorActionPreference = "Stop"

Set-Location "C:\Users\Admin\Documents\code\engram\server"
$env:PYTHONUTF8 = "1"

$logDir = "C:\Users\Admin\.engram\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$log = Join-Path $logDir ("engram-{0}.log" -f (Get-Date -Format "yyyyMMdd"))
"[{0}] start-engram.ps1: launching engram-server" -f (Get-Date -Format "s") | Out-File -FilePath $log -Append -Encoding utf8

& uv run engram-server *>> $log
