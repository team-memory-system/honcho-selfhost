from starlette.middleware.trustedhost import TrustedHostMiddleware

from src.config import AppSettings, settings
from src.main import app


def test_app_uses_configured_trusted_host_middleware(monkeypatch):
    monkeypatch.setenv("TRUSTED_HOSTS", '["localhost","127.0.0.1","api"]')
    configured = AppSettings().TRUSTED_HOSTS
    assert configured == ["localhost", "127.0.0.1", "api"]

    middleware = next(
        item for item in app.user_middleware if item.cls is TrustedHostMiddleware
    )
    assert middleware.kwargs["allowed_hosts"] == settings.TRUSTED_HOSTS
