"""Onboarding assets for /brain/setup: the downloadable PowerShell installer and
the tiny progressive copy-to-clipboard script.

The PowerShell script is a raw template with ``__MCP_URL__`` / ``__REPO__`` tokens
filled by ``render_setup_script`` — kept token-based (not str.format/f-string) so
PowerShell's own ``{ }`` blocks need no escaping.
"""

from __future__ import annotations

_PS1_TEMPLATE = r"""#Requires -Version 5.1
<#
  Engram setup — connect this PC to Hiren's brain (MCP tools + skill).
  Safe to re-run: every step is idempotent. It never edits anything but your
  Claude Code MCP config and ~/.claude/skills/engram/SKILL.md.
#>
$ErrorActionPreference = 'Stop'
$McpUrl   = '__MCP_URL__'
$Repo     = '__REPO__'
$SkillDir = Join-Path $HOME '.claude/skills/engram'

Write-Host ''
Write-Host '== Engram setup ==' -ForegroundColor Cyan
Write-Host "MCP: $McpUrl"
Write-Host "Repo: $Repo"
Write-Host ''

# 1) Register the Engram MCP server with Claude Code (idempotent).
$claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claude) {
    Write-Warning 'Claude Code CLI (claude) not found on PATH. Install Claude Code, then re-run. Skipping MCP registration.'
} else {
    $existing = ''
    try { $existing = (& claude mcp list) -join "`n" } catch { $existing = '' }
    if ($existing -match '(?m)^\s*engram\b' -or $existing -match 'engram\s') {
        Write-Host '[ok] MCP server "engram" already registered — left as is.' -ForegroundColor Green
    } else {
        & claude mcp add --transport http --scope user engram $McpUrl
        if ($LASTEXITCODE -eq 0) {
            Write-Host '[ok] Registered MCP server "engram".' -ForegroundColor Green
            Write-Host '     Next: open a session, run /mcp -> engram -> Authenticate (GitHub).'
        } else {
            Write-Warning 'claude mcp add failed. Check the CLI and try again.'
        }
    }
}

# 2) Install the engram skill (SKILL.md) for Claude Code / Cowork.
$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $gh) {
    Write-Warning 'GitHub CLI (gh) not found. Install it and run: gh auth login  — then re-run to install the skill.'
} else {
    & gh auth status *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning 'gh is installed but not signed in. Run: gh auth login  — then re-run this script.'
    } else {
        New-Item -ItemType Directory -Force -Path $SkillDir | Out-Null
        $dest = Join-Path $SkillDir 'SKILL.md'
        $raw = & gh api "repos/$Repo/contents/skills/engram/SKILL.md" -H 'Accept: application/vnd.github.raw'
        if ($LASTEXITCODE -eq 0 -and $raw) {
            $raw | Out-File -FilePath $dest -Encoding utf8
            Write-Host "[ok] Installed skill -> $dest" -ForegroundColor Green
        } else {
            Write-Warning 'Could not download SKILL.md. Is your GitHub account on the brain repo allowlist?'
        }
    }
}

Write-Host ''
Write-Host 'Done.' -ForegroundColor Cyan
Write-Host ' - claude.ai (web/mobile): Settings -> Connectors -> add the MCP URL above, sign in with GitHub.'
Write-Host ' - Claude Code: just start a session; the engram skill loads automatically.'
Write-Host ''
"""


def render_setup_script(mcp_url: str, repo: str = "metalfinger/brain") -> str:
    """Fill the PowerShell installer template with the live MCP URL and repo."""
    return _PS1_TEMPLATE.replace("__MCP_URL__", mcp_url).replace("__REPO__", repo)
