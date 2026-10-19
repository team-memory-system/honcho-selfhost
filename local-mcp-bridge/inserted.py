"""What the agents' apps put into a person's turn, kept from a teammate.

Claude Code and Codex add text of their own to the turns a person types: the
CLAUDE.md and AGENTS.md files, the account and git context, word of a finished task,
the folder and the shell, what a command printed. They also write turns no person
typed: a summary of a conversation that ran out of room, a skill's instructions, a
message from another session, the result of a tool. The collector leaves all of it
out (Codex's since 2026-10-05, Claude Code's since 2026-10-19), but the memory holds
what it stored before as the person's own words: on 2026-10-19, 24 of the 32
messages that told teammates about the owner's computers were such text. A teammate
reads what a person typed and what an agent answered, so each message comes to them
without that text, and a message that held nothing else does not come at all. The
owner reads everything as it is stored.
"""

from __future__ import annotations

import re
from typing import Any

#: The blocks an app wraps around what it adds to a turn, and around what a command
#: printed. A goal reminder holds the person's own /goal and stays.
TAGS = (
    "system-reminder",
    "task-notification",
    "environment_context",
    "recommended_plugins",
    "user_instructions",
    "permissions instructions",
    "app-context",
    "local-command-caveat",
    "local-command-stdout",
    "local-command-stderr",
    "bash-stdout",
    "bash-stderr",
)
_NAMES = "|".join(re.escape(tag) for tag in TAGS)
_BLOCK = re.compile(rf"<({_NAMES})(?:\s[^<>]*)?>[\s\S]*?</\1>")
_GOAL = re.compile(r"^<system-reminder>\s*Current goal:")
#: The AGENTS.md or CLAUDE.md files in front of a Codex turn, under their heading.
_HEADING = r"# (?:AGENTS|CLAUDE)\.md instructions(?: for [^\n]*)?\s*"
_POLICY = re.compile(rf"{_HEADING}<INSTRUCTIONS>[\s\S]*?</INSTRUCTIONS>")
#: A long message goes into the memory in parts, and a block begun in one part ends
#: in the next: an opening left without its end, and an end left without its opening.
_OPENING = re.compile(rf"(?:{_HEADING})?<({_NAMES}|INSTRUCTIONS)(?:\s[^<>]*)?>")
_CLOSING = re.compile(rf"</({_NAMES}|INSTRUCTIONS)>")
#: Turns that are an app's from their first words.
_APP_TURN = re.compile(
    r"^(?:<artifact-content-authored-by-others/>"
    r"|This session is being continued from a previous conversation"
    r"|Base directory for this skill:"
    r"|Another Claude session sent a message:"
    r"|\[external_agent_tool_result\])"
)
_BLANK_LINES = re.compile(r"\n\s*\n\s*\n")


def said(message: Any) -> str:
    """What a message says to a teammate: its content without what an app put in,
    or "" when nothing else was in it. `message` is a Honcho message or its content."""
    if isinstance(message, dict):
        content = message.get("content")
        part, parts = _part(message.get("metadata"))
    else:
        content, part, parts = message, 1, 1
    text = "" if content is None else str(content)
    text = _BLOCK.sub(
        lambda block: block[0] if _GOAL.match(block[0]) else "", _POLICY.sub("", text)
    )
    if part > 1:
        text = _after_an_unopened_end(text)
    if part < parts:
        text = _before_an_unended_opening(text)
    text = _BLANK_LINES.sub("\n\n", text).strip()
    return "" if _APP_TURN.match(text) else text


def _part(metadata: Any) -> tuple[int, int]:
    """Which part of a long message this one is, and of how many: (1, 1) when whole."""
    if not isinstance(metadata, dict):
        return 1, 1
    try:
        return int(metadata.get("split_part") or 1), int(
            metadata.get("split_total") or 1
        )
    except (TypeError, ValueError):
        return 1, 1


def _after_an_unopened_end(text: str) -> str:
    for end in _CLOSING.finditer(text):
        if f"<{end[1]}" not in text[: end.start()]:
            return text[end.end() :]
    return text


def _before_an_unended_opening(text: str) -> str:
    for opening in _OPENING.finditer(text):
        if f"</{opening[1]}>" not in text[opening.end() :]:
            return text[: opening.start()]
    return text
