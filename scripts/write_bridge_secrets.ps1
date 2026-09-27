# Windows counterpart of write_bridge_secrets.sh.
#
# Puts the MCP bridge bearer tokens on disk from 1Password so no plist, scheduled
# task or compose file holds one. The bridges read HONCHO_MCP_BEARER_TOKEN_FILE and
# docker-compose.selfhost.yml mounts these two files read-only.
#
# Windows has no $HOME, so set HONCHO_CONFIG_DIR before running compose:
#   $env:HONCHO_CONFIG_DIR = "$env:USERPROFILE\.config\honcho"
#
# Creating the 1Password items is a write and needs the desktop app, so it stays a
# one-time manual step. Give the two bridges different tokens: sharing one means
# whoever holds the teammates' token can also call the owner's bridge.
#
#   op item create --category="API Credential" --vault Agent `
#     --title "honcho mcp bridge token" credential="$(-join ((1..32) | % { '{0:x2}' -f (Get-Random -Max 256) }))"

$ErrorActionPreference = 'Stop'

$configHome = if ($env:HONCHO_CONFIG_DIR) { $env:HONCHO_CONFIG_DIR } else { Join-Path $env:USERPROFILE '.config\honcho' }

function Write-BridgeToken {
    param([string]$Directory, [string]$Item)

    New-Item -ItemType Directory -Force -Path $Directory | Out-Null
    $target = Join-Path $Directory 'bearer-token'
    $token = (op read "op://Agent/$Item/credential").Trim()
    if ([string]::IsNullOrWhiteSpace($token)) { throw "empty token for $Item" }

    # No trailing newline, and no other account on the machine can read it.
    $temporary = "$target.tmp"
    [System.IO.File]::WriteAllText($temporary, $token, (New-Object System.Text.UTF8Encoding $false))
    $account = "$env:USERDOMAIN\$env:USERNAME"
    icacls $temporary /inheritancelevel:r /grant:r "${account}:F" /q | Out-Null
    Move-Item -Force -Path $temporary -Destination $target
    Write-Output "$target <- op://Agent/$Item/credential"
}

Write-BridgeToken -Directory (Join-Path $configHome 'mcp-bridge')   -Item 'honcho mcp bridge token'
Write-BridgeToken -Directory (Join-Path $configHome 'coworker-mcp') -Item 'honcho shared mcp bridge token'
