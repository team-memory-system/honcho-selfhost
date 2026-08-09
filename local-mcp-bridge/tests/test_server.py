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
