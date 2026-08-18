"""
GitPulse - Team Reports service.

Builds a presentation-ready "view" for the Team Reports page from the
report produced by ``GitHubAPI.build_team_report``. All statistics come
from real GitHub data - nothing is hardcoded. Also provides:

* Date-range resolution (Today / 7 days / 30 days / This month / custom)
* Team summary + member performance rows
* Chart payloads (commits, PRs, activity score, weekly activity)
* Top contributors ranking
* Automatically calculated team insights
* AI / rule-based team performance summary
* CSV and PDF export of the report

CSV uses only the standard library. PDF is produced with a small
dependency-free PDF writer so the app never needs a heavy PDF package.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from config.logging_setup import get_logger
from config.settings import settings

logger = get_logger("reports")

# How many members make it into the "Top Contributors" ranking.
TOP_CONTRIBUTORS_LIMIT = 10


# ----------------------------------------------------------------------
# Date range resolution
# ----------------------------------------------------------------------
def _parse_iso(value: str) -> Optional[datetime]:
    """Parse an ISO datetime/date string to an aware UTC datetime or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_date(value: str) -> Optional[datetime]:
    """Parse a YYYY-MM-DD (or ISO) date to UTC midnight."""
    parsed = _parse_iso(value)
    if parsed is None:
        return None
    return parsed.replace(hour=0, minute=0, second=0, microsecond=0)


def iso_string(value: datetime) -> str:
    """Render a datetime as the ISO string the GitHub API accepts."""
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def resolve_range(
    period: str = "30d",
    from_str: str = "",
    to_str: str = "",
) -> dict[str, Any]:
    """
    Map a period preset (or custom date range) to a concrete window.

    Returns:
        {
            "period": normalized period name,
            "label": human-readable range label,
            "since": datetime (start, inclusive),
            "until": datetime (end, inclusive),
            "days": approximate number of days (for display),
        }
    """
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    period = (period or "30d").strip().lower()

    if period == "today":
        since, until = today, now
        label = "Today"
    elif period == "7d":
        since, until = now - timedelta(days=7), now
        label = "Last 7 Days"
    elif period == "month":
        since, until = today.replace(day=1), now
        label = "This Month"
    elif period == "custom":
        since = _parse_date(from_str) or (now - timedelta(days=30))
        until = _parse_date(to_str) or now
        if since > until:
            since, until = until, since
        # A date-only end means "through the end of that day".
        if until == until.replace(hour=0, minute=0, second=0, microsecond=0):
            until = until.replace(hour=23, minute=59, second=59, microsecond=0)
        label = "Custom Range"
    else:  # default: last 30 days
        period = "30d"
        since, until = now - timedelta(days=30), now
        label = "Last 30 Days"

    days = max(1, int((until - since).total_seconds() // 86400) + 1)
    return {
        "period": period,
        "label": label,
        "since": since,
        "until": until,
        "days": days,
    }


# ----------------------------------------------------------------------
# View builder
# ----------------------------------------------------------------------
def build_view(
    report: dict[str, Any],
    since: datetime,
    until: datetime,
    range_label: str,
    period: str,
    ai: Optional[dict] = None,
) -> dict[str, Any]:
    """Build the full Team Reports view model from a GitHub report."""
    members = list(report.get("members") or [])
    overview = report.get("overview") or {}
    pull_requests = list(report.get("pull_requests") or [])
    issues = list(report.get("issues") or [])

    rows = [_member_row(m) for m in members]
    rows.sort(key=lambda r: (r["activity_score"], r["commits"]), reverse=True)

    total_members = overview.get("members", len(rows)) or 0
    active = overview.get(
        "active_members", sum(1 for r in rows if r["is_active"])
    ) or 0
    inactive = overview.get(
        "inactive_members", total_members - active
    ) or 0
    total_commits = overview.get("total_commits", sum(r["commits"] for r in rows)) or 0
    total_prs = overview.get("total_prs", len(pull_requests)) or 0
    merged_prs = overview.get(
        "merged_prs", sum(1 for pr in pull_requests if pr.get("merged"))
    ) or 0
    open_issues = overview.get(
        "open_issues", sum(1 for i in issues if i.get("state") == "open")
    ) or 0
    code_reviews = sum(r["prs_reviewed"] for r in rows)

    summary = {
        "total_members": total_members,
        "active_members": active,
        "inactive_members": inactive,
        "total_commits": total_commits,
        "pull_requests": total_prs,
        "merged_pull_requests": merged_prs,
        "open_issues": open_issues,
        "code_reviews": code_reviews,
    }

    charts = {
        "commits": _member_chart(rows, "commits"),
        "prs": _member_chart(rows, "prs_created"),
        "score": _member_chart(rows, "activity_score"),
        "weekly": _weekly_activity(report, since, until),
    }

    top = sorted(rows, key=lambda r: r["commits"], reverse=True)[:TOP_CONTRIBUTORS_LIMIT]
    top_contributors = [
        {
            "rank": i + 1,
            "username": r["username"],
            "commits": r["commits"],
            "activity_score": r["activity_score"],
        }
        for i, r in enumerate(top)
    ]

    insights = _build_insights(rows, summary)

    return {
        "range": {
            "since": iso_string(since),
            "until": iso_string(until),
            "label": range_label,
            "period": period,
            "days": max(1, int((until - since).total_seconds() // 86400) + 1),
        },
        "repo": report.get("repo") or {},
        "summary": summary,
        "members": rows,
        "charts": charts,
        "top_contributors": top_contributors,
        "insights": insights,
        "ai": ai or {"summary": "", "engine": "rule-based", "available": False},
    }


def _member_row(m: dict[str, Any]) -> dict[str, Any]:
    """Normalize one member into the report table row shape."""
    return {
        "username": m.get("username", ""),
        "avatar": m.get("avatar", ""),
        "url": m.get("url", ""),
        "commits": m.get("commits", 0) or 0,
        "commits_all_time": m.get("commits_all_time", 0) or 0,
        "prs_created": m.get("prs_created", 0) or 0,
        "prs_merged": m.get("prs_merged", 0) or 0,
        "prs_reviewed": m.get("prs_reviewed", 0) or 0,
        "issues_created": m.get("issues_created", 0) or 0,
        "activity_score": m.get("activity_score", 0) or 0,
        "activity_status": m.get("activity_status") or "INACTIVE",
        "is_active": bool(m.get("is_active")),
        "last_active_text": m.get("last_active_text") or "No activity",
    }


def _member_chart(rows: list[dict], key: str) -> dict[str, list]:
    """Chart payload (labels + values) for one per-member metric."""
    return {
        "labels": [r["username"] for r in rows],
        "values": [r[key] for r in rows],
    }


def _weekly_activity(
    report: dict[str, Any], since: datetime, until: datetime
) -> dict[str, list]:
    """Bucket the activity feed by ISO week (Monday start) within the range."""
    buckets: dict[datetime.date, int] = {}
    for item in report.get("activity_feed") or []:
        parsed = _parse_iso(item.get("date"))
        if not parsed:
            continue
        if parsed < since or parsed > until:
            continue
        start = parsed.date() - timedelta(days=parsed.date().weekday())
        buckets[start] = buckets.get(start, 0) + 1

    ordered = sorted(buckets.items())
    return {
        "labels": [k.strftime("%b %d") for k, _ in ordered],
        "values": [v for _, v in ordered],
    }


def _build_insights(rows: list[dict], summary: dict[str, Any]) -> list[dict]:
    """Automatically calculated team insights (always data-driven)."""
    insights: list[dict[str, str]] = []

    if rows:
        most_active = max(rows, key=lambda r: r["activity_score"])
        insights.append(
            {
                "icon": "⚡",
                "title": "Most Active Member",
                "value": f"@{most_active['username']}",
                "detail": f"Activity score {most_active['activity_score']}/100",
            }
        )

        top_commit = max(rows, key=lambda r: r["commits"])
        insights.append(
            {
                "icon": "⌥",
                "title": "Highest Commit Contributor",
                "value": f"@{top_commit['username']}",
                "detail": f"{top_commit['commits']} commits",
            }
        )

        idle = [
            r["username"]
            for r in rows
            if r["commits"] == 0
            and r["prs_created"] == 0
            and r["issues_created"] == 0
        ]
        insights.append(
            {
                "icon": "◎",
                "title": "Members With No Activity",
                "value": str(len(idle)),
                "detail": (
                    ", ".join(f"@{u}" for u in idle[:5])
                    if idle
                    else "Everyone has activity"
                ),
            }
        )

        avg_commits = round(sum(r["commits"] for r in rows) / len(rows), 1)
        insights.append(
            {
                "icon": "⇄",
                "title": "Average Commits per Member",
                "value": str(avg_commits),
                "detail": f"Across {len(rows)} members",
            }
        )

    insights.append(
        {
            "icon": "⌥",
            "title": "Total Team Commits",
            "value": str(summary["total_commits"]),
            "detail": "In the selected period",
        }
    )
    insights.append(
        {
            "icon": "⇄",
            "title": "PR Activity",
            "value": str(summary["pull_requests"]),
            "detail": f"{summary['merged_pull_requests']} merged",
        }
    )
    insights.append(
        {
            "icon": "✎",
            "title": "Review Activity",
            "value": str(summary["code_reviews"]),
            "detail": "Pull request reviews",
        }
    )
    return insights


# ----------------------------------------------------------------------
# AI / rule-based team summary
# ----------------------------------------------------------------------
def rule_based_summary(view: dict[str, Any], range_label: str) -> str:
    """Deterministic one-to-two sentence team performance summary."""
    summary = view["summary"]
    if summary["total_members"] == 0:
        return "No team activity was detected in the selected period."

    parts = [
        f"The team made {summary['total_commits']} commits in the {range_label.lower()}."
    ]
    parts.append(f"{summary['active_members']} members were active.")

    top = view["top_contributors"][0] if view["top_contributors"] else None
    if top and top["commits"]:
        parts.append(
            f"@{top['username']} contributed the highest number of commits "
            f"({top['commits']})."
        )
    parts.append(
        f"{summary['merged_pull_requests']} pull requests were merged and "
        f"{summary['code_reviews']} reviews were submitted."
    )
    return " ".join(parts)


def build_ai_summary(
    view: dict[str, Any], range_label: str
) -> dict[str, Any]:
    """
    Produce the "AI Team Analysis" narrative.

    Returns a dict {summary, engine, available}. A rule-based summary is
    always produced so the section never looks broken. When Anthropic is
    configured a short AI narrative is requested and used on success.
    """
    narrative = rule_based_summary(view, range_label)

    if not settings.anthropic_configured:
        return {"summary": narrative, "engine": "rule-based", "available": False}

    try:
        from utils.ai_analyzer import ClaudeAnalyzer

        compact = [
            {
                "username": m["username"],
                "commits": m["commits"],
                "prs_created": m["prs_created"],
                "prs_merged": m["prs_merged"],
                "reviews": m["prs_reviewed"],
                "issues": m["issues_created"],
                "activity_score": m["activity_score"],
                "status": m["activity_status"],
            }
            for m in view["members"][:20]
        ]
        prompt = (
            "You are a senior engineering manager. Based ONLY on the GitHub "
            "metrics below, write a short team-performance summary in 2-3 "
            "sentences. Mention total commits, how many members were active, "
            "and the top contributor. Be concrete and constructive. No markdown.\n"
            f"Period: {range_label}\n"
            f"Summary: {view['summary']}\n"
            f"Members: {compact}\n"
        )
        text = ClaudeAnalyzer().chat(
            system=(
                "You are a senior engineering manager writing a concise team "
                "performance summary. Reply with plain text only."
            ),
            prompt=prompt,
            max_tokens=300,
        )
    except Exception as exc:  # noqa: BLE001 - always fall back
        logger.warning("AI team summary unavailable: %s", exc)
        text = None

    if text and text.strip():
        return {"summary": text.strip(), "engine": "ai", "available": True}
    return {"summary": narrative, "engine": "rule-based", "available": True}


# ----------------------------------------------------------------------
# Exports
# ----------------------------------------------------------------------
def to_csv(view: dict[str, Any]) -> str:
    """Serialize the report view to CSV (utf-8-sig for Excel friendliness)."""
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    rng = view["range"]
    repo_name = (view.get("repo") or {}).get("name", "")

    writer.writerow(["GitPulse Team Report", repo_name])
    writer.writerow(["Period", rng["label"], "From", rng["since"], "To", rng["until"]])
    writer.writerow([])
    writer.writerow(["Summary", "Value"])
    for key, value in view["summary"].items():
        writer.writerow([key.replace("_", " ").title(), value])
    writer.writerow([])
    writer.writerow(
        [
            "Member", "Commits", "Pull Requests", "Merged PRs", "Reviews",
            "Issues", "Activity Score", "Status",
        ]
    )
    for row in view["members"]:
        writer.writerow(
            [
                row["username"], row["commits"], row["prs_created"],
                row["prs_merged"], row["prs_reviewed"], row["issues_created"],
                row["activity_score"], row["activity_status"],
            ]
        )
    writer.writerow([])
    writer.writerow(["Top Contributors"])
    writer.writerow(["Rank", "Username", "Commits", "Activity Score"])
    for row in view["top_contributors"]:
        writer.writerow(
            [row["rank"], row["username"], row["commits"], row["activity_score"]]
        )
    writer.writerow([])
    writer.writerow(["Insights"])
    for insight in view["insights"]:
        writer.writerow([insight["title"], insight["value"], insight["detail"]])
    writer.writerow([])
    writer.writerow(["Team Analysis"])
    writer.writerow([view["ai"].get("summary", "")])
    return buf.getvalue()


def to_pdf(view: dict[str, Any]) -> bytes:
    """Serialize the report view to a valid PDF (no external libraries)."""
    from utils.pdf_export import report_to_pdf

    return report_to_pdf(view)
