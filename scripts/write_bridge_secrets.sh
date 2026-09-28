#!/bin/sh
# Put the MCP bridge bearer tokens on disk from 1Password, so no plist and no
# compose file holds one.
#
# The bridges read HONCHO_MCP_BEARER_TOKEN_FILE, and docker-compose.selfhost.yml
# mounts these two files read-only. Reading is what the service account token
# allows; creating the items is a write and needs Touch ID, so that part is a
# one-time manual step:
#
#   env -u OP_SERVICE_ACCOUNT_TOKEN op item create --category="API Credential" \
#     --vault Agent --title "honcho mcp bridge token" credential="$(openssl rand -hex 32)"
#   env -u OP_SERVICE_ACCOUNT_TOKEN op item create --category="API Credential" \
#     --vault Agent --title "honcho shared mcp bridge token" credential="$(openssl rand -hex 32)"
#
# Give the two bridges different tokens. One value used for both means anyone
# holding the teammates' token can also call the owner's bridge.
set -eu

# Same knob docker-compose.selfhost.yml uses, so the files land where it mounts from.
config_home="${HONCHO_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/honcho}"

write_token() {
  directory="$1"
  item="$2"
  mkdir -p "$directory"
  target="$directory/bearer-token"
  temporary="$target.tmp.$$"
  umask 177
  op read "op://Agent/$item/credential" | tr -d '\n' > "$temporary"
  [ -s "$temporary" ] || { rm -f "$temporary"; echo "empty token for $item" >&2; exit 1; }
  mv "$temporary" "$target"
  chmod 600 "$target"
  printf '%s <- op://Agent/%s/credential\n' "$target" "$item"
}

write_token "$config_home/mcp-bridge" "honcho mcp bridge token"
write_token "$config_home/coworker-mcp" "honcho shared mcp bridge token"
