"""
GitPulse - GitHub API wrapper.

A thin, resilient client around the GitHub REST API. It handles:

* Token validation before any request is made.
* Automatic pagination (GitHub caps pages at 100 items).
* Rate-limit detection with a short backoff.
* Clean exception mapping so routes never see raw `requests` errors.

All heavy lifting for the dashboard is provided here: team members,
commits, pull requests, issues, languages and activity scoring.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Optional

import requests

# Import the shared logger. `get_logger` safely returns the "github" logger.
from config.logging_setup import get_logger
from config.settings import settings

logger = get_logger("github")

API_BASE = "https://api.github.com"
# Number of seconds to wait before retrying when the rate limit is hit.
RATE_LIMIT_BACKOFF = 5
# How many times a failing request is retried before giving up.
MAX_RETRIES = 3

# ----------------------------------------------------------------------
# Process-wide response cache.
# A fresh GitHubAPI is built on every request, so per-instance caching
# would never survive a page load. This module-level cache keys on a
# short hash of the token + endpoint + params and keeps the dashboard
# from hammering the GitHub API on every load. The token value itself is
# never stored or logged - only a hash is used as part of the key.
# ----------------------------------------------------------------------
HTTP_CACHE_TTL = 60  # seconds
HTTP_CACHE_MAX_ENTRIES = 2048
_HTTP_CACHE: dict[tuple, tuple[float, object]] = {}

# How many recent commits get per-commit file/stat details fetched.
COMMIT_DETAIL_LIMIT = 50

# Cap on how many issues the Issues page will fetch for one repository.
# Guards against pathological repos while still covering normal usage.
MAX_ISSUES_PER_REPO = 1000


def clear_http_cache() -> None:
    """Drop every cached GitHub response (used by the Refresh action)."""
    _HTTP_CACHE.clear()


class GitHubError(Exception):
    """Raised for any GitHub API failure with a human-readable message."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class GitHubAPI:
    """Authenticated GitHub REST API client."""

    def __init__(self, token: str) -> None:
        self.token = token.strip()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "GitPulse-Team-Intelligence",
            }
        )
        # Short, non-reversible cache key derived from the token. Only the
        # hash is used in memory as part of a cache key; the token itself
        # is never logged or exposed.
        self._cache_id = hashlib.sha256(self.token.encode("utf-8")).hexdigest()[:16]

    def clear_cache(self) -> None:
        """Clear the process-wide HTTP response cache for this token."""
        clear_http_cache()

    # ------------------------------------------------------------------
    # Low-level request helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _friendly_status(status_code: int, path: str, detail: str) -> str:
        """
        Map a GitHub API failure to a clear, human-readable message.

        Never includes the token. `detail` is the raw response body
        (GitHub error payloads do not echo tokens).
        """
        if status_code == 401:
            return (
                "Your GitHub token is invalid or has expired. Create a new one at "
                "github.com/settings/tokens and try again."
            )
        if status_code == 403:
            return (
                "GitHub denied access (HTTP 403). Your token may lack the required "
                "'repo' scope, or you have hit the API rate limit. Check your token "
                "permissions at github.com/settings/tokens."
            )
        if status_code == 404:
            return (
                f"Repository or owner not found (HTTP 404 on {path}). Check "
                "that the repository exists and that your token has access "
                "to it."
            )
        return f"GitHub {status_code} on {path}: {detail[:200]}"

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        retries: int = MAX_RETRIES,
        json: Optional[dict] = None,
    ) -> dict:
        """
        Perform one authenticated request with retry + rate-limit handling.

        Returns the JSON body as a dict (or empty dict for 204 responses).
        Client errors (4xx) are raised immediately with a friendly message;
        only server errors (5xx) and network failures are retried.
        """
        url = f"{API_BASE}{path}"
        params = params or {}
        last_error: Optional[GitHubError] = None

        # Read-through cache for idempotent GET requests.
        cache_key: Optional[tuple] = None
        if method.upper() == "GET" and json is None:
            cache_key = (self._cache_id, path, tuple(sorted(params.items())))
            hit = _HTTP_CACHE.get(cache_key)
            if hit and (time.time() - hit[0]) < HTTP_CACHE_TTL:
                return hit[1]

        for attempt in range(retries):
            try:
                resp = self.session.request(
                    method, url, params=params, json=json, timeout=30
                )
            except requests.RequestException as exc:  # network failure
                logger.warning("Network error on %s: %s (attempt %d)", path, exc, attempt + 1)
                last_error = GitHubError(f"Network error reaching GitHub: {exc}")
                time.sleep(RATE_LIMIT_BACKOFF)
                continue

            # --- Rate limit exceeded: wait and retry ---
            if resp.status_code == 403 and "rate limit" in resp.text.lower():
                reset = resp.headers.get("X-RateLimit-Reset")
                wait = RATE_LIMIT_BACKOFF
                if reset:
                    wait = max(int(reset) - int(time.time()), 0) + 1
                logger.warning("GitHub rate limit hit, sleeping %ds", wait)
                time.sleep(wait)
                continue

            # --- Success ---
            if resp.status_code == 204:
                if cache_key is not None:
                    _HTTP_CACHE[cache_key] = (time.time(), {})
                return {}
            if 200 <= resp.status_code < 300:
                try:
                    result = resp.json()
                except ValueError as exc:
                    raise GitHubError(
                        f"GitHub returned an invalid JSON response for {path}.",
                        status_code=502,
                    ) from exc
                if cache_key is not None:
                    now = time.time()
                    for key, (created, _) in list(_HTTP_CACHE.items()):
                        if now - created >= HTTP_CACHE_TTL:
                            _HTTP_CACHE.pop(key, None)
                    if len(_HTTP_CACHE) >= HTTP_CACHE_MAX_ENTRIES:
                        oldest = min(_HTTP_CACHE, key=lambda k: _HTTP_CACHE[k][0])
                        _HTTP_CACHE.pop(oldest, None)
                    _HTTP_CACHE[cache_key] = (now, result)
                return result

            # --- Client errors (4xx): report immediately, never retry ---
            if 400 <= resp.status_code < 500:
                raise GitHubError(
                    self._friendly_status(resp.status_code, path, resp.text),
                    status_code=resp.status_code,
                )

            # --- Server errors (5xx): retry with backoff ---
            last_error = GitHubError(
                f"GitHub {resp.status_code} on {path}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
            time.sleep(RATE_LIMIT_BACKOFF)

        raise last_error or GitHubError(f"Request to {path} failed after retries")

    def _iter_pages(
        self,
        path: str,
        params: Optional[dict] = None,
        per_page: int = 100,
    ) -> Iterator[list[dict]]:
        """
        Yield paginated results as lists of items.

        GitHub caps `per_page` at 100, so larger collections are fetched
        page by page until an empty page is returned.
        """
        page = 1
        while True:
            data = self._request(
                "GET", path, params={**(params or {}), "page": page, "per_page": per_page}
            )
            if not isinstance(data, list):
                break
            yield data
            if len(data) < per_page:
                break
            page += 1

    # ------------------------------------------------------------------
    # Authenticated user + accessible repositories
    # ------------------------------------------------------------------
    def get_authenticated_user(self) -> dict:
        """Return the profile of the currently authenticated user (/user)."""
        return self._request("GET", "/user")

    def get_user_repos(
        self,
        affiliation: str = "owner,collaborator,organization_member",
        sort: str = "updated",
        direction: str = "desc",
        per_page: int = 100,
    ) -> list[dict]:
        """
        Return repositories accessible to the authenticated user.

        `affiliation=owner,collaborator,organization_member` covers repos
        the user owns, contributes to as a collaborator, or can access
        through an organization. Results are normalized to just the fields
        the repository selector needs (no secrets or tokens are included).
        """
        out: list[dict] = []
        for page in self._iter_pages(
            "/user/repos",
            params={
                "affiliation": affiliation,
                "sort": sort,
                "direction": direction,
            },
            per_page=per_page,
        ):
            out.extend(page)
        return [
            {
                "full_name": item.get("full_name", ""),
                "name": item.get("name", ""),
                "owner": (item.get("owner") or {}).get("login", ""),
                "private": item.get("private", False),
                "default_branch": item.get("default_branch", "main"),
                "description": item.get("description", ""),
            }
            for item in out
        ]

    # ------------------------------------------------------------------
    # Token validation
    # ------------------------------------------------------------------
    def validate_token(self) -> Optional[dict]:
        """
        Validate the token by calling the authenticated user endpoint.

        Returns the authenticated user dict, or raises GitHubError with a
        precise reason (bad credentials / missing scope / network failure),
        so the login page never blames a valid token for a network problem.
        """
        try:
            return self.get_authenticated_user()
        except GitHubError as exc:
            logger.error("Token validation failed (status=%s): %s", exc.status_code, exc.message)
            if exc.status_code == 401:
                raise GitHubError(
                    "The token is invalid or has expired. Double-check it or "
                    "create a new one (github.com/settings/tokens).",
                    status_code=401,
                ) from exc
            if exc.status_code == 403:
                raise GitHubError(
                    "The token was accepted but GitHub denied access. It may "
                    "lack the required scope (e.g. 'repo').",
                    status_code=403,
                ) from exc
            if exc.status_code is None:
                raise GitHubError(
                    "Could not reach GitHub. Check your network connection "
                    "and try again.",
                ) from exc
            raise GitHubError(
                f"GitHub could not validate the token (HTTP {exc.status_code}).",
                status_code=exc.status_code,
            ) from exc

    # ------------------------------------------------------------------
    # Single-entity lookups
    # ------------------------------------------------------------------
    def get_user(self, username: str) -> dict:
        """Fetch a public user profile by username."""
        return self._request("GET", f"/users/{username}")

    def get_organization(self, org: str) -> dict:
        """Fetch an organization profile (works only for public orgs/token)."""
        return self._request("GET", f"/orgs/{org}")

    def get_repository(self, owner: str, repo: str) -> dict:
        """Fetch a single repository's metadata."""
        return self._request("GET", f"/repos/{owner}/{repo}")

    def get_branches(self, owner: str, repo: str) -> list[dict]:
        """Return all branches of a repository."""
        out: list[dict] = []
        for page in self._iter_pages(f"/repos/{owner}/{repo}/branches"):
            out.extend(page)
        return out

    def get_repository_languages(self, owner: str, repo: str) -> dict[str, int]:
        """Return the language breakdown (bytes per language) of a repo."""
        return self._request("GET", f"/repos/{owner}/{repo}/languages")

    # ------------------------------------------------------------------
    # Team / contributor data
    # ------------------------------------------------------------------
    def get_contributors(self, owner: str, repo: str, per_page: int = 100) -> list[dict]:
        """
        Return contributors for a repository.

        The `anonymous` flag is turned off so we only get named members.
        """
        out: list[dict] = []
        for page in self._iter_pages(
            f"/repos/{owner}/{repo}/contributors",
            params={"anon": "false"},
            per_page=per_page,
        ):
            out.extend(page)
        return out

    def get_collaborators(
        self,
        owner: str,
        repo: str,
        per_page: int = 100,
        affiliation: Optional[str] = None,
    ) -> list[dict]:
        """
        Return every person with access to the repository (paginated).

        GET /repos/{owner}/{repo}/collaborators
        ``affiliation`` filters by ``outside``, ``direct`` or ``all`` (default).
        Each item includes the user's ``permissions`` (admin / push / pull).

        Unlike /contributors (which only lists people with commits) this
        endpoint returns the real collaborators, including people who have
        been granted access but have not pushed anything yet.
        """
        params: dict[str, Any] = {}
        if affiliation:
            params["affiliation"] = affiliation
        out: list[dict] = []
        for page in self._iter_pages(
            f"/repos/{owner}/{repo}/collaborators", params=params, per_page=per_page
        ):
            out.extend(page)
        return out

    def get_repository_collaborators(self, owner: str, repo: str, per_page: int = 100) -> list[dict]:
        """
        Return the repository's actual collaborators via
        GET /repos/{owner}/{repo}/collaborators (paginated).

        Unlike /contributors (users who have committed), this lists every
        user granted access to the repository, including collaborators who
        have never pushed a commit. Each entry is normalized to the fields
        the dashboard needs: username, avatar, url, role and permissions.

        Requires the authenticated token to have push access to the repo;
        callers should fall back to `get_contributors` on GitHubError.

        The GitHub API response status and the number of collaborators found
        are logged for debugging. The token itself is never logged.
        """
        path = f"/repos/{owner}/{repo}/collaborators"
        out: list[dict] = []
        try:
            for page in self._iter_pages(path, per_page=per_page):
                out.extend(page)
            logger.info(
                "Collaborators fetch for %s/%s returned HTTP 200 with %d collaborator(s)",
                owner, repo, len(out),
            )
        except GitHubError as exc:
            logger.warning(
                "Collaborators fetch for %s/%s failed (status=%s): %s",
                owner, repo, exc.status_code or "n/a", exc.message,
            )
            raise
        return [
            {
                "username": item.get("login", "unknown"),
                "avatar": item.get("avatar_url", ""),
                "url": item.get("html_url", ""),
                "role": self._collaborator_role(item),
                "permissions": self._collaborator_permissions(item),
                "pending": False,
            }
            for item in out
        ]

    def get_pending_invitations(self, owner: str, repo: str, per_page: int = 100) -> list[dict]:
        """
        Return pending repository invitations via
        GET /repos/{owner}/{repo}/invitations (paginated).

        Collaborators that were added but have not yet accepted the
        invitation do not appear in /collaborators; they show up here until
        they accept. Merging these into the member list makes newly added
        members visible immediately. Requires the same push/admin access as
        the collaborators endpoint.

        The GitHub API response status and the number of pending invitations
        found are logged for debugging. The token itself is never logged.
        """
        path = f"/repos/{owner}/{repo}/invitations"
        out: list[dict] = []
        try:
            for page in self._iter_pages(path, per_page=per_page):
                out.extend(page)
            logger.info(
                "Invitations fetch for %s/%s returned HTTP 200 with %d pending invitation(s)",
                owner, repo, len(out),
            )
        except GitHubError as exc:
            logger.warning(
                "Invitations fetch for %s/%s failed (status=%s): %s",
                owner, repo, exc.status_code or "n/a", exc.message,
            )
            raise
        return [
            {
                "username": item.get("invitee", {}).get("login", "unknown"),
                "avatar": item.get("invitee", {}).get("avatar_url", ""),
                "url": item.get("invitee", {}).get("html_url", ""),
                "role": f"pending {item.get('permissions', 'read')}",
                "permissions": self._permissions_from_role(item.get("permissions", "read")),
                "pending": True,
            }
            for item in out
        ]

    @staticmethod
    def _permissions_from_role(role: str) -> dict[str, bool]:
        """Map a GitHub role string (admin/write/read/...) to permission flags."""
        flags: dict[str, bool] = {
            "admin": False,
            "maintain": False,
            "push": False,
            "triage": False,
            "pull": False,
        }
        role = (role or "").lower()
        if role == "admin":
            flags.update(admin=True, push=True, pull=True)
        elif role == "maintain":
            flags.update(maintain=True, push=True, pull=True)
        elif role in ("write", "push"):
            flags.update(push=True, pull=True)
        elif role == "triage":
            flags.update(triage=True, pull=True)
        else:  # read / pull
            flags["pull"] = True
        return flags

    @staticmethod
    def _collaborator_permissions(item: dict) -> dict[str, bool]:
        """Extract the boolean permission flags GitHub returns for a collaborator."""
        perms = item.get("permissions") or {}
        return {
            "admin": bool(perms.get("admin")),
            "maintain": bool(perms.get("maintain")),
            "push": bool(perms.get("push")),
            "triage": bool(perms.get("triage")),
            "pull": bool(perms.get("pull")),
        }

    @staticmethod
    def _collaborator_role(item: dict) -> str:
        """Derive a readable role from role_name (preferred) or permissions."""
        role = item.get("role_name") or ""
        if role:
            return role
        perms = item.get("permissions") or {}
        if perms.get("admin"):
            return "admin"
        if perms.get("maintain"):
            return "maintain"
        if perms.get("push"):
            return "write"
        if perms.get("triage"):
            return "triage"
        return "read"

    def get_org_members(self, org: str, per_page: int = 100) -> list[dict]:
        """Return members of an organization (requires membership scope)."""
        out: list[dict] = []
        try:
            for page in self._iter_pages(f"/orgs/{org}/members", per_page=per_page):
                out.extend(page)
        except GitHubError as exc:
            logger.warning("Could not list org members (%s); using contributors.", exc.message)
        return out

    # ------------------------------------------------------------------
    # Activity data
    # ------------------------------------------------------------------
    def get_commits(
        self,
        owner: str,
        repo: str,
        author: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        per_page: int = 100,
    ) -> list[dict]:
        """
        Return commits for a repo, optionally filtered by author and date.

        Args:
            owner:   Repository owner.
            repo:    Repository name.
            author:  GitHub login to filter commits for (optional).
            since:   ISO-8601 date to look back from (optional).
            until:   ISO-8601 upper bound (optional).
        """
        params: dict[str, Any] = {}
        if author:
            params["author"] = author
        if since:
            params["since"] = since
        if until:
            params["until"] = until

        out: list[dict] = []
        for page in self._iter_pages(
            f"/repos/{owner}/{repo}/commits", params=params, per_page=per_page
        ):
            out.extend(page)
        return out

    def get_pull_requests(
        self,
        owner: str,
        repo: str,
        state: str = "open",
        per_page: int = 100,
    ) -> list[dict]:
        """Return pull requests filtered by state ('open' | 'closed' | 'all')."""
        out: list[dict] = []
        for page in self._iter_pages(
            f"/repos/{owner}/{repo}/pulls",
            params={"state": state},
            per_page=per_page,
        ):
            out.extend(page)
        return out

    def get_issues(
        self,
        owner: str,
        repo: str,
        state: str = "open",
        per_page: int = 100,
    ) -> list[dict]:
        """Return issues (pull requests excluded) filtered by state."""
        out: list[dict] = []
        for page in self._iter_pages(
            f"/repos/{owner}/{repo}/issues",
            params={"state": state},
            per_page=per_page,
        ):
            out.extend(page)
        return out

    def list_issues(
        self,
        owner: str,
        repo: str,
        state: str = "all",
        per_page: int = 100,
        limit: int = MAX_ISSUES_PER_REPO,
    ) -> list[dict]:
        """
        Return normalized issues (pull requests excluded) from the selected
        repository.

        Paginates through the GitHub /issues endpoint up to `limit` items so
        the Issues page works for large repositories without loading an
        unbounded number of records into the browser. Each issue is shaped
        into the clean structure the Issues page renders (labels keep their
        GitHub colors so badges can stay readable on the dark theme).
        """
        out: list[dict] = []
        for page in self._iter_pages(
            f"/repos/{owner}/{repo}/issues",
            params={"state": state},
            per_page=per_page,
        ):
            for item in page:
                if item.get("pull_request"):
                    continue
                out.append(
                    {
                        "number": item.get("number", 0),
                        "title": item.get("title", ""),
                        "body": item.get("body") or "",
                        "state": item.get("state", ""),
                        "author": (item.get("user") or {}).get("login", ""),
                        "author_avatar": (item.get("user") or {}).get("avatar_url", ""),
                        "labels": [
                            {
                                "name": label.get("name", ""),
                                "color": (label.get("color") or "6e7781"),
                            }
                            for label in item.get("labels") or []
                        ],
                        "assignees": [
                            (assignee or {}).get("login", "")
                            for assignee in item.get("assignees") or []
                        ],
                        "created_at": item.get("created_at", ""),
                        "updated_at": item.get("updated_at", ""),
                        "closed_at": item.get("closed_at"),
                        "comments_count": item.get("comments", 0),
                        "html_url": item.get("html_url", ""),
                    }
                )
                if len(out) >= limit:
                    return out
        return out

    def get_last_activity(
        self,
        owner: str,
        repo: str,
        since: Optional[str] = None,
        per_page: int = 100,
    ) -> list[dict]:
        """Return events across the repo (pushes, PRs, issues, comments)."""
        since = since or (datetime.now(timezone.utc) - timedelta(days=90)).isoformat() + "Z"
        out: list[dict] = []
        for page in self._iter_pages(
            f"/repos/{owner}/{repo}/events", params={"since": since}, per_page=per_page
        ):
            out.extend(page)
        return out

    def get_repo_events(
        self,
        owner: str,
        repo: str,
        since: Optional[str] = None,
        per_page: int = 100,
    ) -> list[dict]:
        """
        Return the repository's real public event stream.

        GitHub's /events endpoint only returns the most recent 300 events
        (roughly the last 90 days), so `since` is a best-effort filter on
        top of whatever GitHub returns.
        """
        params: dict[str, Any] = {}
        if since:
            params["since"] = since
        out: list[dict] = []
        for page in self._iter_pages(
            f"/repos/{owner}/{repo}/events", params={}, per_page=per_page
        ):
            out.extend(page)
        return out

    def get_issue(self, owner: str, repo: str, number: int) -> dict:
        """Return a single issue by number (the /issues/<n> endpoint)."""
        return self._request("GET", f"/repos/{owner}/{repo}/issues/{number}")

    def get_pr_commits(
        self,
        owner: str,
        repo: str,
        number: int,
        per_page: int = 100,
    ) -> list[dict]:
        """Return the commits included in a pull request."""
        out: list[dict] = []
        for page in self._iter_pages(
            f"/repos/{owner}/{repo}/pulls/{number}/commits", per_page=per_page
        ):
            out.extend(page)
        return out

    def get_pr_comments(
        self,
        owner: str,
        repo: str,
        number: int,
        per_page: int = 100,
    ) -> list[dict]:
        """Return the inline review comments on a pull request."""
        out: list[dict] = []
        for page in self._iter_pages(
            f"/repos/{owner}/{repo}/pulls/{number}/comments", per_page=per_page
        ):
            out.extend(page)
        return out

    def get_issue_comments(
        self,
        owner: str,
        repo: str,
        number: int,
        per_page: int = 100,
    ) -> list[dict]:
        """Return the comments on an issue (or on a PR's discussion thread)."""
        out: list[dict] = []
        for page in self._iter_pages(
            f"/repos/{owner}/{repo}/issues/{number}/comments", per_page=per_page
        ):
            out.extend(page)
        return out

    def get_issue_timeline(
        self,
        owner: str,
        repo: str,
        number: int,
        per_page: int = 100,
    ) -> list[dict]:
        """Return an issue's timeline events (labeled, closed, referenced, ...)."""
        out: list[dict] = []
        for page in self._iter_pages(
            f"/repos/{owner}/{repo}/issues/{number}/timeline", per_page=per_page
        ):
            out.extend(page)
        return out

    # ------------------------------------------------------------------
    # Team / organization
    # ------------------------------------------------------------------
    def get_team_members(self, org: str, team_slug: str, per_page: int = 100) -> list[dict]:
        """
        Return members of an organization team.

        Requires a token with read access to the organization. Raises
        GitHubError when the team does not exist or access is denied.
        """
        out: list[dict] = []
        for page in self._iter_pages(
            f"/orgs/{org}/teams/{team_slug}/members", per_page=per_page
        ):
            out.extend(page)
        return out

    def get_team(self, org: str, team_slug: str) -> dict:
        """Return a single team's metadata."""
        return self._request("GET", f"/orgs/{org}/teams/{team_slug}")

    # ------------------------------------------------------------------
    # Activity detail
    # ------------------------------------------------------------------
    def get_commit_details(self, owner: str, repo: str, sha: str) -> dict:
        """Return a single commit with its changed files and diff stats."""
        return self._request("GET", f"/repos/{owner}/{repo}/commits/{sha}")

    def get_pull_request(self, owner: str, repo: str, number: int) -> dict:
        """Return a single pull request with additions/deletions/changed files."""
        return self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}")

    def get_pr_reviews(self, owner: str, repo: str, number: int, per_page: int = 100) -> list[dict]:
        """Return all reviews submitted on a pull request."""
        out: list[dict] = []
        for page in self._iter_pages(
            f"/repos/{owner}/{repo}/pulls/{number}/reviews", per_page=per_page
        ):
            out.extend(page)
        return out

    def get_pr_reviewers(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        """Return the requested reviewers of a pull request."""
        return self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}/requested_reviewers")

    def get_pr_files(self, owner: str, repo: str, number: int, per_page: int = 100) -> list[dict]:
        """Return the files (with patches) changed by a pull request."""
        out: list[dict] = []
        for page in self._iter_pages(
            f"/repos/{owner}/{repo}/pulls/{number}/files", per_page=per_page
        ):
            out.extend(page)
        return out

    # ------------------------------------------------------------------
    # Git objects (used by the AI auto-fix workflow - all via API)
    # ------------------------------------------------------------------
    def get_branch_sha(self, owner: str, repo: str, branch: str) -> str:
        """Return the current commit SHA of a branch."""
        ref = self._request("GET", f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
        return ref.get("object", {}).get("sha", "")

    def get_default_branch(self, owner: str, repo: str) -> str:
        """Return the repository's default branch name."""
        meta = self.get_repository(owner, repo)
        return meta.get("default_branch", "main")

    def create_branch(self, owner: str, repo: str, new_branch: str, base_sha: str) -> dict:
        """Create a branch (ref) pointing at an existing commit SHA."""
        return self._request(
            "POST",
            "/repos/{0}/{1}/git/refs".format(owner, repo),
            json={"ref": f"refs/heads/{new_branch}", "sha": base_sha},
        )

    def _create_blob(self, owner: str, repo: str, content: str) -> str:
        """Create a git blob and return its SHA."""
        blob = self._request(
            "POST",
            f"/repos/{owner}/{repo}/git/blobs",
            json={"content": content, "encoding": "utf-8"},
        )
        return blob.get("sha", "")

    def _create_tree(
        self,
        owner: str,
        repo: str,
        base_tree: str,
        path: str,
        blob_sha: str,
    ) -> str:
        """Create a tree with a single changed file on top of a base tree."""
        tree = self._request(
            "POST",
            f"/repos/{owner}/{repo}/git/trees",
            json={
                "base_tree": base_tree,
                "tree": [{"path": path, "mode": "100644", "type": "blob", "sha": blob_sha}],
            },
        )
        return tree.get("sha", "")

    def _create_commit(
        self,
        owner: str,
        repo: str,
        message: str,
        tree_sha: str,
        parent_sha: str,
    ) -> str:
        """Create a commit and return its SHA."""
        commit = self._request(
            "POST",
            f"/repos/{owner}/{repo}/git/commits",
            json={"message": message, "tree": tree_sha, "parents": [parent_sha]},
        )
        return commit.get("sha", "")

    def _update_branch_ref(self, owner: str, repo: str, branch: str, commit_sha: str) -> dict:
        """Point a branch ref at a new commit SHA."""
        return self._request(
            "PATCH",
            f"/repos/{owner}/{repo}/git/refs/heads/{branch}",
            json={"sha": commit_sha, "force": False},
        )

    def commit_file_via_api(
        self,
        owner: str,
        repo: str,
        branch: str,
        path: str,
        content: str,
        message: str,
    ) -> str:
        """
        Commit a file's new content to a branch using only the Git Data API.

        This never touches the local filesystem and never modifies the
        default branch: the caller is responsible for creating `branch`
        first (a new feature branch). Returns the new commit SHA.
        """
        base_sha = self.get_branch_sha(owner, repo, branch)
        blob_sha = self._create_blob(owner, repo, content)
        tree_sha = self._create_tree(owner, repo, base_sha, path, blob_sha)
        commit_sha = self._create_commit(owner, repo, message, tree_sha, base_sha)
        self._update_branch_ref(owner, repo, branch, commit_sha)
        return commit_sha

    def fetch_file_content(self, owner: str, repo: str, path: str, ref: str) -> str:
        """Return the current text content of a file on a given branch."""
        data = self._request(
            "GET", f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref}
        )
        content = data.get("content", "")
        if data.get("encoding") == "base64":
            import base64
            content = base64.b64decode(content).decode("utf-8", errors="replace")
        return content

    def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        head: str,
        base: str,
        body: str = "",
    ) -> dict:
        """Open a pull request. Never merges anything."""
        return self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            json={"title": title, "head": head, "base": base, "body": body},
        )

    # ------------------------------------------------------------------
    # Dashboard aggregation
    # ------------------------------------------------------------------
    def _since_iso(self, days: int) -> str:
        return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat() + "Z"

    def _collect_commits(
        self, owner: str, repo: str, since: str, until: Optional[str] = None
    ):
        """
        Return (counts, latest_dates, recent_list, total) for commits between
        `since` and `until` (both ISO strings). `until` is optional.

        ``counts`` maps a GitHub login to the number of commits authored by
        that account (matched via ``commit.author.login``). Commits whose
        author is unknown (``author`` is null, or the commit was authored with
        an email GitHub cannot map to a user) are not attributed to anyone and
        never crash the aggregation; they are still counted in ``total``.
        """
        counts: dict[str, int] = {}
        latest: dict[str, datetime] = {}
        recent: list[dict] = []
        total = 0
        params: dict[str, Any] = {"since": since}
        if until:
            params["until"] = until
        for page in self._iter_pages(
            f"/repos/{owner}/{repo}/commits", params=params, per_page=100
        ):
            for commit in page:
                total += 1
                author = commit.get("author") or {}
                login = author.get("login")
                date = (commit.get("commit") or {}).get("author", {}).get("date")
                parsed = None
                if date:
                    try:
                        parsed = datetime.fromisoformat(date.replace("Z", "+00:00"))
                    except ValueError:
                        parsed = None
                if login:
                    counts[login] = counts.get(login, 0) + 1
                    if parsed and (login not in latest or parsed > latest[login]):
                        latest[login] = parsed
                if login and parsed:
                    recent.append(
                        {
                            "sha": commit.get("sha", "")[:10],
                            "full_sha": commit.get("sha", ""),
                            "author": login,
                            "message": (commit.get("commit") or {}).get("message", "").split("\n")[0],
                            "date": date,
                            "html_url": commit.get("html_url", ""),
                        }
                    )
        return counts, latest, recent, total

    def _iter_commits_90d(self, owner: str, repo: str) -> Iterator[dict]:
        """Yield every commit from the last 90 days (paginated)."""
        since = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat() + "Z"
        for page in self._iter_pages(
            f"/repos/{owner}/{repo}/commits",
            params={"since": since},
            per_page=100,
        ):
            for commit in page:
                yield commit

    def build_team_report(
        self,
        owner: str,
        repo: str,
        days: Optional[int] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Aggregate everything needed by the dashboard into one structure.

        Team members come from the real repository collaborators
        (GET /repos/{owner}/{repo}/collaborators), with the repository owner
        guaranteed to be present and GITHUB_TEAM members merged in as well.

        The activity window is the last `days` days (default
        ACTIVITY_WINDOW_DAYS). For exact ranges pass explicit `since`/`until`
        ISO strings instead - both are optional and any combination works.

        Returns a dict with:
            overview:   {members, active/inactive counts, commits, PRs, issues}
            members:    per-user metrics including the weighted activity score.
            activity_feed: recent commits + PRs + issues from ALL collaborators.
            pushes:     most recent commits with file-level detail.
            pull_requests: recent PRs with review status.
            issues:     recent issues.
            languages:  byte counts per language.
            repo:       repository metadata.
        """
        from utils import activity as activity_mod

        days = days or settings.ACTIVITY_WINDOW_DAYS
        if since is None:
            since = self._since_iso(days)
        logger.info("GitHub repository: %s/%s (since %s, until %s)", owner, repo, since, until)

        # --- Repo metadata (owner identity is used below) ---
        try:
            repo_meta = self.get_repository(owner, repo)
        except GitHubError:
            repo_meta = {}
        repo_owner = repo_meta.get("owner") or {}
        owner_login = repo_owner.get("login", "")
        owner_avatar = repo_owner.get("avatar_url", "")
        owner_url = repo_owner.get("html_url", f"https://github.com/{owner_login}")

        # ------------------------------------------------------------------
        # Team source - REAL repository collaborators (not just contributors)
        # ------------------------------------------------------------------
        # The old code only used /contributors, which lists people who have
        # committed. Collaborators who have been granted access but not pushed
        # anything yet never appeared - that is why "Total Members" showed 1.
        collaborators: list[dict[str, Any]] = []
        try:
            collaborators = self.get_collaborators(owner, repo)
            logger.info("Collaborators fetched: %d", len(collaborators))
        except GitHubError as exc:
            logger.warning(
                "Collaborators endpoint unavailable (%s); falling back to contributors.",
                exc.message,
            )

        # Contributors also give us an all-time commit count per user.
        contributors: list[dict[str, Any]] = []
        try:
            contributors = self.get_contributors(owner, repo)
        except GitHubError:
            contributors = []
        contributor_counts: dict[str, int] = {
            (c.get("login") or ""): int(c.get("contributions") or 0)
            for c in contributors
            if c.get("login")
        }

        # --- Team source: REAL repository collaborators (not just contributors) ---
        # GitHub's /collaborators endpoint returns the actual members of the
        # repository (not just people who have committed), so the dashboard
        # shows everyone granted access. Invitations that have not been
        # accepted yet are merged in too, otherwise newly added members stay
        # invisible until they accept, and the repository owner is always
        # included. Contributors are used only when the token cannot list
        # collaborators at all.
        team_name = settings.GITHUB_TEAM or ""
        members: list[dict[str, Any]] = []
        seen: set[str] = set()

        def _add_member(
            login: str,
            avatar: str,
            url: str,
            role: str,
            permission: str,
            permissions: Optional[dict[str, bool]] = None,
            pending: bool = False,
        ) -> None:
            if not login or login in seen:
                return
            seen.add(login)
            members.append(
                {
                    "username": login,
                    "avatar": avatar,
                    "url": url,
                    "role": role,
                    "permission": permission,
                    "permissions": permissions or {},
                    "is_owner": login == owner_login,
                    "pending": pending,
                    "commits": 0,
                    "commits_all_time": contributor_counts.get(login, 0),
                    "contributions": contributor_counts.get(login, 0),
                    "additions": 0,
                    "deletions": 0,
                    "pr_count": 0,
                    "prs_created": 0,
                    "prs_open": 0,
                    "prs_merged": 0,
                    "prs_reviewed": 0,
                    "issue_count": 0,
                    "issues_created": 0,
                    "issues_closed": 0,
                    "last_active": None,
                    "last_active_days": None,
                }
            )

        if collaborators:
            for item in collaborators:
                login = item.get("login")
                if not login:
                    continue
                perms = item.get("permissions") or {}
                permission = (
                    "admin" if perms.get("admin")
                    else "maintain" if perms.get("maintain")
                    else "write" if perms.get("push")
                    else "read"
                )
                _add_member(
                    login,
                    item.get("avatar_url", ""),
                    item.get("html_url", ""),
                    "owner" if login == owner_login else "collaborator",
                    "admin" if login == owner_login else permission,
                    permissions=perms,
                )

            # Pending invitations: people granted access who have not yet
            # accepted. They are normalized to the same member shape.
            try:
                pending_invites = self.get_pending_invitations(owner, repo)
            except GitHubError as exc:
                logger.warning(
                    "Could not list pending invitations for %s/%s (%s).",
                    owner, repo, exc.message,
                )
                pending_invites = []
            for item in pending_invites:
                username = item.get("username") or item.get("login") or ""
                if not username:
                    continue
                perms = item.get("permissions") or {}
                _add_member(
                    username,
                    item.get("avatar") or item.get("avatar_url") or "",
                    item.get("url") or item.get("html_url") or "",
                    item.get("role") or "pending read",
                    (
                        "admin" if perms.get("admin")
                        else "maintain" if perms.get("maintain")
                        else "write" if perms.get("push")
                        else "read"
                    ),
                    permissions=perms,
                    pending=True,
                )
        else:
            # Fallback when the collaborators endpoint is not accessible.
            for item in contributors:
                login = item.get("login")
                if not login:
                    continue
                _add_member(
                    login,
                    item.get("avatar_url", ""),
                    item.get("html_url", ""),
                    "owner" if login == owner_login else "contributor",
                    "admin" if login == owner_login else "read",
                )

        # The owner must always be a member - even if the collaborators
        # endpoint omits them, they are shown (but never as the ONLY member).
        if owner_login and owner_login not in seen:
            _add_member(
                owner_login, owner_avatar, owner_url, "owner", "admin",
                permissions={"admin": True, "push": True, "pull": True},
            )

        # Optional org team members are merged in (union), never replacing
        # the collaborators - this preserves the GITHUB_TEAM feature.
        if team_name:
            try:
                team_members = self.get_team_members(owner, team_name)
            except GitHubError as exc:
                logger.warning("Team '%s' not accessible (%s).", team_name, exc.message)
                team_members = []
            for item in team_members:
                login = item.get("login")
                if not login:
                    continue
                _add_member(
                    login,
                    item.get("avatar_url", ""),
                    item.get("html_url", ""),
                    "owner" if login == owner_login else "team member",
                    "admin" if login == owner_login else "read",
                )

        logger.info("Team members resolved: %d", len(members))

        # --- Commits (each attributed to commit.author.login) ---
        commit_counts, last_commit, recent_commits, commit_total = self._collect_commits(
            owner, repo, since, until
        )
        logger.info("Commits fetched: %d (window: %d)", commit_total, len(recent_commits))
        member_index = {m["username"]: m for m in members}
        for login, count in commit_counts.items():
            if login in member_index:
                member_index[login]["commits"] = count
        latest_by_member: dict[str, datetime] = {}

        # --- Pull requests (all states, filtered to the window) ---
        prs_all = self.get_pull_requests(owner, repo, state="all")
        logger.info("Pull requests fetched: %d", len(prs_all))
        prs: list[dict[str, Any]] = []
        prs_in_window = [
            pr for pr in prs_all
            if self._within_window(
                pr.get("created_at") or pr.get("updated_at"), since, until
            )
        ]
        for pr in prs_in_window[:40]:
            number = pr.get("number", 0)
            detail = pr
            try:
                detail = self.get_pull_request(owner, repo, number)
            except GitHubError:
                pass
            reviews = self.get_pr_reviews(owner, repo, number)
            reviewers: list[str] = []
            try:
                reviewers = [
                    r.get("login", "")
                    for r in self.get_pr_reviewers(owner, repo, number).get("users", [])
                ]
            except GitHubError:
                pass
            review_states = [r.get("state", "").upper() for r in reviews]
            review_status = "approved" if "APPROVED" in review_states else (
                "changes_requested" if "CHANGES_REQUESTED" in review_states else (
                    "reviewed" if review_states else "no_reviews"
                )
            )
            for review in reviews:
                reviewer = (review.get("user") or {}).get("login")
                if reviewer:
                    self._track_latest(latest_by_member, reviewer, review.get("submitted_at"))
                    if reviewer in member_index:
                        member_index[reviewer]["prs_reviewed"] += 1

            author = (pr.get("user") or {}).get("login", "")
            if author:
                self._track_latest(latest_by_member, author, pr.get("created_at"))
                self._track_latest(latest_by_member, author, pr.get("updated_at"))
                if author in member_index:
                    member_index[author]["prs_created"] += 1
                    member_index[author]["pr_count"] += 1
                    if pr.get("merged_at"):
                        member_index[author]["prs_merged"] += 1
                    elif pr.get("state") == "open":
                        member_index[author]["prs_open"] += 1

            prs.append(
                {
                    "number": number,
                    "title": pr.get("title", ""),
                    "author": author,
                    "state": pr.get("state", ""),
                    "merged": bool(pr.get("merged_at")),
                    "created_at": pr.get("created_at", ""),
                    "updated_at": pr.get("updated_at", ""),
                    "html_url": pr.get("html_url", ""),
                    "additions": detail.get("additions", 0) or 0,
                    "deletions": detail.get("deletions", 0) or 0,
                    "changed_files": detail.get("changed_files", 0) or 0,
                    "review_status": review_status,
                    "reviewers": reviewers,
                }
            )

        # --- Issues (PRs excluded by the issues endpoint) ---
        issues_all = self.get_issues(owner, repo, state="all")
        logger.info("Issues fetched: %d", len(issues_all))
        issues: list[dict[str, Any]] = []
        for issue in issues_all:
            if issue.get("pull_request"):
                continue
            if not self._within_window(
                issue.get("created_at") or issue.get("updated_at"), since, until
            ):
                continue
            author = (issue.get("user") or {}).get("login", "")
            if author:
                self._track_latest(latest_by_member, author, issue.get("created_at"))
                self._track_latest(latest_by_member, author, issue.get("updated_at"))
                if author in member_index:
                    member_index[author]["issues_created"] += 1
                    member_index[author]["issue_count"] += 1
                    if issue.get("state") == "closed":
                        member_index[author]["issues_closed"] += 1
            issues.append(
                {
                    "number": issue.get("number", 0),
                    "title": issue.get("title", ""),
                    "author": author,
                    "state": issue.get("state", ""),
                    "labels": [label.get("name", "") for label in issue.get("labels", [])],
                    "created_at": issue.get("created_at", ""),
                    "updated_at": issue.get("updated_at", ""),
                    "html_url": issue.get("html_url", ""),
                    "assignees": [
                        (a or {}).get("login", "")
                        for a in issue.get("assignees", [])
                    ],
                }
            )

        # --- Merge commit + review activity into last-activity ---
        for login, parsed in last_commit.items():
            self._track_latest(latest_by_member, login, parsed.isoformat())

        # --- Unified activity feed (commits + PRs + issues) ---
        activity_feed: list[dict[str, Any]] = []
        for c in recent_commits:
            activity_feed.append(
                {
                    "author": c.get("author") or "unknown",
                    "actor": c.get("author") or "unknown",
                    "type": "push",
                    "category": "commit",
                    "action": "pushed commit",
                    "title": c.get("message", ""),
                    "date": c.get("date", ""),
                    "relative": relative_time_label(c.get("date", "")),
                    "url": c.get("html_url", ""),
                    "sha": c.get("sha", ""),
                    "detail": {"sha": c.get("full_sha", ""), "message": c.get("message", "")},
                }
            )
        for pr in prs_in_window:
            pr_author = (pr.get("user") or {}).get("login", "unknown")
            if pr.get("merged_at"):
                pr_action = "merged pull request"
            elif pr.get("state") == "open":
                pr_action = "opened pull request"
            else:
                pr_action = "closed pull request"
            activity_feed.append(
                {
                    "author": pr_author,
                    "actor": pr_author,
                    "type": "pull_request",
                    "category": "pull_request",
                    "action": pr_action,
                    "title": pr.get("title", ""),
                    "date": pr.get("updated_at") or pr.get("created_at") or "",
                    "relative": relative_time_label(pr.get("updated_at") or pr.get("created_at") or ""),
                    "url": pr.get("html_url", ""),
                    "number": pr.get("number"),
                    "detail": {"number": pr.get("number"), "title": pr.get("title", "")},
                }
            )
        for issue in issues:
            issue_author = (issue.get("author") or "").strip() or "unknown"
            activity_feed.append(
                {
                    "author": issue_author,
                    "actor": issue_author,
                    "type": "issue",
                    "category": "issue",
                    "action": (
                        "closed issue" if issue.get("state") == "closed" else "opened issue"
                    ),
                    "title": issue.get("title", ""),
                    "date": issue.get("updated_at") or issue.get("created_at") or "",
                    "relative": relative_time_label(issue.get("updated_at") or issue.get("created_at") or ""),
                    "url": issue.get("html_url", ""),
                    "number": issue.get("number"),
                    "detail": {"number": issue.get("number"), "title": issue.get("title", "")},
                }
            )

        # --- Real GitHub event stream merged into the feed ---------------
        # This surfaces pushes, branch creation, members added, reviews and
        # comments that the synthesized feed above cannot see.
        try:
            events = self.get_repo_events(owner, repo, since=since)
        except GitHubError as exc:
            logger.warning(
                "Could not fetch repo events for %s/%s (%s).",
                owner, repo, exc.message,
            )
            events = []
        for event in events:
            item = self._event_feed_item(event)
            if item and self._within_window(item.get("date"), since, until):
                activity_feed.append(item)

        activity_feed.sort(key=lambda item: item.get("date") or "", reverse=True)
        activity_feed = activity_feed[:100]

        # --- Languages ---
        try:
            languages = self.get_repository_languages(owner, repo)
        except GitHubError:
            languages = {}

        # --- Finalize members: last activity + score ---
        now = datetime.now(timezone.utc)
        active_members = 0
        for member in members:
            last = latest_by_member.get(member["username"])
            if last:
                member["last_active"] = last.isoformat()
                member["last_active_days"] = max(
                    int((now - last).total_seconds() // 86400), 0
                )
            member = activity_mod.enrich_member(member)
            member["is_active"] = member["activity_status"] == "ACTIVE"
            if member["is_active"]:
                active_members += 1
            member["score_reason"] = activity_mod.score_reason(
                member["username"],
                member.get("commits", 0),
                member.get("prs_created", 0),
                member.get("prs_reviewed", 0),
                member.get("issues_created", 0),
            )

        members.sort(key=lambda m: m["activity_score"], reverse=True)

        # --- Recent pushes (commit details for the top N) ---
        pushes: list[dict[str, Any]] = []
        total_additions = 0
        total_deletions = 0
        for commit in recent_commits[:COMMIT_DETAIL_LIMIT]:
            try:
                detail = self.get_commit_details(owner, repo, commit["full_sha"])
                if not isinstance(detail, dict):
                    raise GitHubError("Unexpected commit detail payload", status_code=502)
                files = [
                    {
                        "filename": f.get("filename", ""),
                        "additions": f.get("additions", 0),
                        "deletions": f.get("deletions", 0),
                        "status": f.get("status", ""),
                    }
                    for f in detail.get("files", [])
                ][:30]
                stats = {
                    "additions": detail.get("stats", {}).get("additions", 0) or 0,
                    "deletions": detail.get("stats", {}).get("deletions", 0) or 0,
                }
                commit["files"] = files
                commit["stats"] = stats
                total_additions += stats["additions"]
                total_deletions += stats["deletions"]
                author_login = commit.get("author")
                if author_login and author_login in member_index:
                    member_index[author_login]["additions"] = (
                        member_index[author_login].get("additions", 0) + stats["additions"]
                    )
                    member_index[author_login]["deletions"] = (
                        member_index[author_login].get("deletions", 0) + stats["deletions"]
                    )
            except (GitHubError, AttributeError, TypeError):
                commit["files"] = []
                commit["stats"] = {"additions": 0, "deletions": 0}
            pushes.append(commit)
        pushes.reverse()

        total_members = len(members)
        # PR/issue counts come from the FULL lists (not the time-window slice)
        # so the overview always matches the actual repo state.
        open_prs = sum(1 for pr in prs_all if pr.get("state") == "open")
        merged_prs = sum(1 for pr in prs_all if pr.get("merged_at"))
        closed_prs = sum(
            1 for pr in prs_all
            if pr.get("state") == "closed" and not pr.get("merged_at")
        )
        total_prs = len(prs_all)
        real_issues = [i for i in issues_all if not i.get("pull_request")]
        open_issues = sum(1 for i in real_issues if i.get("state") == "open")
        closed_issues = sum(
            1 for i in real_issues
            if i.get("state") == "closed" and i.get("closed_at")
        )
        contributors_count = len(contributors)
        overview = {
            "members": total_members,
            # Active = activity within the last RECENTLY_ACTIVE_DAYS (7) days.
            "active_members": active_members,
            # Inactive = everything else, so Active + Inactive always equals total.
            "inactive_members": total_members - active_members,
            "recently_active_members": sum(
                1 for m in members if m["activity_status"] == "RECENTLY ACTIVE"
            ),
            "total_commits": commit_total,
            "open_prs": open_prs,
            "merged_prs": merged_prs,
            "closed_prs": closed_prs,
            "total_prs": total_prs,
            "open_issues": open_issues,
            "closed_issues": closed_issues,
            "contributors_count": contributors_count,
            "activity_events": len(events),
            "total_additions": total_additions,
            "total_deletions": total_deletions,
        }

        return {
            "owner": owner,
            "repo": repo,
            "team_name": team_name or repo,
            "overview": overview,
            "members": members,
            "activity_feed": activity_feed,
            "pushes": pushes,
            "pull_requests": prs,
            "issues": issues,
            "languages": languages,
            "contributors": [
                {
                    "username": c.get("login", ""),
                    "contributions": c.get("contributions", 0),
                    "avatar": c.get("avatar_url", ""),
                    "url": c.get("html_url", ""),
                }
                for c in contributors
            ],
            "repo": {
                "name": repo_meta.get("full_name", f"{owner}/{repo}"),
                "description": repo_meta.get("description", ""),
                "stars": repo_meta.get("stargazers_count", 0),
                "forks": repo_meta.get("forks_count", 0),
                "open_issues": repo_meta.get("open_issues_count", 0),
                "default_branch": repo_meta.get("default_branch", "main"),
            },
        }

    @staticmethod
    def _within_window(
        date_str: Optional[str],
        since: str,
        until: Optional[str] = None,
    ) -> bool:
        """True when an ISO date is between `since` and `until` (both ISO)."""
        if not date_str:
            return False
        try:
            parsed = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            return False
        try:
            since_parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            since_parsed = None
        if since_parsed is not None and parsed < since_parsed:
            return False
        if until:
            try:
                until_parsed = datetime.fromisoformat(until.replace("Z", "+00:00"))
            except ValueError:
                until_parsed = None
            if until_parsed is not None and parsed > until_parsed:
                return False
        return True

    @staticmethod
    def _track_latest(
        mapping: dict[str, datetime],
        login: str,
        date_str: Optional[str],
    ) -> None:
        """Record the newest date seen for a member."""
        if not date_str:
            return
        try:
            parsed = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            return
        if login not in mapping or parsed > mapping[login]:
            mapping[login] = parsed

    @staticmethod
    def _event_feed_item(event: dict) -> Optional[dict]:
        """Map a raw GitHub event into an activity-feed item (or None)."""
        etype = event.get("type", "")
        actor = ((event.get("actor") or {}).get("login")) or ""
        created = event.get("created_at", "")
        payload = event.get("payload") or {}
        repo_full = ((event.get("repo") or {}).get("name")) or ""
        repo_url = f"https://github.com/{repo_full}"

        if etype == "PushEvent":
            commits = payload.get("commits") or []
            count = len(commits)
            title = ""
            if commits:
                title = (commits[-1].get("message") or "").split("\n")[0]
            ref = payload.get("ref", "") or ""
            branch = ref.rsplit("/", 1)[-1] if ref else ""
            return {
                "author": actor,
                "actor": actor,
                "type": "push",
                "category": "commit",
                "action": (
                    f"pushed {count} commit{'s' if count != 1 else ''}"
                    + (f" to {branch}" if branch else "")
                ),
                "title": title or branch or "branch push",
                "date": created,
                "relative": relative_time_label(created),
                "url": payload.get("compare") or repo_url,
                "detail": {
                    "ref": ref,
                    "count": count,
                    "shas": [c.get("sha", "")[:7] for c in commits[:20]],
                },
            }
        if etype == "PullRequestEvent":
            pr = payload.get("pull_request") or {}
            action = payload.get("action", "updated")
            state_label = "merged" if pr.get("merged_at") else action
            return {
                "author": actor,
                "actor": actor,
                "type": "pull_request",
                "category": "pull_request",
                "action": f"{action} pull request",
                "title": pr.get("title", ""),
                "date": created,
                "relative": relative_time_label(created),
                "url": pr.get("html_url", ""),
                "number": pr.get("number"),
                "detail": {"state": state_label, "number": pr.get("number")},
            }
        if etype == "PullRequestReviewEvent":
            pr = payload.get("pull_request") or {}
            state = (payload.get("review") or {}).get("state", "reviewed")
            return {
                "author": actor,
                "actor": actor,
                "type": "pull_request",
                "category": "pull_request",
                "action": f"reviewed pull request ({state})",
                "title": pr.get("title", ""),
                "date": created,
                "relative": relative_time_label(created),
                "url": pr.get("html_url", ""),
                "number": pr.get("number"),
                "detail": {"review_state": state, "number": pr.get("number")},
            }
        if etype == "PullRequestReviewCommentEvent":
            pr = payload.get("pull_request") or {}
            comment = payload.get("comment") or {}
            return {
                "author": actor,
                "actor": actor,
                "type": "pull_request",
                "category": "pull_request",
                "action": "commented on pull request",
                "title": pr.get("title", ""),
                "date": created,
                "relative": relative_time_label(created),
                "url": comment.get("html_url") or pr.get("html_url", ""),
                "number": pr.get("number"),
                "detail": {
                    "number": pr.get("number"),
                    "comment": (comment.get("body") or "")[:200],
                },
            }
        if etype == "IssuesEvent":
            issue = payload.get("issue") or {}
            action = payload.get("action", "updated")
            return {
                "author": actor,
                "actor": actor,
                "type": "issue",
                "category": "issue",
                "action": f"{action} issue",
                "title": issue.get("title", ""),
                "date": created,
                "relative": relative_time_label(created),
                "url": issue.get("html_url", ""),
                "number": issue.get("number"),
                "detail": {"state": issue.get("state", ""), "number": issue.get("number")},
            }
        if etype == "IssueCommentEvent":
            issue = payload.get("issue") or {}
            comment = payload.get("comment") or {}
            return {
                "author": actor,
                "actor": actor,
                "type": "issue",
                "category": "issue",
                "action": "commented on issue",
                "title": issue.get("title", ""),
                "date": created,
                "relative": relative_time_label(created),
                "url": comment.get("html_url") or issue.get("html_url", ""),
                "number": issue.get("number"),
                "detail": {
                    "number": issue.get("number"),
                    "comment": (comment.get("body") or "")[:200],
                },
            }
        if etype in ("CreateEvent", "DeleteEvent"):
            ref_type = payload.get("ref_type", "")
            ref = payload.get("ref", "") or ""
            verb = "created" if etype == "CreateEvent" else "deleted"
            what = f"{ref_type} {ref}".strip() if ref_type else "branch"
            return {
                "author": actor,
                "actor": actor,
                "type": "branch",
                "category": "commit",
                "action": f"{verb} {what}",
                "title": what,
                "date": created,
                "relative": relative_time_label(created),
                "url": f"{repo_url}/tree/{ref}" if ref else repo_url,
                "detail": {"ref_type": ref_type, "ref": ref},
            }
        if etype == "MemberEvent":
            member = payload.get("member") or {}
            return {
                "author": actor,
                "actor": actor,
                "type": "member",
                "category": "member",
                "action": f"added member {member.get('login', '')}",
                "title": f"Member added: {member.get('login', '')}",
                "date": created,
                "relative": relative_time_label(created),
                "url": (member or {}).get("html_url", ""),
                "detail": {"member": member.get("login", "")},
            }
        if etype in ("WatchEvent", "ForkEvent", "ReleaseEvent"):
            action_map = {
                "WatchEvent": "starred the repository",
                "ForkEvent": "forked the repository",
                "ReleaseEvent": "published a release",
            }
            label = action_map.get(etype, etype)
            return {
                "author": actor,
                "actor": actor,
                "type": "other",
                "category": "other",
                "action": label,
                "title": label,
                "date": created,
                "relative": relative_time_label(created),
                "url": repo_url,
                "detail": {},
            }
        return None

    def build_pr_detail(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        """Build a rich, dashboard-ready detail view for one pull request."""
        pr = self.get_pull_request(owner, repo, number)
        author = (pr.get("user") or {}).get("login", "unknown")
        additions = (pr or {}).get("additions") or 0
        deletions = (pr or {}).get("deletions") or 0
        changed_files = (pr or {}).get("changed_files") or 0
        try:
            commits = self.get_pr_commits(owner, repo, number)
        except GitHubError:
            commits = []
        try:
            comments = self.get_pr_comments(owner, repo, number)
        except GitHubError:
            comments = []
        try:
            reviews = self.get_pr_reviews(owner, repo, number)
        except GitHubError:
            reviews = []
        return {
            "number": pr.get("number"),
            "title": pr.get("title", ""),
            "state": pr.get("state", ""),
            "merged": bool(pr.get("merged_at")),
            "merged_at": pr.get("merged_at"),
            "author": author,
            "author_avatar": (pr.get("user") or {}).get("avatar_url", ""),
            "author_url": (pr.get("user") or {}).get("html_url", ""),
            "created_at": pr.get("created_at"),
            "updated_at": pr.get("updated_at"),
            "body": pr.get("body") or "",
            "url": pr.get("html_url", ""),
            "head": (pr.get("head") or {}).get("ref", ""),
            "base": (pr.get("base") or {}).get("ref", ""),
            "additions": additions,
            "deletions": deletions,
            "changed_files": changed_files,
            "commits_count": len(commits),
            "labels": [l.get("name") for l in pr.get("labels") or []],
            "commits": [
                {
                    "sha": c.get("sha", "")[:7],
                    "full_sha": c.get("sha", ""),
                    "message": ((c.get("commit") or {}).get("message") or "").split("\n")[0],
                    "author": ((c.get("commit") or {}).get("author") or {}).get("name", ""),
                    "date": ((c.get("commit") or {}).get("author") or {}).get("date", ""),
                }
                for c in commits[:20]
            ],
            "comments": [
                {
                    "author": (c.get("user") or {}).get("login", ""),
                    "date": c.get("created_at", ""),
                    "body": (c.get("body") or "")[:300],
                }
                for c in comments[:20]
            ],
            "reviews": [
                {
                    "author": (r.get("user") or {}).get("login", ""),
                    "state": r.get("state", ""),
                    "submitted_at": r.get("submitted_at", ""),
                    "body": (r.get("body") or "")[:200],
                }
                for r in reviews[:20]
            ],
        }

    def build_issue_detail(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        """Build a rich, dashboard-ready detail view for one issue."""
        issue = self.get_issue(owner, repo, number)
        try:
            comments = self.get_issue_comments(owner, repo, number)
        except GitHubError:
            comments = []
        try:
            timeline = self.get_issue_timeline(owner, repo, number)
        except GitHubError:
            timeline = []
        events = [
            {
                "event": t.get("event", ""),
                "actor": ((t.get("actor") or {}).get("login")) or "",
                "date": t.get("created_at", ""),
            }
            for t in timeline[:40]
            if t.get("event") not in ("commented", "cross-referenced")
        ]
        return {
            "number": issue.get("number"),
            "title": issue.get("title", ""),
            "state": issue.get("state", ""),
            "author": (issue.get("user") or {}).get("login", ""),
            "author_avatar": (issue.get("user") or {}).get("avatar_url", ""),
            "created_at": issue.get("created_at"),
            "updated_at": issue.get("updated_at"),
            "closed_at": issue.get("closed_at"),
            "body": issue.get("body") or "",
            "url": issue.get("html_url", ""),
            "labels": [
                {
                    "name": label.get("name", ""),
                    "color": (label.get("color") or "6e7781"),
                }
                for label in issue.get("labels") or []
            ],
            "assignees": [
                (assignee or {}).get("login", "")
                for assignee in issue.get("assignees") or []
            ],
            "comments_count": len(comments),
            "comments": [
                {
                    "author": (c.get("user") or {}).get("login", ""),
                    "date": c.get("created_at", ""),
                    "body": (c.get("body") or "")[:300],
                }
                for c in comments[:20]
            ],
            "timeline_events": events,
        }

    def build_commit_detail(self, owner: str, repo: str, sha: str) -> dict[str, Any]:
        """Build a dashboard-ready detail view for a single commit."""
        detail = self.get_commit_details(owner, repo, sha)
        if not isinstance(detail, dict):
            return {"sha": sha, "error": "Commit detail unavailable"}
        files = detail.get("files") or []
        return {
            "sha": detail.get("sha", sha),
            "short_sha": sha[:7],
            "message": ((detail.get("commit") or {}).get("message") or "").split("\n")[0],
            "full_message": (detail.get("commit") or {}).get("message") or "",
            "author": ((detail.get("commit") or {}).get("author") or {}).get("name", ""),
            "author_login": ((detail.get("author") or {}).get("login")) or "",
            "author_avatar": ((detail.get("author") or {}).get("avatar_url")) or "",
            "date": ((detail.get("commit") or {}).get("author") or {}).get("date", ""),
            "url": detail.get("html_url", ""),
            "stats": {
                "additions": (detail.get("stats") or {}).get("additions", 0),
                "deletions": (detail.get("stats") or {}).get("deletions", 0),
                "total": (detail.get("stats") or {}).get("total", 0),
            },
            "files": [
                {
                    "filename": f.get("filename", ""),
                    "status": f.get("status", ""),
                    "additions": f.get("additions", 0),
                    "deletions": f.get("deletions", 0),
                    "patch": (f.get("patch") or "")[:400],
                }
                for f in files[:30]
            ],
        }

    def build_member_profile(self, owner: str, repo: str, username: str, days: Optional[int] = None) -> dict[str, Any]:
        """Build a focused profile for a single team member."""
        from utils import activity as activity_mod

        days = days or settings.ACTIVITY_WINDOW_DAYS
        since = self._since_iso(days)

        commits = self.get_commits(owner, repo, author=username, since=since)
        prs = self.get_pull_requests(owner, repo, state="all")
        authored_prs = [pr for pr in prs if (pr.get("user") or {}).get("login") == username]
        reviewed_prs = []
        for pr in authored_prs[:20]:
            for review in self.get_pr_reviews(owner, repo, pr["number"]):
                if (review.get("user") or {}).get("login") == username:
                    reviewed_prs.append(
                        {"pr": pr["number"], "state": review.get("state", ""), "submitted_at": review.get("submitted_at", "")}
                    )
        issues_all = self.get_issues(owner, repo, state="all")
        authored_issues = [
            i for i in issues_all
            if not i.get("pull_request") and (i.get("user") or {}).get("login") == username
        ]
        try:
            languages = self.get_repository_languages(owner, repo)
        except GitHubError:
            languages = {}

        last_active: Optional[datetime] = None
        for c in commits:
            date = (c.get("commit") or {}).get("author", {}).get("date")
            if date:
                try:
                    d = datetime.fromisoformat(date.replace("Z", "+00:00"))
                    if last_active is None or d > last_active:
                        last_active = d
                except ValueError:
                    pass

        member = {
            "username": username,
            "commits": len(commits),
            "pr_count": len(authored_prs),
            "prs_created": len(authored_prs),
            "prs_merged": sum(1 for pr in authored_prs if pr.get("merged_at")),
            "prs_reviewed": len(reviewed_prs),
            "issue_count": len(authored_issues),
            "issues_created": len(authored_issues),
            "issues_closed": sum(1 for i in authored_issues if i["state"] == "closed"),
            "last_active": last_active.isoformat() if last_active else None,
            "last_active_days": (
                max(int((datetime.now(timezone.utc) - last_active).total_seconds() // 86400), 0)
                if last_active else None
            ),
        }
        member = activity_mod.enrich_member(member)

        return {
            "username": username,
            "member": member,
            "commits": [
                {
                    "sha": c.get("sha", "")[:10],
                    "message": (c.get("commit") or {}).get("message", "").split("\n")[0],
                    "date": (c.get("commit") or {}).get("author", {}).get("date", ""),
                }
                for c in commits[:20]
            ],
            "pull_requests": [
                {
                    "number": pr.get("number"),
                    "title": pr.get("title", ""),
                    "state": pr.get("state", ""),
                    "merged": bool(pr.get("merged_at")),
                    "created_at": pr.get("created_at", ""),
                    "html_url": pr.get("html_url", ""),
                }
                for pr in authored_prs[:20]
            ],
            "reviews": reviewed_prs[:20],
            "issues": [
                {
                    "number": i.get("number"),
                    "title": i.get("title", ""),
                    "state": i.get("state", ""),
                    "created_at": i.get("created_at", ""),
                    "html_url": i.get("html_url", ""),
                }
                for i in authored_issues[:20]
            ],
            "languages": languages,
        }



def relative_time_label(date_str: str) -> str:
    """Human-friendly label like 'just now', '10 minutes ago' or '2 days ago'."""
    if not date_str:
        return ""
    try:
        parsed = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except ValueError:
        return ""
    seconds = max(int((datetime.now(timezone.utc) - parsed).total_seconds()), 0)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = seconds // 86400
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def compute_activity_score(member: dict[str, Any]) -> int:
    """
    Calculate a 0-100 activity score for a developer.

    Weighting (deliberately simple and transparent):
      * Commits      -> up to 50 points
      * Open PRs     -> up to 25 points
      * Open issues  -> up to 15 points (contribution to issues)
      * Recency      -> up to 10 points (touched in last 7 days)

    The thresholds are forgiving so a junior engineer is not punished.
    """
    commits = min(member.get("commits", 0), 50) / 50 * 50
    prs = min(member.get("pr_count", 0), 5) / 5 * 25
    issues = min(member.get("issue_count", 0), 3) / 3 * 15

    last_days = member.get("last_active_days")
    if last_days is None:
        recency = 0.0
    elif last_days <= 7:
        recency = 10.0
    elif last_days <= 14:
        recency = 5.0
    else:
        recency = 0.0

    return int(round(commits + prs + issues + recency))


def validate_token_available(token: str) -> bool:
    """Cheap pre-check: refuse empty or whitespace tokens before any call."""
    return bool(token and token.strip())
