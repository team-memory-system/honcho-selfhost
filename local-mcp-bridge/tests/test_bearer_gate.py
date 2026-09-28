"""A wrong bearer token has to fail at `initialize`, not at the first tool call.

A teammate's installer checks its connection with `initialize` and `tools/list`.
When only `_require_auth` guarded the bridge, both succeeded with any token, so the
check reported a working connection that refused every real question.
"""

from __future__ import annotations

import pytest
from starlette.middleware import Middleware
from starlette.testclient import TestClient

import server

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "gate-test", "version": "0"},
    },
}
HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}


def _client() -> TestClient:
    app = server.mcp.http_app(
        path=server.MCP_PATH, middleware=[Middleware(server.BearerGate)]
    )
    return TestClient(app, base_url="http://127.0.0.1:8765")


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "right-token"])
def test_a_missing_or_wrong_token_is_refused_before_initialize(
    monkeypatch: pytest.MonkeyPatch, authorization: str | None
) -> None:
    monkeypatch.setattr(server, "OPTIONAL_BEARER_TOKEN", "right-token")
    headers = dict(HEADERS)
    if authorization:
        headers["authorization"] = authorization
    with _client() as client:
        response = client.post(server.MCP_PATH, json=INITIALIZE, headers=headers)
    assert response.status_code == 401
    assert "invalid bearer token" in response.json()["error"]


def test_the_right_token_reaches_initialize(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "OPTIONAL_BEARER_TOKEN", "right-token")
    with _client() as client:
        response = client.post(
            server.MCP_PATH,
            json=INITIALIZE,
            headers={**HEADERS, "authorization": "Bearer right-token"},
        )
    assert response.status_code == 200


def test_a_bridge_without_a_token_is_left_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local development runs without a token; the gate only enforces one it has."""
    monkeypatch.setattr(server, "OPTIONAL_BEARER_TOKEN", "")
    with _client() as client:
        response = client.post(server.MCP_PATH, json=INITIALIZE, headers=HEADERS)
    assert response.status_code == 200
