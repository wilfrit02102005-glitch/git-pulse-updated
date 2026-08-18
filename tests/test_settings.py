"""Tests for the application configuration validation."""

from config.settings import Settings


def _clean() -> Settings:
    s = Settings()
    s.FLASK_ENV = "development"
    s.SECRET_KEY = "a-real-random-secret"
    s.GITHUB_OWNER = "acme-corp"
    s.GITHUB_REPO = "main-app"
    s.GITHUB_TOKEN = "github_pat_real_value"
    return s


def test_valid_config_passes():
    assert _clean().validate() == []


def test_production_flags_insecure_secret():
    s = _clean()
    s.FLASK_ENV = "production"
    s.SECRET_KEY = "dev-insecure-secret"

    problems = s.validate()

    assert any("SECRET_KEY" in p for p in problems)


def test_missing_owner_and_repo_are_optional():
    """Owner/repo are optional now: users pick a repo after login."""
    s = _clean()
    s.GITHUB_OWNER = ""
    s.GITHUB_REPO = ""

    problems = s.validate()

    assert not any("GITHUB_OWNER" in p for p in problems)
    assert not any("GITHUB_REPO" in p for p in problems)


def test_missing_token_is_reported():
    s = _clean()
    s.GITHUB_TOKEN = ""

    problems = s.validate()

    assert any("GITHUB_TOKEN" in p for p in problems)


def test_placeholder_token_is_reported():
    s = _clean()
    s.GITHUB_TOKEN = "github_pat_xxxxxxxxxxxxxxxxx"

    problems = s.validate()

    assert any("GITHUB_TOKEN" in p and "placeholder" in p for p in problems)


def test_config_status_reports_presence_without_leaking_token():
    s = _clean()
    status = s.config_status()

    assert status["GITHUB_OWNER"] == "configured"
    assert status["GITHUB_REPO"] == "configured"
    assert status["GITHUB_TOKEN"] == "configured"
    # The secret itself must never appear in the status output.
    assert "github_pat_real_value" not in str(status)


def test_config_status_reports_missing_and_placeholder():
    s = _clean()
    s.GITHUB_OWNER = ""
    s.GITHUB_REPO = "your-repository-8"
    s.GITHUB_TOKEN = ""

    status = s.config_status()

    assert status["GITHUB_OWNER"] == "not configured"
    assert status["GITHUB_REPO"] == "placeholder value (edit .env)"
    assert status["GITHUB_TOKEN"] == "not configured"


def test_placeholder_values_are_reported():
    s = _clean()
    s.GITHUB_OWNER = "your-org-or-username"
    s.GITHUB_REPO = "your-repo"

    problems = s.validate()

    assert any("placeholder" in p for p in problems)


def test_edited_placeholder_values_are_reported():
    s = _clean()
    s.GITHUB_OWNER = "your-github-username-or-org"
    s.GITHUB_REPO = "your-repository-name"

    problems = s.validate()

    assert any("GITHUB_OWNER" in p and "placeholder" in p for p in problems)
    assert any("GITHUB_REPO" in p and "placeholder" in p for p in problems)


def test_oauth_configured_requires_both_credentials():
    s = _clean()
    s.GITHUB_CLIENT_ID = "id"
    s.GITHUB_CLIENT_SECRET = ""
    assert not s.oauth_configured

    s.GITHUB_CLIENT_SECRET = "secret"
    assert s.oauth_configured
