"""
GitPulse - application configuration.

This module loads environment variables from the `.env` file (if present),
validates the critical ones, and exposes a single `Settings` object that
the rest of the application imports. Keeping config in one place means
there are no hard-coded secrets anywhere in the codebase.
"""

import os

from dotenv import load_dotenv

# Load variables from the .env file located in the project root.
# `override=False` means real environment variables win over .env values,
# which is important when running on platforms like Render / Railway.
load_dotenv(override=False)


class Settings:
    """Typed access to all environment configuration."""

    def __init__(self) -> None:
        # --- Flask core ---
        self.SECRET_KEY: str = os.getenv("SECRET_KEY", "dev-insecure-secret")
        self.FLASK_ENV: str = os.getenv("FLASK_ENV", "development")
        self.DEBUG: bool = os.getenv("FLASK_DEBUG", "0") == "1"
        self.BASE_URL: str = os.getenv("BASE_URL", "http://localhost:5000")

        # --- GitHub OAuth ---
        self.GITHUB_CLIENT_ID: str = os.getenv("GITHUB_CLIENT_ID", "")
        self.GITHUB_CLIENT_SECRET: str = os.getenv("GITHUB_CLIENT_SECRET", "")
        self.GITHUB_REDIRECT_URI: str = os.getenv(
            "GITHUB_REDIRECT_URI", f"{self.BASE_URL}/auth/callback"
        )

        # --- Access control ---
        raw_users: str = os.getenv("ALLOWED_GITHUB_USERS", "")
        # Normalize: split on comma, strip whitespace, drop empties, lowercase.
        self.ALLOWED_GITHUB_USERS: list[str] = [
            u.strip().lower() for u in raw_users.split(",") if u.strip()
        ]

        # --- GitHub data scope (optional) ---
        # These are only used as an optional bootstrap default for users who
        # have NOT picked a repository in the dashboard. Once a user selects
        # a repository it is stored in their session and wins over these.
        self.GITHUB_OWNER: str = os.getenv("GITHUB_OWNER", "")
        self.GITHUB_REPO: str = os.getenv("GITHUB_REPO", "")
        # Optional team slug (for organizations). When empty, contributors
        # are used as the "team".
        self.GITHUB_TEAM: str = os.getenv("GITHUB_TEAM", "")

        # --- GitHub API token (server-side fallback) ---
        # Used to build the dashboard report when the user has not logged in
        # with OAuth / PAT. Never logged or rendered; only existence is
        # reported in the startup configuration check.
        self.GITHUB_TOKEN: str = (os.getenv("GITHUB_TOKEN", "") or "").strip()

        # --- Webhook secret ---
        # Validates X-Hub-Signature-256 on incoming GitHub webhooks.
        self.GITHUB_WEBHOOK_SECRET: str = os.getenv("GITHUB_WEBHOOK_SECRET", "")

        # --- Activity monitoring ---
        # How far back (in days) activity is analyzed.
        self.ACTIVITY_WINDOW_DAYS: int = int(os.getenv("ACTIVITY_WINDOW_DAYS", "30"))
        # Days without activity before a member is flagged as INACTIVE.
        self.INACTIVE_DAYS: int = int(os.getenv("INACTIVE_DAYS", "30"))
        # Days without activity before a member is flagged as RECENTLY ACTIVE.
        self.RECENTLY_ACTIVE_DAYS: int = int(os.getenv("RECENTLY_ACTIVE_DAYS", "7"))

        # --- Activity score weights (must sum to 100) ---
        self.SCORE_WEIGHTS: dict[str, int] = {
            "commits": int(os.getenv("SCORE_WEIGHT_COMMITS", "40")),
            "prs": int(os.getenv("SCORE_WEIGHT_PRS", "25")),
            "reviews": int(os.getenv("SCORE_WEIGHT_REVIEWS", "20")),
            "issues": int(os.getenv("SCORE_WEIGHT_ISSUES", "15")),
        }
        # --- Activity level thresholds (score ranges) ---
        self.ACTIVITY_THRESHOLDS: dict[str, int] = {
            "highly_active": int(os.getenv("THRESHOLD_HIGHLY_ACTIVE", "80")),
            "active": int(os.getenv("THRESHOLD_ACTIVE", "60")),
            "low": int(os.getenv("THRESHOLD_LOW", "30")),
        }

        # --- AI auto-fix validation (off by default) ---
        # When enabled AND git is available, the fix is applied to a temp
        # checkout and the commands below are run before a PR is created.
        self.AI_FIX_LOCAL_VALIDATION: bool = os.getenv("AI_FIX_LOCAL_VALIDATION", "0") == "1"
        self.AI_FIX_TEST_CMD: str = os.getenv("AI_FIX_TEST_CMD", "python -m pytest")
        self.AI_FIX_LINT_CMD: str = os.getenv("AI_FIX_LINT_CMD", "")

        # --- Anthropic Claude (optional) ---
        self.ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
        self.ANTHROPIC_MODEL: str = os.getenv(
            "ANTHROPIC_MODEL", "claude-3-5-haiku-20241022"
        )

        # --- Logging ---
        self.LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
        self.LOG_DIR: str = os.getenv("LOG_DIR", "logs")

        # --- Rate limiting ---
        self.RATE_LIMIT_MAX_ATTEMPTS: int = int(
            os.getenv("RATE_LIMIT_MAX_ATTEMPTS", "5")
        )
        self.RATE_LIMIT_WINDOW_SECONDS: int = int(
            os.getenv("RATE_LIMIT_WINDOW_SECONDS", "300")
        )

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------
    @property
    def is_development(self) -> bool:
        """True when running locally for development."""
        return self.FLASK_ENV == "development"

    @property
    def oauth_configured(self) -> bool:
        """True when GitHub OAuth credentials are present in the environment."""
        return bool(self.GITHUB_CLIENT_ID and self.GITHUB_CLIENT_SECRET)

    @property
    def anthropic_configured(self) -> bool:
        """True when an Anthropic API key is available."""
        return bool(self.ANTHROPIC_API_KEY)

    @staticmethod
    def _looks_like_placeholder(value: str) -> bool:
        """True when a config value looks like an unfilled .env.example template."""
        if not value:
            return False
        lowered = value.strip().lower()
        tokens = (
            "your-",
            "example",
            "placeholder",
            "sample",
            "change-me",
            "changeme",
            "dummy",
            "github_pat_xxx",  # fine-grained PAT placeholder (xxxxxxxxxxx…)
        )
        return any(token in lowered for token in tokens)

    def config_status(self) -> dict[str, str]:
        """
        Report whether the critical GitHub settings are configured.

        Used by the startup check. Only presence/absence is reported - the
        token value itself is never included, so secrets cannot leak.
        """
        def _mark(value: str) -> str:
            if not value:
                return "not configured"
            if self._looks_like_placeholder(value):
                return "placeholder value (edit .env)"
            return "configured"

        return {
            "GITHUB_OWNER": _mark(self.GITHUB_OWNER),
            "GITHUB_REPO": _mark(self.GITHUB_REPO),
            "GITHUB_TEAM": _mark(self.GITHUB_TEAM),
            "GITHUB_TOKEN": "configured" if self.GITHUB_TOKEN else "not configured",
            "GITHUB_WEBHOOK_SECRET": "configured" if self.GITHUB_WEBHOOK_SECRET else "not configured",
            "ANTHROPIC_API_KEY": "configured" if self.ANTHROPIC_API_KEY else "not configured",
        }

    def validate(self) -> list[str]:
        """
        Run startup validation and return a list of problems.

        Returns an empty list when everything is fine. The app can then
        decide how to surface warnings without crashing outright.
        """
        problems: list[str] = []

        if self.SECRET_KEY == "dev-insecure-secret" and not self.is_development:
            problems.append("SECRET_KEY is not set to a secure value.")

        # Placeholder values (from .env.example or left over by an editor)
        # will not work against the real GitHub API / OAuth provider. Match
        # heuristically so slightly-edited placeholders are still caught.
        placeholder_fields = (
            "GITHUB_OWNER",
            "GITHUB_REPO",
            "GITHUB_TEAM",
            "GITHUB_CLIENT_ID",
            "GITHUB_CLIENT_SECRET",
            "GITHUB_TOKEN",
            "SECRET_KEY",
        )
        for name in placeholder_fields:
            if self._looks_like_placeholder(getattr(self, name, "")):
                problems.append(f"{name} still contains a placeholder value.")

        if not self.GITHUB_TOKEN:
            problems.append(
                "GITHUB_TOKEN is not configured. Set it in .env or log in "
                "with a Personal Access Token."
            )

        weight_sum = sum(self.SCORE_WEIGHTS.values())
        if weight_sum != 100:
            problems.append(
                f"Activity score weights sum to {weight_sum} (expected 100). "
                "Adjust SCORE_WEIGHT_* in .env."
            )

        if self.DEBUG and not self.is_development:
            problems.append("FLASK_DEBUG is enabled but FLASK_ENV is not 'development'.")

        return problems


# A single, importable settings instance for the whole app.
settings = Settings()
