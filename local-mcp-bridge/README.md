# Honcho local MCP bridge

This companion service exposes the self-hosted Honcho v3 API as a streamable
HTTP MCP server. It is local customization source tracked on `main`; it
is not part of the official upstream `mcp/` Cloudflare Worker.

## Runtime path

The tracked source and host-specific runtime state are deliberately separate:

```text
<checkout>/local-mcp-bridge/    # Git-tracked source
~/.config/honcho/mcp-bridge/    # tool state and optional token
~/Library/Logs/Honcho/          # service logs
```

The production-compatible endpoint remains `http://127.0.0.1:8766/mcp`, under
whatever launchd label the installation already uses. Keeping the label, port,
and bearer value stable allows existing Codex and tunnel clients to survive a
source-path migration.

The current Codex identity is carried in request headers:

- workspace: `memory`
- user peer: `user_chen`
- assistant peer: `assistant_codex`

## Install and test

```bash
cd <checkout>/local-mcp-bridge
uv sync --frozen
uv run ruff check server.py tests
uv run pytest -q
```

For a local, unauthenticated development instance bound only to loopback:

```bash
uv run python server.py
```

## Production safety

The externally tunneled bridge must set all of the following:

```text
HONCHO_MCP_BEARER_TOKEN or HONCHO_MCP_BEARER_TOKEN_FILE
HONCHO_MCP_REQUIRE_AUTH=1
HONCHO_MCP_TOOL_CONFIG=~/.config/honcho/mcp-bridge/tool-config.json
HONCHO_MCP_REQUIRE_TOOL_CONFIG=1
HONCHO_MCP_TIMEOUT=300
```

When either required auth or required tool configuration is missing or invalid,
the process exits instead of exposing an unauthenticated or unexpectedly broad
tool set. Direct bearer-token environment variables take precedence over a
token file. Never commit a live token, Cloudflare credential, LaunchAgent with
inline secrets, or dashboard-generated tool state.

Seed a new runtime tool configuration from
`examples/tool-config.read-only.json`, then keep the live file outside Git with
mode `0600`. The example disables every current create, update, delete, dream,
and operational-inspection tool that is disabled in production.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `HONCHO_BASE_URL` | `http://127.0.0.1:8001` | Honcho v3 API |
| `HONCHO_WORKSPACE_ID` | `memory` | default workspace |
| `HONCHO_USER_NAME` | `user_chen` | default user peer |
| `HONCHO_ASSISTANT_NAME` | `assistant` | default assistant peer |
| `HONCHO_MCP_HOST` | `127.0.0.1` | listener host |
| `HONCHO_MCP_PORT` | `8765` | listener port; production overrides to `8766` |
| `HONCHO_MCP_PATH` | `/mcp` | streamable HTTP path |
| `HONCHO_MCP_TIMEOUT` | `300` | Honcho upstream HTTP timeout in seconds |
| `HONCHO_MCP_TOOL_CONFIG` | XDG config path | dashboard-managed tool state |
| `HONCHO_MCP_BEARER_TOKEN` | empty | direct MCP bearer secret |
| `HONCHO_MCP_BEARER_TOKEN_FILE` | empty | repo-external bearer file |
| `HONCHO_MCP_REQUIRE_AUTH` | false | fail startup when bearer is unavailable |
| `HONCHO_MCP_REQUIRE_TOOL_CONFIG` | false | fail startup when tool state is unavailable |
| `HONCHO_MCP_HIDE_PEER_CARDS` | false | omit card tools and reject card inclusion in session context for this process |
| `HONCHO_MCP_SCOPE_FROM_GATE` | false | behind a team server's gate: answer the owner as usual and a teammate only from the conversations in the folders the gate names (see below) |

`X-Honcho-Workspace-ID`, `X-Honcho-User-Name`, and
`X-Honcho-Assistant-Name` request headers override the corresponding defaults.

## Separate coworker endpoint

A second launchd service listens on `127.0.0.1:8767`, using its own tool
configuration in `~/.config/honcho/coworker-mcp/tool-config.json`. Its public
MCP URL is whatever hostname the operator's tunnel maps to that port; the
address is given to teammates directly and is not recorded here.

This process sets `HONCHO_MCP_HIDE_PEER_CARDS=1`. The card tools are not
registered, and session-context requests specifying `peer_target` are rejected.
Ordinary session context, search, and representation remain available. Request
headers cannot turn card tools back on. Search and representation still return
personal information; this configuration restricts card endpoints, not facts.

Cloudflare Access application `honcho-mcp-coworker` protects the new hostname.
The existing coworker service token is accepted there; the owner's original
MCP and API hosts retain their registered-device policy. Existing credentials
remain valid at the new URL. The owner's bridge has no coworker-header rule.

To migrate an existing client, change only the `honcho_chen` server URL to the
new URL, preserve its authentication headers, and restart the client from a
shell with its existing `HONCHO_CHEN_*` environment variables loaded. Verify
using `get_representation(peer_id="user_chen", observer_id="user_chen")`.

## Folders behind a team server's gate

A team server reaches this bridge only through its gate, which checks each
caller's Cloudflare Access login, drops every `x-honcho-*` header the caller sent,
and sends two of its own. With `HONCHO_MCP_SCOPE_FROM_GATE=1` every tool call reads
them:

| `x-honcho-scope-mode` | Caller | The bridge |
|---|---|---|
| `all` | the server's owner | works as without the setting |
| `folders` | a teammate | answers `chat` only, from the conversations in the folders in `x-honcho-allowed-folders` |
| missing, repeated or anything else | | refuses every call |

`x-honcho-allowed-folders` is base64url, with or without padding, of a UTF-8 JSON
list such as `[{"path": "/Users/me/dev/honcho", "subfolders": true}]`: the folders
the owner opened to that teammate. Each entry has exactly a non-empty `path` and a
`subfolders` that is true or false. A list that is missing, repeated or malformed
refuses the call, and so does an entry with any other key: the bridge cannot tell
what it was meant to hold back, and reading past it could open more than the owner
meant. An empty list is valid and opens nothing.

A conversation is in an open folder when the folder it started in, the session's
`metadata.cwd` as the collector records it, is that folder, or lies under it and
the entry has `subfolders`. Both paths are compared with backslashes read as `/`,
repeated `/` read as one, and a trailing `/` dropped, except from `/` itself or a
drive root such as `C:/`; a root with `subfolders` opens every path under it. A
path that starts with a drive letter ignores case, as Windows does, and any other
path keeps it. A session without a `cwd` in text is in no folder.

A teammate's `chat`:

- is one Honcho chat, about the server's owner (`HONCHO_USER_NAME`) unless
  `peer_id` or `target_peer_id` names another peer, with
  `filters: {"session_id": {"in": [...]}}` naming the sessions in the open
  folders. Honcho recalls only from those sessions, and the answer comes back as
  Honcho gave it.
- is refused when the open folders hold no conversation, in the same words whether
  nothing is open or what is open is empty, and when they hold more than 1000, the
  most one Honcho chat takes; the owner then has to open fewer folders.
- is refused with `session_id` or `filters`, and every other tool is refused.

The bridge lists the workspace's sessions (`POST
/v3/workspaces/{ws}/sessions/list`, 100 to a page) and keeps the list for 60
seconds, so a burst of questions lists them once and a conversation started now is
open within a minute. Calls that arrive while the list is read wait for it. The
folders themselves come with every call, so a folder the owner opens or closes
applies to the next question.

Refusals are recorded in the audit log as `denied`, before the Jev gate and before
Honcho. A session list that cannot be read is recorded as `error`, with its cause,
while the teammate is told only that it could not be listed. The arguments of a
teammate's call record the folders as the gate sent them, as `folders`, and how
many conversations the answer was confined to, as `sessions`. The setting does not
pin the workspace or the peers; the team server's bridge also sets
`HONCHO_MCP_PIN_DEFAULTS=1` and `HONCHO_MCP_ENABLED_TOOLS=chat`.

## Upstream maintenance

This directory is an independently revertible local companion commit. Official
Honcho releases continue to arrive through the repository's existing
`integration/vX.Y.Z` worktree and `main` promotion workflow described in
`../LOCAL_CUSTOMIZATIONS.md`. Do not move this bridge into upstream-owned
`../mcp/`.
