"""Shared pytest fixtures for the GitPulse test suite."""

import pytest

from app import create_app


@pytest.fixture()
def app():
    return create_app()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def logged_in_client(app, client):
    """A client with a valid-looking session token."""
    with client.session_transaction() as sess:
        sess["github_token"] = "test-token"
        sess["github_user"] = "alice"
    return client


@pytest.fixture(autouse=True)
def _no_real_ai_calls(monkeypatch):
    """Keep the test suite offline: never call the real Claude API."""
    from config.settings import settings

    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
