"""Where the memory's files are, kept from a teammate.

Every conversation and message carries in its metadata where it came from on the
owner's computer: the transcript file and the line in it, the hash of the turn, the
folder the conversation ran in, the ids of the originals, the computer, the account.
A teammate reads the memory, not the owner's disk: on 2026-10-16 a teammate's agent
rebuilt the owner's folder layout and account names from the paths alone. So of a
teammate's answer only the metadata named in KEPT goes out, and a folder goes out as
its name alone ("agrune" for /Users/me/dev/agrune). The owner reads everything as it
is stored.
"""

from __future__ import annotations

import re
from typing import Any

#: The metadata a teammate reads, by key: which app a conversation or message came
#: from and which model, who spoke and whether a person did, when, the project and
#: its branch, and what the conversation was called. Every other key stays on the
#: server, a new one included.
KEPT: frozenset[str] = frozenset(
    {
        # the app, the agent and the model
        "source",
        "agent_provider",
        "source_app",
        "originator",
        "model",
        "model_provider",
        "default_model_slug",
        "entrypoint",
        # who spoke, and whether a person did
        "direct_user",
        "memory_origin",
        "automation_kind",
        "claude_role",
        "codex_role",
        "agy_role",
        "grok_role",
        "codex_phase",
        "purpose",
        "target_peer",
        # when
        "created_at",
        "create_time",
        "update_time",
        "last_turn_at",
        "last_imported_at",
        "last_updated",
        "session_start",
        "start_time",
        "date",
        "turn_count",
        # the work
        "project_name",
        "git_branch",
        "title",
        "summary",
        "topic",
        "task",
        "kind",
        "linear_issue",
        "is_archived",
        "is_starred",
        "split_part",
        "split_total",
    }
)

#: The folder a conversation ran in, as the collector recorded it: kept as its name.
FOLDER = "cwd"

#: A home folder, whose name is the account's: "/Users/me", "/home/me", "C:/Users/me".
_HOME = re.compile(r"^(?:/users|/home|[a-z]:/users)/[^/]+$")
_REPEATED_SLASHES = re.compile(r"/+")
_DRIVE_ROOT = re.compile(r"^[A-Za-z]:/?$")


def folder_name(path: str) -> str:
    """The folder's own name, with nothing of the path above it.

    "agrune" for /Users/me/dev/agrune or C:\\Users\\me\\dev\\agrune. A home folder
    itself is "~", since its name is the account's; a root ("/", "C:/") is itself.
    """
    text = _REPEATED_SLASHES.sub("/", path.replace("\\", "/"))
    if text == "/" or _DRIVE_ROOT.match(text):
        return text
    text = text.rstrip("/")
    if _HOME.match(text.casefold()):
        return "~"
    return text.rsplit("/", 1)[-1]


def hidden(value: Any) -> Any:
    """`value`, a tool's result, with every `metadata` in it cut to what a teammate reads.

    A dictionary under the key `metadata` keeps the keys in KEPT, and its folder as
    its name alone. Every other dictionary keeps its keys, lists and tuples come back
    as lists, a Pydantic model goes out as its JSON, and whatever else is there stays
    as it is.
    """
    if isinstance(value, dict):
        return {
            key: (
                _kept(inner)
                if key == "metadata" and isinstance(inner, dict)
                else hidden(inner)
            )
            for key, inner in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [hidden(inner) for inner in value]
    if hasattr(value, "model_dump"):
        return hidden(value.model_dump(mode="json"))
    return value


def _kept(metadata: dict[str, Any]) -> dict[str, Any]:
    kept = {key: inner for key, inner in metadata.items() if key in KEPT}
    folder = metadata.get(FOLDER)
    if isinstance(folder, str) and folder:
        kept[FOLDER] = folder_name(folder)
    return kept
