# Honcho core changes

`0001-selfhost-core.patch` is the complete `src/` and `tests/` difference between
official Honcho v3.2.2 and this self-host source, apart from 0002 below. It began
as the difference between v3.2.1 and the previously deployed self-host source at
`4282bd243d925d8785a004afc13b8eb04eede3df`.

It adds retrieval instructions only on query embedding paths, leaving room for them
when a search query is cut to the token cap, accepts special-token look-alikes as
ordinary message text, and includes regression tests for each.
The dashboard, MCP bridge, and operational scripts remain ordinary files outside
the upstream submodule.

`0002-allowlist-chat-without-peer-card.patch` keeps peer chat inside a session
allowlist (`filters.session_id`, or a list of `scope`s), so a teammate's chat can be
limited to the sessions the owner opened. Upstream already confines recall under an
allowlist, but `agentic_chat` and `agentic_chat_stream` still put the observer's and
the observed peer's peer cards in the prompt. A peer card is built from every
session, so it carried facts from outside the allowlist into the answer; upstream
notes this in `src/routers/sessions.py` (DEV-2201). Under an allowlist the patch
loads neither card, and the agent's tool executor runs only the pair dialectic's
own tools: the tool loop runs any tool name the model emits, and the shared handler
table also holds reads that ignore the allowlist, and writes. Without an allowlist
nothing changes. Before dropping this patch for an official release, check that
both still hold.

`selfhost-source.json` defines patch order. `scripts/prepare-source.mjs` checks each
patch, applies it to an isolated export, and records its SHA-256 in the prepared
source's `.honcho-source.json`. It never patches the upstream checkout. A failed
application leaves the previous prepared source intact.

To change a patch, work in a disposable export of the pinned official commit,
make the change and tests there, then regenerate the patch. Update the upstream
gitlink, manifest, and version file together when adopting an official release.
