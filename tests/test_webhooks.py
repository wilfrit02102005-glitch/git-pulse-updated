"""Tests for GitHub webhook signature verification and normalization."""

import hashlib
import hmac

from utils import webhooks


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


class TestVerifySignature:
    def test_matches_when_secret_set(self, monkeypatch):
        monkeypatch.setattr(webhooks.settings, "GITHUB_WEBHOOK_SECRET", "hunter2")
        body = b'{"ref":"refs/heads/main"}'
        assert webhooks.verify_signature(body, _sign("hunter2", body)) is True

    def test_rejects_bad_signature(self, monkeypatch):
        monkeypatch.setattr(webhooks.settings, "GITHUB_WEBHOOK_SECRET", "hunter2")
        body = b'{"ref":"refs/heads/main"}'
        assert webhooks.verify_signature(body, _sign("wrong", body)) is False

    def test_rejects_missing_header(self, monkeypatch):
        monkeypatch.setattr(webhooks.settings, "GITHUB_WEBHOOK_SECRET", "hunter2")
        assert webhooks.verify_signature(b"body", None) is False

    def test_accepts_in_dev_without_secret(self, monkeypatch):
        monkeypatch.setattr(webhooks.settings, "GITHUB_WEBHOOK_SECRET", "")
        monkeypatch.setattr(webhooks.settings, "FLASK_ENV", "development")
        assert webhooks.verify_signature(b"body", None) is True

    def test_rejects_in_production_without_secret(self, monkeypatch):
        monkeypatch.setattr(webhooks.settings, "GITHUB_WEBHOOK_SECRET", "")
        monkeypatch.setattr(webhooks.settings, "FLASK_ENV", "production")
        assert webhooks.verify_signature(b"body", None) is False


class TestHandleEvent:
    def _payload(self, **overrides):
        payload = {
            "action": "opened",
            "sender": {"login": "alice"},
            "repository": {"full_name": "acme/app"},
        }
        payload.update(overrides)
        return payload

    def test_push(self):
        record = webhooks.handle_event(
            "push",
            {
                "pusher": {"name": "bob"},
                "sender": {"login": "bob"},
                "head": "abc123def456",
                "head_commit": {"message": "Add tests\n\nDetails"},
                "ref": "refs/heads/main",
                "repository": {"full_name": "acme/app"},
            },
        )
        assert record["event"] == "push"
        assert record["sender"] == "bob"
        assert record["summary"].startswith("bob pushed abc123def4: Add tests")

    def test_pull_request(self):
        payload = self._payload(
            pull_request={
                "number": 7,
                "title": "Fix login bug",
                "html_url": "https://github.com/acme/app/pull/7",
            }
        )
        record = webhooks.handle_event("pull_request", payload)
        assert record["event"] == "pull_request"
        assert record["pr_number"] == 7
        assert "opened PR #7" in record["summary"]

    def test_issues(self):
        payload = self._payload(
            issue={"number": 3, "title": "Crash on empty input", "html_url": "https://github.com/acme/app/issues/3"}
        )
        record = webhooks.handle_event("issues", payload)
        assert record["event"] == "issues"
        assert record["issue_number"] == 3

    def test_unknown_event_does_not_crash(self):
        record = webhooks.handle_event("star", {"sender": {"login": "carol"}})
        assert record["event"] == "star"
