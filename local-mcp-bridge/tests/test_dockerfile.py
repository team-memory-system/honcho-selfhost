"""The image holds every module of the bridge.

The Dockerfile names the files it copies one by one, so a module added beside
server.py is left out of the image until it is named there too: on 2026-10-16 the
mcp container restarted in a loop with "No module named 'provenance'" after
provenance.py was added and the tests, which run from the source tree, all passed.
"""

from __future__ import annotations

import re
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent.parent


def _copied() -> set[str]:
    names: set[str] = set()
    for line in (BRIDGE / "Dockerfile").read_text().splitlines():
        if re.match(r"^COPY\s+(?!--from)", line):
            names.update(line.split()[1:-1])
    return names


def test_every_module_beside_server_py_is_copied_into_the_image() -> None:
    modules = {path.name for path in BRIDGE.glob("*.py")}
    assert modules, "no modules found beside the Dockerfile"
    assert modules <= _copied(), f"not in the Dockerfile: {sorted(modules - _copied())}"


def test_every_local_import_of_server_py_is_a_module_beside_it() -> None:
    source = (BRIDGE / "server.py").read_text()
    imported = set(re.findall(r"^import (\w+)$", source, re.MULTILINE))
    local = {name for name in imported if (BRIDGE / f"{name}.py").exists()}
    assert local >= {"audit", "jev_gate", "provenance", "redact"}
    assert {f"{name}.py" for name in local} <= _copied()
