"""Tests for the SQLite-backed store."""

import os

from utils.store import Store


class TestStore:
    def _store(self, tmp_path):
        return Store(db_path=os.path.join(str(tmp_path), "sub", "gitpulse.db"))

    def test_saves_and_lists_analyses(self, tmp_path):
        store = self._store(tmp_path)
        store.save_analysis("code", "src/app.py", {"severity": "high", "problem": "leak"}, author="alice")

        records = store.list_analyses()

        assert len(records) == 1
        assert records[0]["kind"] == "code"
        assert records[0]["target"] == "src/app.py"
        assert records[0]["result"]["problem"] == "leak"

    def test_saves_and_lists_fix_attempts(self, tmp_path):
        store = self._store(tmp_path)
        store.save_fix_attempt(status="created", branch="ai-fix/x", pr_url="https://github.com/x", path="f.py")

        records = store.list_fix_attempts()

        assert len(records) == 1
        assert records[0]["status"] == "created"
        assert records[0]["branch"] == "ai-fix/x"

    def test_saves_and_lists_webhook_events(self, tmp_path):
        store = self._store(tmp_path)
        store.save_webhook_event("push", "pushed", "alice", "acme/app", {"ref": "refs/heads/main"})

        records = store.list_webhook_events()

        assert len(records) == 1
        assert records[0]["event"] == "push"
        assert records[0]["payload_json"]  # json-serialized payload kept

    def test_degrades_gracefully_on_bad_path(self, tmp_path):
        store = Store(db_path="Z:/definitely/not/a/real/path.db")
        assert store._enabled is False
        # Must not raise.
        store.save_analysis("code", "x", {})
        assert store.list_analyses() == []
