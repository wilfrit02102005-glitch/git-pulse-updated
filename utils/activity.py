"""
GitPulse - team activity analysis.

Computes per-member activity from raw GitHub metrics (commits, PRs,
reviews, issues) using configurable weights and thresholds:

    * Activity status:  ACTIVE | RECENTLY ACTIVE | INACTIVE
    * Activity score:   0-100 weighted score
    * Activity label:   Highly Active | Active | Low Activity | Inactive
    * Score reason:     a human-readable explanation of the score

Everything is a pure function of the inputs so it is easy to test.
"""

from __future__ import annotations

from typing import Any, Optional

from config.settings import settings

# Capping values keep a single power-user from dominating a score.
CAP_COMMITS = 30
CAP_PRS = 10
CAP_REVIEWS = 10
CAP_ISSUES = 10


def _weighted(capped_ratio: float, weight: int) -> float:
    """Scale a 0-1 ratio by its configured weight."""
    return max(0.0, min(1.0, capped_ratio)) * weight


def compute_activity_score(
    commits: int = 0,
    prs: int = 0,
    reviews: int = 0,
    issues: int = 0,
    weights: Optional[dict[str, int]] = None,
) -> int:
    """
    Weighted 0-100 activity score.

    Default weights: commits 40%, PRs 25%, reviews 20%, issues 15%.
    Each metric is capped so the score stays comparable between members.
    """
    weights = weights or settings.SCORE_WEIGHTS
    score = (
        _weighted(min(commits, CAP_COMMITS) / CAP_COMMITS, weights.get("commits", 40))
        + _weighted(min(prs, CAP_PRS) / CAP_PRS, weights.get("prs", 25))
        + _weighted(min(reviews, CAP_REVIEWS) / CAP_REVIEWS, weights.get("reviews", 20))
        + _weighted(min(issues, CAP_ISSUES) / CAP_ISSUES, weights.get("issues", 15))
    )
    return int(round(score))


def activity_status(last_active_days: Optional[int]) -> str:
    """
    Classify a member as ACTIVE, RECENTLY ACTIVE or INACTIVE based on
    how many days ago their most recent activity was.
    """
    if last_active_days is None:
        return "INACTIVE"
    if last_active_days <= settings.RECENTLY_ACTIVE_DAYS:
        return "ACTIVE"
    if last_active_days <= settings.INACTIVE_DAYS:
        return "RECENTLY ACTIVE"
    return "INACTIVE"


def activity_label(score: int, thresholds: Optional[dict[str, int]] = None) -> str:
    """Map a 0-100 score to a descriptive label."""
    thresholds = thresholds or settings.ACTIVITY_THRESHOLDS
    if score >= thresholds.get("highly_active", 80):
        return "Highly Active"
    if score >= thresholds.get("active", 60):
        return "Active"
    if score >= thresholds.get("low", 30):
        return "Low Activity"
    return "Inactive"


def score_reason(
    username: str,
    commits: int,
    prs: int,
    reviews: int,
    issues: int,
) -> str:
    """Human-readable explanation of the activity score."""
    parts = []
    if commits:
        parts.append(f"{commits} commit{'s' if commits != 1 else ''}")
    if prs:
        parts.append(f"{prs} PR{'s' if prs != 1 else ''}")
    if reviews:
        parts.append(f"{reviews} review{'s' if reviews != 1 else ''}")
    if issues:
        parts.append(f"{issues} issue{'s' if issues != 1 else ''}")
    if not parts:
        return f"{username} has no recorded activity in the selected period."
    return f"High activity because of {', '.join(parts)} in the selected period."


def last_active_text(last_active_days: Optional[int]) -> str:
    """A friendly label for the member's most recent activity."""
    if last_active_days is None:
        return "No activity"
    if last_active_days == 0:
        return "Today"
    if last_active_days == 1:
        return "Yesterday"
    return f"{last_active_days} days ago"


def enrich_member(member: dict[str, Any]) -> dict[str, Any]:
    """
    Add activity_score, activity_status, activity_label, score_reason,
    is_active and last_active_text to a member dict. Expects the raw
    metrics to already be present.
    """
    username = member.get("username", "unknown")
    commits = member.get("commits", 0) or 0
    prs = (member.get("prs_created", 0) or 0) + (member.get("prs_merged", 0) or 0)
    reviews = member.get("prs_reviewed", 0) or 0
    issues = (member.get("issues_created", 0) or 0) + (member.get("issues_closed", 0) or 0)

    score = compute_activity_score(commits, prs, reviews, issues)
    member["activity_score"] = score
    member["activity_label"] = activity_label(score)
    member["activity_status"] = activity_status(member.get("last_active_days"))
    member["is_active"] = member["activity_status"] == "ACTIVE"
    member["last_active_text"] = last_active_text(member.get("last_active_days"))
    member["score_reason"] = score_reason(username, commits, prs, reviews, issues)
    return member
