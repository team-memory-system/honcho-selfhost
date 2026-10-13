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
and sends its own. With `HONCHO_MCP_SCOPE_FROM_GATE=1` every tool call reads them:

| `x-honcho-scope-mode` | Caller | The bridge |
|---|---|---|
| `all` | the server's owner | works as without the setting |
| `folders` | a teammate | answers the four teammate tools only, from the conversations in the folders in `x-honcho-allowed-folders` and of the apps in `x-honcho-allowed-sources` |
| `off` | a teammate the owner turned off for now | refuses every call, saying the owner turned their access off and that they can ask again once it is back on |
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
path keeps it. A session without a `cwd` in text, or with an empty one, is in no
folder.

A conversation in no folder, such as a ChatGPT import, opens by the app it came
from instead: its session's `metadata.source` when that is 1 to 32 of `a-z`, `0-9`,
`_`, `.` and `-` starting with a letter or digit, else `unknown`.
`x-honcho-allowed-sources` is base64url, with or without padding, of a UTF-8 JSON
list of such names, `["chatgpt"]` for every ChatGPT import. The gate sends it only
when some app is open, and without it none is; a list that is repeated or
malformed, or holds anything but such a name, refuses the call. An app opens only
its conversations in no folder: a session with a folder is open by its folder
alone.

A teammate's own agent reads those conversations and writes the answer itself; the
owner's model never runs for them. They have four tools, each held to the open
conversations:

- `list_conversations`: the open conversations, the most recently active first,
  with when each started and last ran, how many messages it has, its folder, app,
  project and branch, and the owner's first words in it. `since` keeps those active
  on or after a time, `until` those started on or before one, `contains` those whose
  folder, project, branch or opening holds every word given. A conversation's start
  is looked up once (its first page of messages) and kept until it brings in more.
- `search_messages`: one Honcho search (`POST /v3/workspaces/{ws}/search`) with
  `filters: {"session_id": {"in": [...]}}` naming the open sessions, or one of them
  (`conversation_id`), and `created_at` for `since` and `until`.
- `read_conversation`: one open conversation in order, a page at a time, from its
  start or (`newest_first`) its end.
- `search_conclusions`: the facts Honcho drew about the owner, only `explicit` ones
  and only from open sessions; facts that combine several conversations could carry
  what is not open.

Each takes `question`, the teammate's own words: the audit log keeps it as the
call's query, and the Jev gate judges it with the words searched for. Times are
Honcho's, in UTC; a bare date is a whole UTC day. A call is refused when the open
folders and apps hold no conversation, in the same words whether nothing is open or
what is open is empty. Any number may be open: a search names up to 20,000
sessions in its filter (each a bound parameter of one Postgres query), and more are
searched a share at a time, at once, the answers merged each share's best first,
since Honcho returns no score. A conversation named by id
that is not open is refused in the same words whether it is closed or does not
exist. Every other tool, `chat` among them, is refused.

The bridge lists the workspace's sessions (`POST
/v3/workspaces/{ws}/sessions/list`, 100 to a page) and keeps the list for 60
seconds, so a burst of questions lists them once and a conversation started now is
open within a minute. Calls that arrive while the list is read wait for it. The
folders themselves come with every call, so a folder the owner opens or closes
applies to the next question.

Refusals are recorded in the audit log as `denied`, before the Jev gate and before
Honcho. A session list that cannot be read is recorded as `error`, with its cause,
while the teammate is told only that it could not be listed. The arguments of a
teammate's call record the folders and apps as the gate sent them, as `folders`
and `sources`, and how many conversations the call was confined to, as
`sessions`. The setting does not pin the workspace or the peers; the team server's
bridge also sets `HONCHO_MCP_PIN_DEFAULTS=1` and `HONCHO_MCP_ENABLED_TOOLS` to the
four teammate tools.

## Upstream maintenance

This directory is an independently revertible local companion commit. Official
Honcho releases continue to arrive through the repository's existing
`integration/vX.Y.Z` worktree and `main` promotion workflow described in
`../LOCAL_CUSTOMIZATIONS.md`. Do not move this bridge into upstream-owned
`../mcp/`.
