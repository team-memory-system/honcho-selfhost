from types import SimpleNamespace

import pytest

import server


@pytest.mark.parametrize(
    ("path", "params", "blocked"),
    [
        ("/v3/workspaces/memory/peers/user_chen/card", None, True),
        ("/v3/workspaces/memory/peers/assistant/context", None, True),
        (
            "/v3/workspaces/memory/sessions/example/context",
            {"peer_target": "user_chen"},
            True,
        ),
        ("/v3/workspaces/memory/sessions/example/context", None, False),
        ("/v3/workspaces/memory/peers/user_chen/representation", None, False),
        ("/v3/workspaces/memory/search", None, False),
    ],
)
@pytest.mark.parametrize("hide_cards", [True, False])
def test_card_restriction(monkeypatch, path, params, blocked, hide_cards):
    monkeypatch.setattr(server, "HIDE_PEER_CARDS", hide_cards)
    monkeypatch.setattr(server, "MCP_TRANSPORT", "streamable-http")
    monkeypatch.setattr(
        server,
        "get_http_request",
        lambda: SimpleNamespace(headers={}),
    )
    if blocked and hide_cards:
        with pytest.raises(RuntimeError, match="Peer cards are disabled"):
            server._require_card_access(path, params)
    else:
        server._require_card_access(path, params)


def test_restriction_prevents_upstream_request(monkeypatch):
    monkeypatch.setattr(server, "HIDE_PEER_CARDS", True)
    monkeypatch.setattr(server, "MCP_TRANSPORT", "streamable-http")
    monkeypatch.setattr(server, "_require_auth", lambda: None)
    monkeypatch.setattr(
        server,
        "get_http_request",
        lambda: SimpleNamespace(headers={"cf-access-client-id": "coworker.access"}),
    )

    def unexpected_client(**kwargs):
        pytest.fail("Restricted card request reached upstream")

    monkeypatch.setattr(server.httpx, "Client", unexpected_client)
    with pytest.raises(RuntimeError, match="Peer cards are disabled"):
        server._request("GET", "/v3/workspaces/memory/peers/user_chen/card")
