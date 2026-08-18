"""
GitPulse - authentication helpers.

Provides:
  * `create_github_oauth(app)` - registers the GitHub OAuth provider.
  * `login_required(fn)` - route guard that redirects to /login.
  * `is_login_rate_limited(ip)` - brute-force protection for /auth/login.
  * `is_user_allowed(username)` - access control via ALLOWED_GITHUB_USERS.
"""

from __future__ import annotations

import base64
import hashlib
import time
from collections import defaultdict, deque
from functools import wraps
from typing import Callable

from cryptography.fernet import Fernet, InvalidToken
from flask import redirect, request, session, url_for

from config.logging_setup import get_logger
from config.settings import settings

logger = get_logger("auth")

# ----------------------------------------------------------------------
# OAuth provider registration
# ----------------------------------------------------------------------
def create_github_oauth(app):
    """Register GitHub as an OAuth provider on the Flask app."""
    from authlib.integrations.flask_client import OAuth

    oauth = OAuth(app)
    oauth.register(
        name="github",
        client_id=settings.GITHUB_CLIENT_ID,
        client_secret=settings.GITHUB_CLIENT_SECRET,
        authorize_url="https://github.com/login/oauth/authorize",
        authorize_params=None,
        access_token_url="https://github.com/login/oauth/access_token",
        access_token_params=None,
        refresh_token_url=None,
        client_kwargs={"scope": "read:user user:email repo"},
    )
    return oauth


# ----------------------------------------------------------------------
# Session helpers
# ----------------------------------------------------------------------
def _session_fernet() -> Fernet:
    """Build a stable encryption key from the Flask secret key."""
    digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypt_session_token(token: str) -> str:
    return "enc1:" + _session_fernet().encrypt(token.encode("utf-8")).decode("ascii")


def _decrypt_session_token(value: str) -> str:
    # Backward compatibility: old sessions stored the token as plaintext.
    # They are transparently upgraded to encrypted storage on the next login.
    if not value.startswith("enc1:"):
        return value
    try:
        return _session_fernet().decrypt(value[5:].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeDecodeError):
        logger.warning("Stored GitHub session token could not be decrypted.")
        return ""


def store_session_token(token: str, method: str) -> None:
    """
    Store a validated token in the secure session cookie.

    Args:
        token:  The GitHub token (OAuth or PAT).
        method: 'oauth' or 'pat' - recorded for audit logging.
    """
    session["github_token"] = _encrypt_session_token(token)
    session["auth_method"] = method
    session.permanent = True  # make the session respect PERMANENT_SESSION_LIFETIME
    logger.info("Session token stored (method=%s)", method)


def get_session_token() -> str:
    """Return the token stored in the current session, or ''."""
    value = session.get("github_token", "") or ""
    return _decrypt_session_token(value) if isinstance(value, str) else ""


def clear_session_token() -> None:
    """Remove the token and wipe the session (used on logout)."""
    session.clear()


def is_logged_in() -> bool:
    """True when a token is present in the session."""
    return bool(get_session_token())


def get_selected_repo() -> str:
    """Return the repository ('owner/name') selected in the session, or ''."""
    return session.get("selected_repo", "") or ""


def set_selected_repo(repo: str) -> None:
    """
    Store the user's chosen repository in the session.

    Accepts 'owner/name' (or 'owner/name/' / with surrounding whitespace)
    and normalizes it. An empty value clears the selection.
    """
    repo = (repo or "").strip().strip("/")
    if repo:
        session["selected_repo"] = repo
    else:
        session.pop("selected_repo", None)


# ----------------------------------------------------------------------
# Access control
# ----------------------------------------------------------------------
def is_user_allowed(username: str) -> bool:
    """
    Enforce the ALLOWED_GITHUB_USERS allow-list.

    An empty allow-list means "allow everyone" (convenient for local dev).
    """
    if not settings.ALLOWED_GITHUB_USERS:
        logger.warning("ALLOWED_GITHUB_USERS is empty - access control disabled!")
        return True
    return username.strip().lower() in settings.ALLOWED_GITHUB_USERS


# ----------------------------------------------------------------------
# Login rate limiting (brute-force protection)
# ----------------------------------------------------------------------
# Map IP -> deque of timestamps of recent login attempts.
_login_attempts: dict[str, deque] = defaultdict(deque)


def record_login_attempt(ip: str) -> None:
    """Record a login attempt timestamp for an IP."""
    now = time.time()
    attempts = _login_attempts[ip]
    attempts.append(now)
    # Drop attempts older than the window to keep memory bounded.
    while attempts and now - attempts[0] > settings.RATE_LIMIT_WINDOW_SECONDS:
        attempts.popleft()


def is_login_rate_limited(ip: str) -> bool:
    """
    Return True when an IP has exceeded the allowed attempts in the window.

    This is an in-memory heuristic - fine for a single process deployment.
    """
    now = time.time()
    attempts = _login_attempts[ip]
    while attempts and now - attempts[0] > settings.RATE_LIMIT_WINDOW_SECONDS:
        attempts.popleft()
    limited = len(attempts) >= settings.RATE_LIMIT_MAX_ATTEMPTS
    if limited:
        logger.warning("Login rate limit hit for IP %s", ip)
    return limited


# ----------------------------------------------------------------------
# Route guard
# ----------------------------------------------------------------------
def login_required(fn: Callable):
    """Protect a route: redirect unauthenticated users to /login."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_logged_in():
            logger.info("Blocked unauthenticated access to %s", request.path)
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrapper
