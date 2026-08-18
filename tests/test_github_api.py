"""Tests for the GitHub API wrapper."""

import pytest

from utils.github_api import GitHubAPI, GitHubError, compute_activity_score


def _repo_payload(owner="o", repo="r"):
    """A realistic /repos/{owner}/{repo} response."""
    return {
        "full_name": f"{owner}/{repo}",
        "description": "",
        "stargazers_count": 0,
        "forks_count": 0,
        "open_issues_count": 0,
        "default_branch": "main",
        "owner": {"login": owner, "avatar_url": f"https://avatar/{owner}", "html_url": f"https://github.com/{owner}"},
    }


def _report_mock(path_responses: dict[str, object]):
    """
    Build a _request fake that returns the first matching payload, where
    matches are checked most-specific-first so subpaths do not shadow lists.
    """

    def fake_request(method, path, params=None, retries=3):
        ordered = [
            ("requested_reviewers", "/pulls/1/requested_reviewers"),
            ("reviews", "/pulls/1/reviews"),
            ("single_pull", "/pulls/1"),
            ("pulls", "/pulls"),
            ("collaborators", "/collaborators"),
            ("contributors", "/contributors"),
            ("commits", "/commits"),
            ("issues", "/issues"),
            ("languages", "/languages"),
        ]
        for key, marker in ordered:
            if marker in path and key in path_responses:
                return path_responses[key]
        if "repo_default" in path_responses:
            return path_responses["repo_default"]
        return _repo_payload()

    return fake_request


class TestActivityScore:
    def test_minimum(self):
        member = {"commits": 0, "pr_count": 0, "issue_count": 0, "last_active_days": None}
        assert compute_activity_score(member) == 0

    def test_maximum_is_capped(self):
        member = {"commits": 500, "pr_count": 99, "issue_count": 99, "last_active_days": 1}
        assert compute_activity_score(member) == 100

    def test_partial_weights(self):
        member = {"commits": 10, "pr_count": 2, "issue_count": 1, "last_active_days": 10}
        assert compute_activity_score(member) == 30

    def test_recency_tier(self):
        assert compute_activity_score({"commits": 0, "pr_count": 0, "issue_count": 0, "last_active_days": 10}) == 5
        assert compute_activity_score({"commits": 0, "pr_count": 0, "issue_count": 0, "last_active_days": 30}) == 0


class TestPagination:
    def test_iter_pages_stops_on_short_page(self, monkeypatch):
        api = GitHubAPI("t")
        requested_pages = []

        def fake_request(method, path, params=None, retries=3):
            page = (params or {}).get("page", 1)
            requested_pages.append(page)
            size = 10 if page < 2 else 1
            return [{"page": page}] * size

        monkeypatch.setattr(api, "_request", fake_request)

        items = [item for page in api._iter_pages("/x", per_page=10) for item in page]

        assert len(items) == 11
        assert requested_pages == [1, 2]


class TestUserRepos:
    def test_get_user_repos_normalizes_fields(self, monkeypatch):
        api = GitHubAPI("t")

        def fake_request(method, path, params=None, retries=3):
            assert path == "/user/repos"
            assert (params or {}).get("affiliation") == "owner,collaborator,organization_member"
            return [
                {
                    "full_name": "acme/app",
                    "name": "app",
                    "owner": {"login": "acme"},
                    "private": True,
                    "default_branch": "develop",
                    "description": "demo",
                }
            ]

        monkeypatch.setattr(api, "_request", fake_request)

        repos = api.get_user_repos()

        assert repos == [
            {
                "full_name": "acme/app",
                "name": "app",
                "owner": "acme",
                "private": True,
                "default_branch": "develop",
                "description": "demo",
            }
        ]

    def test_get_user_repos_propagates_access_denied(self, monkeypatch):
        api = GitHubAPI("t")

        def boom(method, path, params=None, retries=3):
            raise GitHubError("403 Forbidden", status_code=403)

        monkeypatch.setattr(api, "_request", boom)

        with pytest.raises(GitHubError) as exc:
            api.get_user_repos()

        assert exc.value.status_code == 403


class TestBuildTeamReport:
    def test_commits_fetched_exactly_once(self, monkeypatch):
        api = GitHubAPI("t")
        seen_paths = []

        def fake_request(method, path, params=None, retries=3):
            seen_paths.append(path)
            if "collaborators" in path:
                return [
                    {"login": "alice", "role_name": "write",
                     "permissions": {"admin": False, "maintain": False, "push": True, "triage": False, "pull": True}},
                    {"login": "bob", "role_name": "read",
                     "permissions": {"admin": False, "maintain": False, "push": False, "triage": False, "pull": True}},
                ]
            if "contributors" in path:
                return [{"login": "alice"}, {"login": "bob"}]
            if "commits" in path:
                return [{"author": {"login": "alice"}, "commit": {"author": {"date": "2024-01-01T00:00:00Z"}}}]
            if "pulls" in path:
                return []
            if "issues" in path:
                return []
            if "languages" in path:
                return {"Python": 100}
            return {
                "full_name": "o/r",
                "description": "",
                "stargazers_count": 0,
                "forks_count": 0,
                "open_issues_count": 0,
                "default_branch": "main",
            }

        monkeypatch.setattr(api, "_request", fake_request)

        report = api.build_team_report("o", "r")

        assert seen_paths.count("/repos/o/r/commits") == 1
        alice = report["members"][0]
        assert alice["username"] == "alice"
        assert alice["commits"] == 1
        assert alice["last_active"] == "2024-01-01T00:00:00+00:00"
        assert report["overview"]["total_commits"] == 1
        assert report["languages"] == {"Python": 100}

    def test_handles_fractional_second_dates(self, monkeypatch):
        api = GitHubAPI("t")

        def fake_request(method, path, params=None, retries=3):
            if "collaborators" in path:
                return [{"login": "alice", "role_name": "write",
                         "permissions": {"admin": False, "maintain": False, "push": True, "triage": False, "pull": True}}]
            if "contributors" in path:
                return [{"login": "alice"}]
            if "commits" in path:
                return [{"author": {"login": "alice"}, "commit": {"author": {"date": "2024-01-01T00:00:00.123456Z"}}}]
            if "pulls" in path or "issues" in path:
                return []
            if "languages" in path:
                return {}
            return {"full_name": "o/r", "description": "", "stargazers_count": 0, "forks_count": 0, "open_issues_count": 0, "default_branch": "main"}

        monkeypatch.setattr(api, "_request", fake_request)

        report = api.build_team_report("o", "r")

        assert report["members"][0]["last_active"] == "2024-01-01T00:00:00.123456+00:00"
        assert report["members"][0]["last_active_days"] >= 0


class TestCollaboratorHelpers:
    """Tests for the raw/normalized collaborator and invitation fetchers."""

    def test_get_collaborators_returns_raw_paginated_items(self, monkeypatch):
        api = GitHubAPI("t")
        requested_pages = []

        def fake_request(method, path, params=None, retries=3):
            assert path == "/repos/o/r/collaborators"
            page = (params or {}).get("page", 1)
            requested_pages.append(page)
            if page == 1:
                return [{"login": f"user{i}"} for i in range(10)]
            return [{"login": "bob"}]

        monkeypatch.setattr(api, "_request", fake_request)

        collabs = api.get_collaborators("o", "r", per_page=10)

        assert len(collabs) == 11
        assert requested_pages == [1, 2]
        assert collabs[0]["login"] == "user0"
        assert collabs[10]["login"] == "bob"

    def test_get_collaborators_forwards_affiliation_filter(self, monkeypatch):
        api = GitHubAPI("t")
        seen = {}

        def fake_request(method, path, params=None, retries=3):
            seen.update(params or {})
            return []

        monkeypatch.setattr(api, "_request", fake_request)

        api.get_collaborators("o", "r", affiliation="outside")
        assert seen.get("affiliation") == "outside"

    def test_get_repository_collaborators_paginates_and_normalizes(self, monkeypatch):
        api = GitHubAPI("t")
        requested_pages = []

        def fake_request(method, path, params=None, retries=3):
            assert path == "/repos/o/r/collaborators"
            page = (params or {}).get("page", 1)
            requested_pages.append(page)
            if page == 1:
                return [
                    {
                        "login": "alice",
                        "avatar_url": "https://x/alice.png",
                        "html_url": "https://github.com/alice",
                        "role_name": "admin",
                        "permissions": {"admin": True, "maintain": False, "push": True, "triage": False, "pull": True},
                    }
                    for _ in range(10)
                ]
            return [
                {
                    "login": "bob",
                    "avatar_url": "",
                    "html_url": "",
                    "role_name": "read",
                    "permissions": {"admin": False, "maintain": False, "push": False, "triage": False, "pull": True},
                }
            ]

        monkeypatch.setattr(api, "_request", fake_request)

        collabs = api.get_repository_collaborators("o", "r", per_page=10)

        assert len(collabs) == 11
        assert requested_pages == [1, 2]
        assert collabs[0]["username"] == "alice"
        assert collabs[0]["avatar"] == "https://x/alice.png"
        assert collabs[0]["role"] == "admin"
        assert collabs[0]["permissions"]["push"] is True
        assert collabs[0]["pending"] is False
        assert collabs[10]["username"] == "bob"
        assert collabs[10]["role"] == "read"

    def test_get_repository_collaborators_derives_role_from_permissions(self, monkeypatch):
        api = GitHubAPI("t")

        def fake_request(method, path, params=None, retries=3):
            return [
                {
                    "login": "carol",
                    "avatar_url": "",
                    "html_url": "",
                    "role_name": "",
                    "permissions": {"admin": False, "maintain": False, "push": True, "triage": False, "pull": True},
                }
            ]

        monkeypatch.setattr(api, "_request", fake_request)

        collabs = api.get_repository_collaborators("o", "r")
        assert collabs[0]["role"] == "write"

    def test_get_pending_invitations_normalizes(self, monkeypatch):
        api = GitHubAPI("t")

        def fake_request(method, path, params=None, retries=3):
            assert path == "/repos/o/r/invitations"
            return [
                {
                    "invitee": {
                        "login": "charlie",
                        "avatar_url": "https://x/charlie.png",
                        "html_url": "https://github.com/charlie",
                    },
                    "permissions": "write",
                },
                {
                    "invitee": {
                        "login": "dave",
                        "avatar_url": "",
                        "html_url": "",
                    },
                    "permissions": "read",
                },
            ]

        monkeypatch.setattr(api, "_request", fake_request)

        invites = api.get_pending_invitations("o", "r")

        assert len(invites) == 2
        charlie = invites[0]
        assert charlie["username"] == "charlie"
        assert charlie["avatar"] == "https://x/charlie.png"
        assert charlie["url"] == "https://github.com/charlie"
        assert charlie["role"] == "pending write"
        assert charlie["permissions"]["push"] is True
        assert charlie["pending"] is True
        assert invites[1]["role"] == "pending read"
        assert invites[1]["permissions"]["pull"] is True

    def test_get_pending_invitations_propagates_denied(self, monkeypatch):
        api = GitHubAPI("t")

        def boom(method, path, params=None, retries=3):
            raise GitHubError("403 Forbidden", status_code=403)

        monkeypatch.setattr(api, "_request", boom)

        with pytest.raises(GitHubError) as exc:
            api.get_pending_invitations("o", "r")

        assert exc.value.status_code == 403

    def test_permissions_from_role_maps_all_roles(self):
        assert GitHubAPI._permissions_from_role("admin") == {
            "admin": True, "maintain": False, "push": True, "triage": False, "pull": True,
        }
        assert GitHubAPI._permissions_from_role("maintain")["maintain"] is True
        assert GitHubAPI._permissions_from_role("write")["push"] is True
        assert GitHubAPI._permissions_from_role("triage")["triage"] is True
        assert GitHubAPI._permissions_from_role("read")["pull"] is True
        assert GitHubAPI._permissions_from_role("")["pull"] is True

    def test_build_team_report_includes_collaborator_without_commits(self, monkeypatch):
        """A collaborator who never committed must still appear as a member."""
        api = GitHubAPI("t")

        def fake_request(method, path, params=None, retries=3):
            if "collaborators" in path:
                return [
                    {"login": "alice", "role_name": "write",
                     "permissions": {"admin": False, "maintain": False, "push": True, "triage": False, "pull": True}},
                    {"login": "bob", "role_name": "read",
                     "permissions": {"admin": False, "maintain": False, "push": False, "triage": False, "pull": True}},
                ]
            if "commits" in path:
                return [{"author": {"login": "alice"}, "commit": {"author": {"date": "2024-01-01T00:00:00Z"}}}]
            if "pulls" in path or "issues" in path:
                return []
            if "languages" in path:
                return {"Python": 100}
            return {"full_name": "o/r", "description": "", "stargazers_count": 0, "forks_count": 0, "open_issues_count": 0, "default_branch": "main"}

        monkeypatch.setattr(api, "_request", fake_request)

        report = api.build_team_report("o", "r")

        usernames = {m["username"] for m in report["members"]}
        assert usernames == {"alice", "bob"}
        bob = next(m for m in report["members"] if m["username"] == "bob")
        assert bob["role"] == "collaborator"
        assert bob["permission"] == "read"
        assert bob["permissions"]["pull"] is True
        assert report["overview"]["members"] == 2

class TestCollaboratorMembers:
    """The team must come from /collaborators, not just /contributors."""

    _COLLABORATORS = [
        {
            "login": "mj-viro",
            "avatar_url": "https://avatar/mj-viro",
            "html_url": "https://github.com/mj-viro",
            "permissions": {"admin": True, "push": True, "pull": True},
        },
        {
            "login": "sivasurya10247",
            "avatar_url": "https://avatar/sivasurya10247",
            "html_url": "https://github.com/sivasurya10247",
            "permissions": {"admin": False, "push": True, "pull": True},
        },
        {
            "login": "wilfrit0212005-glitch",
            "avatar_url": "https://avatar/wilfrit",
            "html_url": "https://github.com/wilfrit0212005-glitch",
            "permissions": {"admin": False, "push": False, "pull": True},
        },
        {
            "login": "yogu-crypto",
            "avatar_url": "https://avatar/yogu-crypto",
            "html_url": "https://github.com/yogu-crypto",
            "permissions": {"admin": False, "push": True, "pull": True},
        },
    ]

    def _build(self, api, extra_responses=None):
        path_responses = {
            "repo_default": _repo_payload(owner="mj-viro", repo="team-repo"),
            "collaborators": self._COLLABORATORS,
            "contributors": [],
            "commits": [],
            "pulls": [],
            "issues": [],
            "languages": {},
        }
        path_responses.update(extra_responses or {})
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(api, "_request", _report_mock(path_responses))
        return monkeypatch

    def test_all_collaborators_become_members(self, monkeypatch):
        api = GitHubAPI("t")
        self._build(api)
        report = api.build_team_report("mj-viro", "team-repo")

        assert [m["username"] for m in report["members"]] == [
            "mj-viro", "sivasurya10247", "wilfrit0212005-glitch", "yogu-crypto",
        ]
        assert report["overview"]["members"] == 4
        assert report["overview"]["active_members"] + report["overview"]["inactive_members"] == 4

        owner = next(m for m in report["members"] if m["username"] == "mj-viro")
        assert owner["role"] == "owner"
        assert owner["permission"] == "admin"
        assert owner["is_owner"] is True

        siva = next(m for m in report["members"] if m["username"] == "sivasurya10247")
        assert siva["role"] == "collaborator"
        assert siva["permission"] == "write"

        read_only = next(m for m in report["members"] if m["username"] == "wilfrit0212005-glitch")
        assert read_only["permission"] == "read"

    def test_commit_authors_are_matched_to_the_right_member(self, monkeypatch):
        api = GitHubAPI("t")
        self._build(
            api,
            extra_responses={
                "commits": [
                    {"sha": "a", "author": {"login": "sivasurya10247"}, "html_url": "u",
                     "commit": {"author": {"date": "2026-08-10T00:00:00Z"}, "message": "update dashboard"}},
                    {"sha": "b", "author": {"login": "mj-viro"}, "html_url": "u",
                     "commit": {"author": {"date": "2026-08-09T00:00:00Z"}, "message": "fix login API"}},
                    {"sha": "c", "author": {"login": "yogu-crypto"}, "html_url": "u",
                     "commit": {"author": {"date": "2026-08-08T00:00:00Z"}, "message": "add tests"}},
                    {"sha": "d", "author": None, "html_url": "u",
                     "commit": {"author": {"date": "2026-08-07T00:00:00Z"}, "message": "unattributed commit"}},
                    {"sha": "e", "author": {"login": "sivasurya10247"}, "html_url": "u",
                     "commit": {"author": {"date": "2026-08-06T00:00:00Z"}, "message": "another commit"}},
                ]
            },
        )
        report = api.build_team_report("mj-viro", "team-repo")

        by_name = {m["username"]: m for m in report["members"]}
        # Commits must follow the real author, never all going to the owner.
        assert by_name["mj-viro"]["commits"] == 1
        assert by_name["sivasurya10247"]["commits"] == 2
        assert by_name["yogu-crypto"]["commits"] == 1
        assert by_name["wilfrit0212005-glitch"]["commits"] == 0
        # The null-author commit still counts toward the repo total.
        assert report["overview"]["total_commits"] == 5

    def test_owner_added_even_when_collaborators_endpoint_omits_them(self, monkeypatch):
        api = GitHubAPI("t")
        collaborators_without_owner = [
            c for c in self._COLLABORATORS if c["login"] != "mj-viro"
        ]
        self._build(api, extra_responses={"collaborators": collaborators_without_owner})
        report = api.build_team_report("mj-viro", "team-repo")

        usernames = [m["username"] for m in report["members"]]
        assert "mj-viro" in usernames
        assert report["overview"]["members"] == 4
        owner = next(m for m in report["members"] if m["username"] == "mj-viro")
        assert owner["role"] == "owner"
        assert owner["permission"] == "admin"

    def test_collaborators_endpoint_failure_falls_back_to_contributors(self, monkeypatch):
        api = GitHubAPI("t")

        def fake_request(method, path, params=None, retries=3):
            if "collaborators" in path:
                raise GitHubError("403 Forbidden", status_code=403)
            if "contributors" in path:
                return [{"login": "alice", "contributions": 7}]
            if "commits" in path or "pulls" in path or "issues" in path:
                return []
            if "languages" in path:
                return {}
            return _repo_payload()

        monkeypatch.setattr(api, "_request", fake_request)
        report = api.build_team_report("o", "r")

        usernames = [m["username"] for m in report["members"]]
        # Contributors are used, and the owner is always guaranteed a seat.
        assert "alice" in usernames
        assert "o" in usernames
        assert report["overview"]["members"] == 2
        alice = next(m for m in report["members"] if m["username"] == "alice")
        assert alice["role"] == "contributor"
        assert alice["commits_all_time"] == 7

    def test_build_team_report_merges_pending_invitations_and_owner(self, monkeypatch):
        """Pending invitees and the repo owner must appear as members even if
        the collaborators endpoint only returns accepted collaborators."""
        api = GitHubAPI("t")

        def fake_request(method, path, params=None, retries=3):
            if "invitations" in path:
                return [
                    {
                        "invitee": {"login": "charlie", "avatar_url": "", "html_url": ""},
                        "permissions": "read",
                    }
                ]
            if "collaborators" in path:
                return [
                    {"login": "alice", "role_name": "write",
                     "permissions": {"admin": False, "maintain": False, "push": True, "triage": False, "pull": True}},
                ]
            if "contributors" in path:
                return [{"login": "alice"}]
            if "commits" in path:
                return [{"author": {"login": "owneruser"}, "commit": {"author": {"date": "2024-01-01T00:00:00Z"}}}]
            if "pulls" in path or "issues" in path:
                return []
            if "languages" in path:
                return {}
            return {
                "full_name": "o/r",
                "description": "",
                "stargazers_count": 0,
                "forks_count": 0,
                "open_issues_count": 0,
                "default_branch": "main",
                "owner": {"login": "owneruser"},
            }

        monkeypatch.setattr(api, "_request", fake_request)

        report = api.build_team_report("o", "r")

        usernames = [m["username"] for m in report["members"]]
        assert set(usernames) == {"alice", "charlie", "owneruser"}
        charlie = next(m for m in report["members"] if m["username"] == "charlie")
        assert charlie["pending"] is True
        assert charlie["role"] == "pending read"
        assert charlie["permissions"]["pull"] is True
        owner = next(m for m in report["members"] if m["username"] == "owneruser")
        assert owner["role"] == "owner"
        assert owner["permission"] == "admin"
        assert owner["permissions"]["admin"] is True
        assert owner["pending"] is False
        assert owner["commits"] == 1
        assert report["overview"]["members"] == 3
        assert usernames.count("owneruser") == 1

    def test_activity_feed_contains_real_events_from_all_members(self, monkeypatch):
        api = GitHubAPI("t")
        self._build(
            api,
            extra_responses={
                "commits": [
                    {"sha": "abc123", "author": {"login": "sivasurya10247"}, "html_url": "u",
                     "commit": {"author": {"date": "2026-08-10T12:00:00Z"}, "message": "update dashboard"}},
                ],
                "pulls": [
                    {"number": 1, "title": "Fix login API", "user": {"login": "wilfrit0212005-glitch"},
                     "state": "open", "merged_at": None, "created_at": "2026-08-05T10:00:00Z",
                     "updated_at": "2026-08-05T10:00:00Z", "html_url": "https://github.com/pr/1"},
                ],
                "single_pull": {"additions": 5, "deletions": 2, "changed_files": 1},
                "reviews": [],
                "requested_reviewers": {"users": []},
                "issues": [
                    {"number": 2, "title": "Crash on empty input", "user": {"login": "yogu-crypto"},
                     "state": "open", "created_at": "2026-08-03T09:00:00Z",
                     "updated_at": "2026-08-03T09:00:00Z", "html_url": "https://github.com/issue/2"},
                ],
            },
        )
        report = api.build_team_report("mj-viro", "team-repo")

        feed = report["activity_feed"]
        assert len(feed) == 3
        by_type = {item["type"]: item for item in feed}
        assert by_type["push"]["author"] == "sivasurya10247"
        assert by_type["push"]["action"] == "pushed commit"
        assert by_type["pull_request"]["author"] == "wilfrit0212005-glitch"
        assert by_type["pull_request"]["action"] == "opened pull request"
        assert by_type["issue"]["author"] == "yogu-crypto"
        assert by_type["issue"]["action"] == "opened issue"
        # Newest first.
        assert feed[0]["type"] == "push"

    def test_member_with_no_activity_is_inactive(self, monkeypatch):
        from datetime import datetime, timedelta, timezone

        api = GitHubAPI("t")
        two_days_ago = (
            datetime.now(timezone.utc) - timedelta(days=2)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._build(
            api,
            extra_responses={
                "commits": [
                    {"sha": "a", "author": {"login": "mj-viro"}, "html_url": "u",
                     "commit": {"author": {"date": two_days_ago}, "message": "recent commit"}},
                ]
            },
        )
        report = api.build_team_report("mj-viro", "team-repo")

        by_name = {m["username"]: m for m in report["members"]}
        assert by_name["mj-viro"]["is_active"] is True
        assert by_name["mj-viro"]["last_active_text"] == "2 days ago"
        assert by_name["wilfrit0212005-glitch"]["is_active"] is False
        assert by_name["wilfrit0212005-glitch"]["last_active_text"] == "No activity"
        assert report["overview"]["active_members"] == 1
        assert report["overview"]["inactive_members"] == 3


class TestTokenValidation:
    def test_maps_api_failure_to_friendly_error(self, monkeypatch):
        api = GitHubAPI("t")

        def boom(method, path, params=None, retries=3):
            raise GitHubError("401 Unauthorized", status_code=401)

        monkeypatch.setattr(api, "_request", boom)

        with pytest.raises(GitHubError) as exc:
            api.validate_token()

        assert "invalid or has expired" in str(exc.value)

    def test_reports_missing_scope_for_403(self, monkeypatch):
        api = GitHubAPI("t")

        def boom(method, path, params=None, retries=3):
            raise GitHubError("403 Forbidden", status_code=403)

        monkeypatch.setattr(api, "_request", boom)

        with pytest.raises(GitHubError) as exc:
            api.validate_token()

        assert "scope" in str(exc.value)

    def test_reports_network_failure_as_network_error(self, monkeypatch):
        api = GitHubAPI("t")

        def boom(method, path, params=None, retries=3):
            raise GitHubError("Network error reaching GitHub: timeout", status_code=None)

        monkeypatch.setattr(api, "_request", boom)

        with pytest.raises(GitHubError) as exc:
            api.validate_token()

        assert "Could not reach GitHub" in str(exc.value)
        assert "invalid" not in str(exc.value)
