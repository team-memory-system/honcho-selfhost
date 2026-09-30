# Honcho core changes

`0001-selfhost-core.patch` is the complete `src/` and `tests/` difference between
official Honcho v3.2.1 and the previously deployed self-host source at
`4282bd243d925d8785a004afc13b8eb04eede3df`.

It adds retrieval instructions only on query embedding paths, accepts special-token
look-alikes as ordinary message text, and includes both features' regression tests.
The dashboard, MCP bridge, and operational scripts remain ordinary files outside
the upstream submodule.

`selfhost-source.json` defines patch order. `scripts/prepare-source.mjs` checks each
patch, applies it to an isolated export, and records its SHA-256 in the prepared
source's `.honcho-source.json`. It never patches the upstream checkout. A failed
application leaves the previous prepared source intact.

To change a patch, work in a disposable export of the pinned official commit,
make the change and tests there, then regenerate the patch. Update the upstream
gitlink, manifest, and version file together when adopting an official release.
