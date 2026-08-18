"""
Diagnostic script: list the collaborators and pending invitations GitHub
reports for a repository.

This mirrors exactly what the Team Members page builds its member list from,
so it is the fastest way to see which users GitHub actually returns for:

    GET /repos/{owner}/{repo}/collaborators
    GET /repos/{owner}/{repo}/invitations

Usage:
    python scripts/check_collaborators.py
    python scripts/check_collaborators.py --owner manoj20027949-svg --repo devops
    python scripts/check_collaborators.py --token ghp_xxx --owner ... --repo ...

The token is read from --token, then GITHUB_TOKEN in the environment / .env.
It is never printed.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import settings
from utils.github_api import GitHubAPI, GitHubError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", default=settings.GITHUB_OWNER, help="Repository owner (default: GITHUB_OWNER)")
    parser.add_argument("--repo", default=settings.GITHUB_REPO, help="Repository name (default: GITHUB_REPO)")
    parser.add_argument("--token", default=settings.GITHUB_TOKEN, help="GitHub personal access token (default: GITHUB_TOKEN)")
    args = parser.parse_args()

    if not args.token:
        print(
            "No GitHub token available. Set GITHUB_TOKEN in .env, export it, "
            "or pass --token, then re-run.",
            file=sys.stderr,
        )
        return 2
    if not args.owner or not args.repo:
        print(
            "Owner/repo missing. Pass --owner and --repo, or set "
            "GITHUB_OWNER and GITHUB_REPO in .env.",
            file=sys.stderr,
        )
        return 2

    print(f"Repository: {args.owner}/{args.repo}")
    print(f"Token: {'<configured, not shown>' if args.token else '<none>'}")
    print("-" * 60)

    api = GitHubAPI(args.token)

    collaborators: list[dict] = []
    try:
        collaborators = api.get_repository_collaborators(args.owner, args.repo)
        print(f"Collaborators  (HTTP 200): {len(collaborators)}")
    except GitHubError as exc:
        print(
            f"Collaborators  FAILED (status={exc.status_code or 'n/a'}): "
            f"{exc.message}"
        )

    pending: list[dict] = []
    try:
        pending = api.get_pending_invitations(args.owner, args.repo)
        print(f"Pending invites (HTTP 200): {len(pending)}")
    except GitHubError as exc:
        print(
            f"Pending invites FAILED (status={exc.status_code or 'n/a'}): "
            f"{exc.message}"
        )

    if collaborators:
        print("-" * 60)
        print("Accepted collaborators:")
        for c in collaborators:
            print(
                f"  {c['username']:<28} role={c['role']:<10} "
                f"push={c['permissions'].get('push', False)} "
                f"pull={c['permissions'].get('pull', False)}"
            )

    if pending:
        print("-" * 60)
        print("Pending invitations (shown on the dashboard as 'Pending'):")
        for p in pending:
            print(
                f"  {p['username']:<28} role={p['role']:<16} "
                f"push={p['permissions'].get('push', False)} "
                f"pull={p['permissions'].get('pull', False)}"
            )

    if not collaborators and not pending:
        print("No collaborators or pending invitations returned.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
