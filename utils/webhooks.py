"""
GitPulse - GitHub webhook support.

Incoming webhooks (push / pull_request / issues) are validated with
X-Hub-Signature-256 (HMAC-SHA256 of the raw body using GITHUB_WEBHOOK_SECRET),
then stored and surfaced on the dashboard's recent-activity feed.

Because the app is primarily API-polling based, webhooks are an
enhancement, not a requirement: the dashboard refreshes from the GitHub
REST API on every load. Webhook delivery simply keeps a recent-activity
log warm without extra API calls.

Setup (documented, safe):
    1. Set GITHUB_WEBHOOK_SECRET in .env (a long random string).
    2. In the repository settings: Settings -> Webhooks -> Add webhook.
    3. Payload URL:  https://<your-host>/webhook/github
    4. Content type: application/json
    5. Secret:       the same GITHUB_WEBHOOK_SECRET
    6. Events:       push, pull_request, issues
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, Optional

from config.logging_setup import get_logger
from config.settings import settings

logger = get_logger("app")


def verify_signature(payload_body: bytes, signature_header: Optional[str]) -> bool:
    """
    Validate the X-Hub-Signature-256 header against the raw request body.

    Returns True when:
        * A secret is configured AND the signature matches, or
        * No secret is configured and the app runs in development.
    In production, a missing secret means webhooks are rejected.
    """
    if not settings.GITHUB_WEBHOOK_SECRET:
        if settings.is_development:
            logger.warning("GITHUB_WEBHOOK_SECRET not set; accepting webhook in dev mode.")
            return True
        logger.warning("GITHUB_WEBHOOK_SECRET not set; rejecting webhook.")
        return False

    if not signature_header:
        return False

    expected = "sha256=" + hmac.new(
        settings.GITHUB_WEBHOOK_SECRET.encode("utf-8"),
        payload_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def handle_event(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize a webhook payload into a small activity record.

    Returns a dict suitable for the dashboard's recent-activity feed.
    Never crashes: unknown events produce a minimal record.
    """
    repo = ((payload.get("repository") or {}).get("full_name") or "") or None
    action = payload.get("action")

    if event == "push":
        sender = ((payload.get("pusher") or {}).get("name")) or (
            ((payload.get("sender") or {}).get("login")) or None
        )
        head = payload.get("head") or ""
        message = ""
        commit = payload.get("head_commit") or {}
        if commit:
            message = (commit.get("message") or "").split("\n")[0]
        return {
            "event": "push",
            "action": "pushed",
            "sender": sender,
            "repo": repo,
            "summary": f"{sender} pushed {head[:10]}" + (f": {message}" if message else ""),
            "ref": payload.get("ref", ""),
        }

    if event == "pull_request":
        sender = (payload.get("sender") or {}).get("login")
        pr = payload.get("pull_request") or {}
        summary = f"{sender} {action} PR #{pr.get('number')}: {pr.get('title', '')}"
        return {
            "event": "pull_request",
            "action": action,
            "sender": sender,
            "repo": repo,
            "summary": summary,
            "pr_number": pr.get("number"),
            "pr_url": pr.get("html_url"),
        }

    if event == "issues":
        sender = (payload.get("sender") or {}).get("login")
        issue = payload.get("issue") or {}
        summary = f"{sender} {action} issue #{issue.get('number')}: {issue.get('title', '')}"
        return {
            "event": "issues",
            "action": action,
            "sender": sender,
            "repo": repo,
            "summary": summary,
            "issue_number": issue.get("number"),
            "issue_url": issue.get("html_url"),
        }

    sender = (payload.get("sender") or {}).get("login")
    return {
        "event": event,
        "action": action,
        "sender": sender,
        "repo": repo,
        "summary": f"Webhook event '{event}'{(' (' + str(action) + ')') if action else ''}",
    }
