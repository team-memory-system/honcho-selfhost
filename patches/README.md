# Honcho core changes

`0001-selfhost-core.patch` is the complete `src/` and `tests/` difference between
official Honcho v3.2.2 and this self-host source, apart from 0002 and 0003 below. It began
as the difference between v3.2.1 and the previously deployed self-host source at
`4282bd243d925d8785a004afc13b8eb04eede3df`.

It adds retrieval instructions only on query embedding paths, leaving room for them
when a search query is cut to the token cap, accepts special-token look-alikes as
ordinary message text, and includes regression tests for each.
The dashboard, MCP bridge, and operational scripts remain ordinary files outside
the upstream submodule.

`0002-allowlist-chat-without-peer-card.patch` bounds peer chat in two ways. Under a
session allowlist (`filters.session_id`, or a list of `scope`s), which limits a chat
to the sessions it names, it loads neither peer card. Upstream already confines
recall under an allowlist, but `agentic_chat` and `agentic_chat_stream` still put
the observer's and the observed peer's peer cards in the prompt. A peer card is
built from every session, so it carried facts from outside the allowlist into the
answer; upstream notes this in `src/routers/sessions.py` (DEV-2201). With or
without an allowlist, the agent's tool executor runs only the tools offered to the
model for that query. The tool loop runs any tool name the model emits, and the
shared handler table also holds writes (`update_peer_card`, `delete_observations`,
`create_observations*`) and reads that ignore an allowlist, so upstream lets a chat,
which only reads and which the MCP bridge can open to a teammate, replace a peer card
or delete conclusions once a question, or text the model recalls, gets it to emit
one of those names. Before dropping this patch for an official release, check that
both still hold.

`0003-refresh-claim-while-item-runs.patch` keeps a worker's claim on a work unit
fresh while it runs. Upstream refreshes the claim only when an item finishes, so one
item that runs longer than `STALE_SESSION_TIMEOUT_MINUTES` (5 by default), such as
a long dream, looked abandoned: stale cleanup deleted the claim and another worker
ran the same item again while the first was still on it. A dream measured on
2026-10-12 was taken again after 313 seconds. The worker now refreshes its claim
five times per timeout and stops once the claim is gone. A worker that dies still
leaves its claim to go stale after the same timeout.

`selfhost-source.json` defines patch order. `scripts/prepare-source.mjs` checks each
patch, applies it to an isolated export, and records its SHA-256 in the prepared
source's `.honcho-source.json`. It never patches the upstream checkout. A failed
application leaves the previous prepared source intact.

To change a patch, work in a disposable export of the pinned official commit,
make the change and tests there, then regenerate the patch. Update the upstream
gitlink, manifest, and version file together when adopting an official release.
