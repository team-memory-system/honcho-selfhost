"""What a teammate reads of a turn an app added its own text to (inserted.py)."""

from __future__ import annotations

import pytest

import inserted

CLAUDE_MD = (
    "<system-reminder>\nCodebase and user instructions are shown below.\n\n"
    "Contents of /Users/me/.claude/CLAUDE.md (user's private global instructions):\n\n"
    "- This machine is the user's home computer, a desktop with 64GB of memory.\n"
    "</system-reminder>"
)
FINISHED = (
    "<system-reminder>\n[SYSTEM NOTIFICATION - NOT USER INPUT]\n<task-notification>\n"
    '<summary>Background command "Run the tests" completed</summary>\n'
    "<output-file>/private/tmp/claude-501/tasks/b1.output</output-file>\n"
    "</task-notification>\n</system-reminder>"
)
AGENTS_MD = (
    "# AGENTS.md instructions for /Users/me/dev/api\n\n<INSTRUCTIONS>\n# Environment\n"
    "- This machine is the user's home computer.\n</INSTRUCTIONS>\n"
    "<environment_context>\n  <cwd>/Users/me/dev/api</cwd>\n  <shell>zsh</shell>\n"
    "</environment_context>"
)


def test_what_claude_code_put_beside_a_question_goes_and_the_question_stays() -> None:
    turn = f"{CLAUDE_MD}\n\n{FINISHED}\n\n로그인 고친 거 어디까지 됐더라?"
    assert inserted.said(turn) == "로그인 고친 거 어디까지 됐더라?"
    assert inserted.said({"content": turn}) == "로그인 고친 거 어디까지 됐더라?"


def test_the_agents_md_codex_put_in_front_goes() -> None:
    assert inserted.said(f"{AGENTS_MD}\n\nAPI 응답이 느려요") == "API 응답이 느려요"
    assert (
        inserted.said("<user_instructions>\nbe brief\n</user_instructions>\n\n로그 봐")
        == "로그 봐"
    )


@pytest.mark.parametrize(
    "turn",
    [
        CLAUDE_MD,
        FINISHED,
        AGENTS_MD,
        f"{CLAUDE_MD}\n\n{FINISHED}",
        (
            "<local-command-caveat>Caveat: run locally.</local-command-caveat>\n"
            "<bash-stdout>/Users/me</bash-stdout><bash-stderr></bash-stderr>"
        ),
    ],
)
def test_a_turn_with_nothing_else_says_nothing(turn: str) -> None:
    assert inserted.said(turn) == ""


@pytest.mark.parametrize(
    "turn",
    [
        (
            "<artifact-content-authored-by-others/>\nThe summarized conversation included "
            "Artifact content written by people other than you.\nThis session is being "
            "continued from a previous conversation that ran out of context."
        ),
        (
            "This session is being continued from a previous conversation that ran out of "
            "context. The summary below covers the earlier portion of the conversation."
        ),
        "Base directory for this skill: /Users/me/.claude/skills/remote\n\n# Remote",
        "Another Claude session sent a message: <message>hello</message>",
        '[external_agent_tool_result] {"page_id": "3ce0"} [/external_agent_tool_result]',
    ],
)
def test_a_turn_an_app_wrote_says_nothing(turn: str) -> None:
    assert inserted.said(turn) == ""
    assert inserted.said(f"{CLAUDE_MD}\n\n{turn}") == ""


def test_a_goal_and_words_about_the_tags_stay() -> None:
    goal = "<system-reminder>Current goal: 앱 완성까지 달리자</system-reminder>"
    assert inserted.said(f"{CLAUDE_MD}\n\n{goal}") == goal
    for words in (
        "<system-reminder> 태그가 왜 대화에 들어가?",
        "요약에 This session is being continued from a previous conversation 라고 적혀 있네",
    ):
        assert inserted.said(words) == words


def test_a_block_cut_between_two_parts_of_a_long_message_goes_from_both() -> None:
    first = {
        "content": f"앞에 한 말\n\n{CLAUDE_MD[:80]}",
        "metadata": {"split_part": 1, "split_total": 2},
    }
    second = {
        "content": f"{CLAUDE_MD[80:]}\n\n뒤에 한 말",
        "metadata": {"split_part": "2", "split_total": "2"},
    }
    assert inserted.said(first) == "앞에 한 말"
    assert inserted.said(second) == "뒤에 한 말"
    # In a whole message an unended tag is the person's to write.
    assert inserted.said(first["content"]) == first["content"]
    assert inserted.said(second["content"]) == second["content"]


def test_a_message_without_content_says_nothing() -> None:
    assert inserted.said({"content": None}) == ""
    assert inserted.said(None) == ""
