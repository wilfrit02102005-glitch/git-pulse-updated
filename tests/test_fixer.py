"""Tests for the safe AI auto-fix workflow safety rules."""

from utils import fixer


class TestFixBranchName:
    def test_slugifies(self):
        name = fixer.build_fix_branch_name("Fix: Null Pointer in parse()")
        assert name.startswith("ai-fix/fix-null-pointer-in-parse-")
        assert name.endswith("0") or name[-1].isdigit()

    def test_slug_falls_back(self):
        assert fixer.build_fix_branch_name("!!!").startswith("ai-fix/fix-")


class TestSlugify:
    def test_strips_punctuation(self):
        assert fixer._slugify("Hello, World!") == "hello-world"

    def test_empty_returns_fix(self):
        assert fixer._slugify("...") == "fix"


class _FakeAPI:
    def __init__(self):
        self.created_branches = []
        self.committed = []
        self.prs = []

    def get_default_branch(self, owner, repo):
        return "main"

    def get_branch_sha(self, owner, repo, branch):
        return "base-sha"

    def create_branch(self, owner, repo, branch, sha):
        self.created_branches.append(branch)

    def commit_file_via_api(self, owner, repo, branch, path, content, message):
        self.committed.append((branch, path))

    def create_pull_request(self, owner, repo, title, head, base, body):
        self.prs.append((head, base))
        return {"html_url": "https://github.com/acme/app/pull/1", "number": 1}


class TestCreateFixPullRequest:
    def test_never_merges_and_never_touches_default_branch(self, monkeypatch):
        api = _FakeAPI()
        analysis = {"explanation": "null deref", "problem": "crash", "suggested_fix": "add guard"}

        result = fixer.create_fix_pull_request(
            api, "acme", "app", "src/parse.py", "Null pointer", analysis, "fixed content"
        )

        assert result["status"] == "created"
        assert result["pr_url"].startswith("https://github.com/")
        # The PR head is a feature branch, base stays the default branch.
        assert api.prs[0][1] == "main"
        assert api.prs[0][0].startswith("ai-fix/")
        assert not any(b == "main" for b in api.created_branches)
        assert not any(b == "main" for b, _ in api.committed)

    def test_stops_when_validation_fails(self, monkeypatch):
        monkeypatch.setattr(
            fixer.settings,
            "AI_FIX_LOCAL_VALIDATION",
            1,
        )

        def fail_validate(*args, **kwargs):
            return {"ok": False, "skipped": False, "detail": "Validation failed - see command output."}

        monkeypatch.setattr(fixer, "validate_locally", fail_validate)
        api = _FakeAPI()

        result = fixer.create_fix_pull_request(
            api, "acme", "app", "src/parse.py", "Null pointer", {}, "fixed content"
        )

        assert result["status"] == "validation_failed"
        # No PR created because validation failed.
        assert api.prs == []

    def test_records_error_when_default_branch_read_fails(self, monkeypatch):
        class BrokenAPI(_FakeAPI):
            def get_default_branch(self, owner, repo):
                raise RuntimeError("no repo")

        result = fixer.create_fix_pull_request(
            BrokenAPI(), "acme", "app", "src/parse.py", "Null pointer", {}, "content"
        )
        assert result["status"] == "error"
        assert "default branch" in result["error"]


class TestValidateLocally:
    def test_skipped_when_disabled(self, monkeypatch):
        monkeypatch.setattr(fixer.settings, "AI_FIX_LOCAL_VALIDATION", 0)
        result = fixer.validate_locally("acme", "app", "ai-fix/x", "f.py", "content")
        assert result["ok"] is True
        assert result["skipped"] is True

    def test_skipped_when_no_git(self, monkeypatch):
        monkeypatch.setattr(fixer.settings, "AI_FIX_LOCAL_VALIDATION", 1)
        monkeypatch.setattr(fixer.shutil, "which", lambda _: None)
        result = fixer.validate_locally("acme", "app", "ai-fix/x", "f.py", "content")
        assert result["skipped"] is True
        assert "git" in result["detail"]
