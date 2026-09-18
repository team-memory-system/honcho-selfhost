from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, Self

import pytest

import server


def test_runtime_defaults_are_honcho_scoped() -> None:
    assert server.DEFAULT_WORKSPACE_ID == "memory"
    assert server.TIMEOUT_SECONDS == 300
    assert server.TOOL_CONFIG_PATH.name == "tool-config.json"
    assert server.TOOL_CONFIG_PATH.parent.name == "mcp-bridge"
    assert server.TOOL_CONFIG_PATH.parent.parent.name == "honcho"


def test_required_tool_config_reads_disabled_tools(tmp_path: Path) -> None:
    config_path = tmp_path / "tool-config.json"
    config_path.write_text(
        json.dumps({"version": 1, "disabled_tools": ["delete_session"]})
    )

    assert server._disabled_tool_names(config_path, required=True) == {"delete_session"}


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        (None, "missing"),
        ("not-json", "invalid"),
        ("[]", "must be an object"),
        ('{"disabled_tools": "all"}', "needs a disabled_tools list"),
    ],
)
def test_required_tool_config_fails_closed(
    tmp_path: Path,
    contents: str | None,
    message: str,
) -> None:
    config_path = tmp_path / "tool-config.json"
    if contents is not None:
        config_path.write_text(contents)

    with pytest.raises(RuntimeError, match=message):
        server._disabled_tool_names(config_path, required=True)


def test_bearer_token_file_is_supported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_path = tmp_path / "bearer-token"
    token_path.write_text("file-token\n")
    monkeypatch.delenv("HONCHO_MCP_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("HONCHO_MCP_BEARER_TOKEN_FILE", str(token_path))

    assert server._load_bearer_token() == "file-token"


def test_direct_bearer_token_precedes_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_path = tmp_path / "bearer-token"
    token_path.write_text("file-token\n")
    monkeypatch.setenv("HONCHO_MCP_BEARER_TOKEN", "direct-token")
    monkeypatch.setenv("HONCHO_MCP_BEARER_TOKEN_FILE", str(token_path))

    assert server._load_bearer_token() == "direct-token"


def test_auth_uses_configured_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "OPTIONAL_BEARER_TOKEN", "expected-token")
    monkeypatch.setattr(
        server,
        "get_http_request",
        lambda: SimpleNamespace(headers={"authorization": "Bearer expected-token"}),
    )
    server._require_auth()

    monkeypatch.setattr(
        server,
        "get_http_request",
        lambda: SimpleNamespace(headers={"authorization": "Bearer wrong-token"}),
    )
    with pytest.raises(RuntimeError, match="Unauthorized"):
        server._require_auth()


def test_stdio_transport_does_not_require_http_bearer_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "MCP_TRANSPORT", "stdio")
    monkeypatch.setattr(server, "OPTIONAL_BEARER_TOKEN", "inherited-token")
    monkeypatch.setattr(
        server,
        "get_http_request",
        lambda: (_ for _ in ()).throw(RuntimeError("no request context")),
    )

    server._require_auth()


def test_stdio_defaults_do_not_require_an_http_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        server,
        "get_http_request",
        lambda: (_ for _ in ()).throw(RuntimeError("no request context")),
    )

    assert server._resolve_defaults() == {
        "workspace_id": "memory",
        "user_name": "user_chen",
        "assistant_name": "assistant",
    }


def test_request_passes_300_second_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        content = b'{"status":"ok"}'
        headers: ClassVar[dict[str, str]] = {"content-type": "application/json"}

        @staticmethod
        def json() -> dict[str, str]:
            return {"status": "ok"}

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def request(
            self,
            method: str,
            url: str,
            *,
            json: object,
            params: object,
        ) -> FakeResponse:
            captured.update(
                {"method": method, "url": url, "json": json, "params": params}
            )
            return FakeResponse()

    monkeypatch.setattr(server, "OPTIONAL_BEARER_TOKEN", "")
    monkeypatch.setattr(server.httpx, "Client", FakeClient)

    assert server._request("GET", "/health") == {"status": "ok"}
    assert captured["timeout"] == 300
    assert captured["url"] == "http://127.0.0.1:8001/health"


def test_read_limits_are_clamped_before_upstream_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []
    monkeypatch.setattr(
        server,
        "_resolve_defaults",
        lambda **_kwargs: {
            "workspace_id": "memory",
            "user_name": "user_chen",
            "assistant_name": "assistant_agy",
        },
    )
    monkeypatch.setattr(
        server,
        "_request",
        lambda method, path, **kwargs: requests.append(
            {"method": method, "path": path, **kwargs}
        ),
    )

    server.search("query", limit=500)
    server.get_representation("user_chen", search_max_distance=1.5, max_conclusions=120)

    assert requests[0]["body"]["limit"] == 100  # type: ignore[index]
    assert requests[1]["body"]["search_max_distance"] == 1.0  # type: ignore[index]
    assert requests[1]["body"]["max_conclusions"] == 100  # type: ignore[index]


def test_message_pagination_is_not_sent_as_column_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        server,
        "_resolve_defaults",
        lambda **_kwargs: {
            "workspace_id": "memory",
            "user_name": "user_chen",
            "assistant_name": "assistant_agy",
        },
    )
    monkeypatch.setattr(
        server,
        "_request",
        lambda method, path, **kwargs: captured.update(
            {"method": method, "path": path, **kwargs}
        ),
    )

    server.get_session_messages(
        "session-1",
        filters={"page": 2, "limit": 500, "peer_id": "user_chen"},
    )

    assert captured["body"] == {"filters": {"peer_id": "user_chen"}}
    assert captured["params"] == {"reverse": "false", "page": 2, "size": 100}
