"""What a teammate reads of where the memory's files are: nothing (provenance.py)."""

from __future__ import annotations

from typing import Any

import pytest

import provenance


@pytest.mark.parametrize(
    ("path", "name"),
    [
        ("/Users/me/dev/agrune", "agrune"),
        ("/Users/me/dev/agrune/", "agrune"),
        ("C:\\Users\\chenj\\dev\\portwarden\\", "portwarden"),
        ("/private/tmp//claude-501/x", "x"),
        ("/Users/laonpeople", "~"),
        ("/home/me/", "~"),
        ("C:\\Users\\chenj", "~"),
        ("c:/users/chenj", "~"),
        ("/", "/"),
        ("C:/", "C:/"),
        ("C:\\", "C:/"),
        ("/Users", "Users"),
    ],
)
def test_a_folder_goes_out_as_its_name_alone(path: str, name: str) -> None:
    assert provenance.folder_name(path) == name


def test_only_the_kept_keys_of_a_metadata_go_out_and_a_folder_as_its_name() -> None:
    session = {
        "id": "s-1",
        "metadata": {
            "cwd": "/Users/laonpeople/dev/agrune",
            "source": "claude",
            "version": "2.1.84",
            "file_path": "/Users/chenjing/.claude/projects/-Users-laonpeople-dev-agrune/99ea.jsonl",
            "git_branch": "main",
            "original_session_id": "99ea41cb",
            "project_name": "agrune",
            "project_id": "abc123",
            "last_turn_at": "2026-10-16T00:00:00Z",
            "memory_trigger": "manual_work_mac",
        },
        "configuration": {"dream": {"enabled": True}},
    }
    assert provenance.hidden(session) == {
        "id": "s-1",
        "metadata": {
            "cwd": "agrune",
            "source": "claude",
            "git_branch": "main",
            "project_name": "agrune",
            "last_turn_at": "2026-10-16T00:00:00Z",
        },
        "configuration": {"dream": {"enabled": True}},
    }


def test_a_message_keeps_who_spoke_and_loses_the_file_the_line_and_the_hash() -> None:
    message = {
        "id": "m-1",
        "content": "/Users/laonpeople 아래 뭐가 있는지",
        "metadata": {
            "source": "codex",
            "codex_role": "user",
            "codex_phase": "commentary",
            "direct_user": True,
            "memory_origin": "codex_user",
            "codex_line_index": 366,
            "codex_session_id": "codex-01a1",
            "source_turn_hash": "891f97be46173b272225700e93d457a7",
            "codex_rollout_path": "/Users/chenjing/.codex/sessions/2026/10/05/rollout.jsonl",
            "transcript_path": "/Users/chenjing/.claude/projects/x/y.jsonl",
        },
    }
    hidden = provenance.hidden({"items": [message], "total": 1})
    assert hidden == {
        "items": [
            {
                "id": "m-1",
                # Content is not metadata: the Jev gate judges it, redact.py hides numbers.
                "content": "/Users/laonpeople 아래 뭐가 있는지",
                "metadata": {
                    "source": "codex",
                    "codex_role": "user",
                    "codex_phase": "commentary",
                    "direct_user": True,
                    "memory_origin": "codex_user",
                },
            }
        ],
        "total": 1,
    }


def test_the_result_is_copied_and_other_shapes_are_left_alone() -> None:
    class Model:
        def model_dump(self, mode: str) -> dict[str, Any]:
            assert mode == "json"
            return {"metadata": {"cwd": "/a/b", "file_path": "/a/b/c.jsonl"}}

    result: dict[str, Any] = {
        "metadata": "not a dictionary",
        "nested": ({"metadata": {"source_turn_hash": "x", "kind": "note"}}, 3, None),
        "model": Model(),
        "empty": {"metadata": {}},
        "root": {"metadata": {"cwd": "/", "cwd_other": "/x"}},
    }
    hidden = provenance.hidden(result)
    assert hidden == {
        "metadata": "not a dictionary",
        "nested": [{"metadata": {"kind": "note"}}, 3, None],
        "model": {"metadata": {"cwd": "b"}},
        "empty": {"metadata": {}},
        "root": {"metadata": {"cwd": "/"}},
    }
    assert result["nested"][0]["metadata"] == {"source_turn_hash": "x", "kind": "note"}


def test_a_folder_that_is_not_text_is_dropped() -> None:
    assert provenance.hidden({"metadata": {"cwd": 7, "source": "claude"}}) == {
        "metadata": {"source": "claude"}
    }
    assert provenance.hidden({"metadata": {"cwd": "", "source": "claude"}}) == {
        "metadata": {"source": "claude"}
    }


def test_every_kept_key_is_about_the_app_the_speaker_the_time_or_the_work() -> None:
    # No key that names a file, a line, a hash, an id, a computer or an account.
    for key in provenance.KEPT:
        assert not key.endswith(
            ("_path", "_paths", "_hash", "_sha256", "_id", "_index")
        )
        assert key not in {"cwd", "file_path", "version", "cli_version", "platform"}
