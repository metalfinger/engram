# engram-doctor — round-trip health self-test of the brain deployment.
# Non-mutating (leaves no commit). Exits non-zero if any check FAILs.
#
# Usage (from anywhere):
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Users\Admin\Documents\code\engram\server\scripts\engram-doctor.ps1
$ErrorActionPreference = "Stop"

Set-Location "C:\Users\Admin\Documents\code\engram\server"
$env:PYTHONUTF8 = "1"

# --no-sync: production holds the venv lock; never re-resolve deps here.
& uv run --no-sync python -m engram_server.doctor
exit $LASTEXITCODE
