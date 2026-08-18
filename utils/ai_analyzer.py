"""
GitPulse - AI coaching engine.

Two sources of suggestions:

1. Rule-based engine (always available, zero dependencies). It derives
   coaching from commit frequency, inactivity, PR count and issue
   participation using transparent heuristics.

2. Claude (Anthropic) engine (optional). When `ANTHROPIC_API_KEY` is set,
   developer metrics are sent to Claude and it returns personalized
   coaching as JSON. Failures fall back to the rule-based suggestions so
   the dashboard always has content to show.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from config.logging_setup import get_logger
from config.settings import settings

logger = get_logger("app")

# Time-to-live for the in-memory suggestion cache (seconds).
CACHE_TTL_SECONDS = 600

# Maximum number of characters sent to the AI per file.
MAX_CODE_CHARS = 8000


@dataclass
class CoachingSuggestion:
    """A single coaching recommendation for one developer."""

    member: str
    category: str
    summary: str
    detail: str
    priority: str  # HIGH | MEDIUM | LOW

    def to_dict(self) -> dict[str, str]:
        return {
            "member": self.member,
            "category": self.category,
            "summary": self.summary,
            "detail": self.detail,
            "priority": self.priority,
        }


# ----------------------------------------------------------------------
# Rule-based engine
# ----------------------------------------------------------------------
class RuleBasedAnalyzer:
    """Generates deterministic coaching suggestions from metrics."""

    def analyze(self, members: list[dict[str, Any]]) -> list[CoachingSuggestion]:
        suggestions: list[CoachingSuggestion] = []
        for member in members:
            suggestions.extend(self._suggest_for(member))
        # Most important first.
        priority_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        suggestions.sort(key=lambda s: priority_order.get(s.priority, 9))
        return suggestions

    def _suggest_for(self, member: dict[str, Any]) -> list[CoachingSuggestion]:
        """Build coaching suggestions for one developer."""
        suggestions: list[CoachingSuggestion] = []
        username = member.get("username", "unknown")
        commits = member.get("commits", 0)
        prs = member.get("pr_count", 0)
        issues = member.get("issue_count", 0)
        inactive_days = member.get("last_active_days")

        # --- Inactive developer ---
        if inactive_days is not None and inactive_days > 14:
            suggestions.append(
                CoachingSuggestion(
                    member=username,
                    category="Inactivity",
                    summary=f"{username} has been inactive for {inactive_days} days.",
                    detail=(
                        f"No commits detected in the last {inactive_days} days. "
                        "Check in with them about blockers, vacation, or role changes."
                    ),
                    priority="HIGH",
                )
            )

        # --- Low commit frequency ---
        if commits == 0:
            suggestions.append(
                CoachingSuggestion(
                    member=username,
                    category="Commit Activity",
                    summary=f"{username} has no commits in the analyzed window.",
                    detail=(
                        "Zero commits recorded. Verify they are tracked with the correct "
                        "email in git config, or investigate engagement."
                    ),
                    priority="MEDIUM",
                )
            )
        elif commits < 10:
            suggestions.append(
                CoachingSuggestion(
                    member=username,
                    category="Commit Activity",
                    summary=f"{username} shows low commit frequency ({commits} commits).",
                    detail=(
                        "Fewer than 10 commits in 90 days suggests shallow engagement. "
                        "Encourage smaller, more frequent merges and weekly check-ins."
                    ),
                    priority="MEDIUM",
                )
            )

        # --- Review / PR participation ---
        if prs == 0 and commits > 0:
            suggestions.append(
                CoachingSuggestion(
                    member=username,
                    category="Code Review",
                    summary=f"{username} has no open pull requests.",
                    detail=(
                        "They are committing but not opening PRs. Confirm their branch "
                        "strategy and encourage early PRs for visibility."
                    ),
                    priority="LOW",
                )
            )

        # --- Issue engagement ---
        if issues == 0 and commits > 0:
            suggestions.append(
                CoachingSuggestion(
                    member=username,
                    category="Issue Participation",
                    summary=f"{username} has no open issues assigned.",
                    detail=(
                        "No issue participation detected. Encourage triage duty and "
                        "pairing on bug-fixes to broaden context."
                    ),
                    priority="LOW",
                )
            )

        # --- High performer (recognition) ---
        if commits >= 30 and inactive_days is not None and inactive_days <= 7:
            suggestions.append(
                CoachingSuggestion(
                    member=username,
                    category="Recognition",
                    summary=f"{username} is a high performer ({commits} commits).",
                    detail=(
                        "Strong sustained output. Consider recognizing them publicly and "
                        "checking they are not at burnout risk."
                    ),
                    priority="LOW",
                )
            )

        return suggestions


# ----------------------------------------------------------------------
# Claude (Anthropic) engine
# ----------------------------------------------------------------------
class ClaudeAnalyzer:
    """Sends developer metrics to Claude and parses coaching JSON."""

    def __init__(self) -> None:
        import anthropic  # imported lazily so the fallback works without the SDK

        self.client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        self.model = settings.ANTHROPIC_MODEL

    def chat(self, system: str, prompt: str, max_tokens: int = 2000) -> Optional[str]:
        """
        Send a single chat turn to Claude and return the text response.

        Returns None on any API failure (network, auth, malformed model).
        The caller decides how to fall back.
        """
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=0.4,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(
                block.text for block in response.content if getattr(block, "type", "") == "text"
            )
            return text.strip() or None
        except Exception as exc:  # noqa: BLE001 - fall back on any API error
            logger.warning("Claude request failed: %s", exc)
            return None

    def _build_prompt(self, members: list[dict[str, Any]]) -> str:
        """Create a strict, self-contained prompt for Claude."""
        compact = [
            {
                "username": m.get("username"),
                "commits_90d": m.get("commits"),
                "open_prs": m.get("pr_count"),
                "open_issues": m.get("issue_count"),
                "days_since_last_commit": m.get("last_active_days"),
                "activity_score": m.get("activity_score"),
            }
            for m in members
        ]
        return (
            "You are a senior engineering manager coach. Based ONLY on the GitHub "
            "metrics below, write short, actionable coaching suggestions.\n"
            "Respond with a JSON array only, no markdown, in this exact shape:\n"
            '{"suggestions": [{"member": "<username>", "category": "<one of: '
            'Commit Activity|Inactivity|Code Review|Issue Participation|Recognition|'
            'General>", "summary": "<one short sentence>", '
            '"detail": "<1-2 sentences, actionable>", "priority": "HIGH|MEDIUM|LOW"}]}\n'
            "Metrics:\n"
            + json.dumps(compact)
        )

    def analyze(self, members: list[dict[str, Any]]) -> list[CoachingSuggestion]:
        """Ask Claude for coaching; returns an empty list on any failure."""
        if not members:
            return []

        text = self.chat(
            system=(
                "You are a senior engineering manager coach. Respond with JSON only."
            ),
            prompt=self._build_prompt(members),
        )
        if not text:
            return []
        return self._parse_response(text)

    @staticmethod
    def _parse_response(text: str) -> list[CoachingSuggestion]:
        """Defensively parse Claude's JSON array response."""
        text = text.strip()
        # Strip markdown code fences if the model wrapped the JSON.
        if text.startswith("```"):
            text = text.strip("`")
            first_brace = text.find("{")
            last_brace = text.rfind("}")
            if first_brace != -1 and last_brace != -1:
                text = text[first_brace : last_brace + 1]

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return []

        raw_items = payload.get("suggestions", []) if isinstance(payload, dict) else payload
        if not isinstance(raw_items, list):
            return []

        suggestions: list[CoachingSuggestion] = []
        for item in raw_items:
            if not isinstance(item, dict) or not item.get("member"):
                continue
            suggestions.append(
                CoachingSuggestion(
                    member=str(item.get("member")),
                    category=str(item.get("category", "General")),
                    summary=str(item.get("summary", "")),
                    detail=str(item.get("detail", "")),
                    priority=str(item.get("priority", "LOW")).upper(),
                )
            )
        return suggestions


# ----------------------------------------------------------------------
# Facade with caching
# ----------------------------------------------------------------------
_cache: dict[str, tuple[float, list[CoachingSuggestion]]] = {}


def generate_suggestions(
    members: list[dict[str, Any]],
    use_ai: bool = True,
) -> list[CoachingSuggestion]:
    """
    Generate coaching suggestions for the whole team.

    Strategy:
      1. Rule-based suggestions are ALWAYS produced (guaranteed output).
      2. If Claude is configured AND `use_ai` is True, ask Claude and merge
         its results in front of the rule-based ones.
      3. Results are cached in-memory for CACHE_TTL_SECONDS to avoid
         burning API credits on every page load.

    Returns:
        A list of CoachingSuggestion objects.
    """
    cache_key = hashlib.sha256(json.dumps(members, default=str).encode()).hexdigest()

    cached = _cache.get(cache_key)
    if cached and (time.time() - cached[0]) < CACHE_TTL_SECONDS:
        logger.debug("Serving coaching suggestions from cache.")
        return cached[1]

    rule_based = RuleBasedAnalyzer().analyze(members)

    ai_suggestions: list[CoachingSuggestion] = []
    if use_ai and settings.anthropic_configured:
        ai_suggestions = ClaudeAnalyzer().analyze(members)
        if ai_suggestions:
            logger.info("Claude produced %d coaching suggestions.", len(ai_suggestions))
        else:
            logger.info("Claude returned nothing; keeping rule-based suggestions.")

    merged = ai_suggestions + rule_based
    _cache[cache_key] = (time.time(), merged)
    return merged


# ======================================================================
# AI code / PR / issue analysis (AI Error Detection + Auto-Fix input)
# ======================================================================

def _extract_json(text: str) -> Any:
    """
    Defensively extract a JSON payload from a Claude response.

    Handles markdown code fences and stray prose around the JSON object.
    Returns None when no valid JSON can be found.
    """
    if not text:
        return None
    text = text.strip()
    # Strip a ```json ... ``` fence if present.
    if text.startswith("```"):
        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            text = text[first_brace : last_brace + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Last resort: pull the first balanced {...} block out of the text.
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    return None
    return None


def _ai_result_or_none(prompt: str, system: str) -> Optional[dict]:
    """Ask Claude for a JSON dict; returns None on any failure."""
    if not settings.anthropic_configured:
        return None
    text = ClaudeAnalyzer().chat(system=system, prompt=prompt, max_tokens=3000)
    payload = _extract_json(text) if text else None
    if isinstance(payload, dict):
        return payload
    return None


# ----------------------------------------------------------------------
# Rule-based code analysis fallback (uses the existing regex scanner)
# ----------------------------------------------------------------------
def rule_based_code_analysis(filename: str, content: str) -> dict:
    """
    Analyze a single file with the existing regex CodeScanner and format
    the worst finding into the AI result shape so the frontend and the
    fix workflow can always consume a uniform dict.
    """
    from utils.code_scanner import CodeScanner
    from utils.code_scanner import (
        SCANNABLE_EXTENSIONS,
        _rules_for,
        _scan_content,
        analyze_python_content,
    )

    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext not in SCANNABLE_EXTENSIONS:
        return {
            "severity": "low",
            "file": filename,
            "line": 0,
            "error_type": "none",
            "problem": "No obvious issue detected by the rule-based scanner.",
            "explanation": "The file was scanned with the built-in regex rules.",
            "suggested_fix": "",
            "fixed_code": None,
            "engine": "rule-based",
        }

    findings = _scan_content(filename, content, _rules_for(ext))
    if ext == ".py":
        # Deterministic checks (syntax errors, undefined names, unused imports).
        findings.extend(analyze_python_content(filename, content))
    findings.sort(key=CodeScanner.severity_sort_key)
    if not findings:
        return {
            "severity": "low",
            "file": filename,
            "line": 0,
            "error_type": "none",
            "problem": "No obvious issue detected by the rule-based scanner.",
            "explanation": "The file was scanned with the built-in regex rules.",
            "suggested_fix": "",
            "fixed_code": None,
            "engine": "rule-based",
        }
    top = findings[0]
    return {
        "severity": top.severity.lower(),
        "file": top.filename,
        "line": top.line_number,
        "error_type": top.rule_id,
        "problem": top.description,
        "explanation": top.description,
        "suggested_fix": top.recommendation,
        "fixed_code": None,
        "engine": "rule-based",
    }


# ----------------------------------------------------------------------
# Public analysis entry points
# ----------------------------------------------------------------------
def analyze_code(filename: str, content: str, context: str = "") -> dict:
    """
    AI-powered analysis of a single code file.

    Returns a dict with keys:
        severity, file, line, error_type, problem, explanation,
        suggested_fix, fixed_code, engine

    Falls back to the rule-based scanner when Anthropic is unavailable or
    returns something unusable, so callers always get a dict.
    """
    fallback = rule_based_code_analysis(filename, content)

    if not settings.anthropic_configured:
        return fallback

    prompt = (
        "Analyze the following source file for bugs, syntax errors, runtime "
        "errors, logic errors, security issues, code quality problems and "
        "missing error handling.\n"
        f"Context: {context}\n"
        f"File: {filename}\n"
        "```\n"
        f"{content[:MAX_CODE_CHARS]}\n"
        "```\n"
        "Respond with a single JSON object only, no markdown:\n"
        '{"severity": "high|medium|low", "file": "<path>", "line": <int>, '
        '"error_type": "<short label>", "problem": "<one line>", '
        '"explanation": "<1-2 sentences>", "suggested_fix": "<description>", '
        '"fixed_code": "<complete corrected file content or empty string>"}\n'
        "If there is nothing wrong, set severity to \"low\", error_type to "
        "\"none\" and fixed_code to an empty string."
    )
    payload = _ai_result_or_none(
        prompt,
        system=(
            "You are a senior code reviewer and static-analysis engineer. "
            "Only output the requested JSON object."
        ),
    )
    if not payload:
        return fallback

    result = {
        "severity": str(payload.get("severity", fallback.get("severity", "low"))).lower(),
        "file": str(payload.get("file") or filename),
        "line": int(payload.get("line") or 0),
        "error_type": str(payload.get("error_type") or "unknown"),
        "problem": str(payload.get("problem") or fallback.get("problem", "")),
        "explanation": str(payload.get("explanation") or ""),
        "suggested_fix": str(payload.get("suggested_fix") or ""),
        "fixed_code": payload.get("fixed_code") or "",
        "engine": "ai",
    }
    return result


def analyze_pull_request(pr: dict, diff: str = "") -> dict:
    """
    AI analysis of a pull request: quality, bugs, security, complexity,
    and suggested improvements. Falls back to a rule-based summary.
    """
    fallback = {
        "severity": "low",
        "problem": "No AI analysis available.",
        "explanation": "ANTHROPIC_API_KEY is not configured; showing a rule-based summary.",
        "suggestions": [],
        "engine": "rule-based",
    }

    if not settings.anthropic_configured:
        return fallback

    prompt = (
        "Analyze this pull request. Provide code quality notes, potential "
        "bugs, security concerns, complexity assessment and suggested "
        "improvements. Be constructive.\n"
        f"Title: {pr.get('title', '')}\n"
        f"Body: {pr.get('body', '')[:1000]}\n"
        f"Changed files: {pr.get('changed_files', 0)} | "
        f"Additions: {pr.get('additions', 0)} | Deletions: {pr.get('deletions', 0)}\n"
        "Diff (truncated):\n"
        f"```\n{diff[:MAX_CODE_CHARS]}\n```\n"
        "Respond with a single JSON object:\n"
        '{"severity": "high|medium|low", "problem": "<one line>", '
        '"explanation": "<1-2 sentences>", '
        '"suggestions": ["<suggestion 1>", "<suggestion 2>"]}'
    )
    payload = _ai_result_or_none(
        prompt,
        system=(
            "You are a senior pull-request reviewer. Only output the requested JSON object."
        ),
    )
    if not payload:
        return fallback
    suggestions = payload.get("suggestions")
    if not isinstance(suggestions, list):
        suggestions = []
    return {
        "severity": str(payload.get("severity", "medium")).lower(),
        "problem": str(payload.get("problem", "")),
        "explanation": str(payload.get("explanation", "")),
        "suggestions": [str(s) for s in suggestions],
        "engine": "ai",
    }


def analyze_issue(issue: dict) -> dict:
    """
    AI analysis of a GitHub issue: summary, likely root cause, suggested
    solution, related files, and implementation steps.
    """
    fallback = {
        "severity": "medium",
        "summary": str(issue.get("title", "")),
        "root_cause": "No AI analysis available.",
        "solution": "Review the issue and reproduce before fixing.",
        "related_files": [],
        "steps": [],
        "engine": "rule-based",
    }

    if not settings.anthropic_configured:
        return fallback

    prompt = (
        "Analyze this GitHub issue and help a developer triage it.\n"
        f"Title: {issue.get('title', '')}\n"
        f"Body: {issue.get('body', '')[:2000]}\n"
        f"Labels: {', '.join(issue.get('labels', []))}\n"
        "Respond with a single JSON object:\n"
        '{"severity": "high|medium|low", "summary": "<one line>", '
        '"root_cause": "<likely root cause>", "solution": "<suggested solution>", '
        '"related_files": ["<file>", ...], "steps": ["<step 1>", "<step 2>", ...]}'
    )
    payload = _ai_result_or_none(
        prompt,
        system=(
            "You are a senior software engineer triaging GitHub issues. "
            "Only output the requested JSON object."
        ),
    )
    if not payload:
        return fallback

    def _as_list(key: str) -> list[str]:
        value = payload.get(key, [])
        return [str(v) for v in value] if isinstance(value, list) else []

    return {
        "severity": str(payload.get("severity", "medium")).lower(),
        "summary": str(payload.get("summary") or issue.get("title", "")),
        "root_cause": str(payload.get("root_cause", "")),
        "solution": str(payload.get("solution", "")),
        "related_files": _as_list("related_files"),
        "steps": _as_list("steps"),
        "engine": "ai",
    }


# ----------------------------------------------------------------------
# Repository-level analysis (AI Error Detection / Fix recommendations)
# ----------------------------------------------------------------------
def analyze_repository(report: dict) -> dict:
    """
    Rule-based health analysis for the whole repository. Always available
    (zero dependencies) and produces a uniform list of findings plus an
    overall health score. Used by the AI Analysis and AI Fixes tabs.
    """
    findings: list[dict] = []
    overview = report.get("overview") or {}
    members = report.get("members") or []
    pushes = report.get("pushes") or []
    pull_requests = report.get("pull_requests") or []
    issues = report.get("issues") or []

    def add(severity, category, title, explanation, recommendation, affected=""):
        findings.append(
            {
                "severity": severity,
                "category": category,
                "title": title,
                "explanation": explanation,
                "recommendation": recommendation,
                "affected": affected,
            }
        )

    total_members = overview.get("members", len(members)) or 0
    inactive = overview.get("inactive_members", 0) or 0

    # --- Team activity health ---
    if total_members and inactive:
        ratio = inactive / total_members
        if ratio >= 0.5:
            add(
                "high",
                "Team Activity",
                f"{inactive} of {total_members} members are inactive",
                (
                    f"{ratio:.0%} of the team has no activity in the last "
                    f"{settings.ACTIVITY_WINDOW_DAYS} days. Risk of knowledge loss "
                    "and uneven workload."
                ),
                "Reach out to inactive members, check for blocked or unassigned work, "
                "and balance the backlog across the team.",
                affected="team",
            )
        elif ratio >= 0.25:
            add(
                "medium",
                "Team Activity",
                f"{inactive} of {total_members} members are inactive",
                (
                    "A notable share of the team has no commits in the analyzed window."
                ),
                "Verify git identity configuration and confirm those members are not blocked.",
                affected="team",
            )

    for member in members:
        inactive_days = member.get("last_active_days")
        if inactive_days is not None and inactive_days > 14:
            add(
                "high",
                "Team Activity",
                f"{member.get('username')} inactive for {inactive_days} days",
                (
                    f"No activity from {member.get('username')} for {inactive_days} days "
                    "while the rest of the team is committing."
                ),
                "Check in about blockers, PTO, or whether they need reassigned work.",
                affected=member.get("username", ""),
            )
        if member.get("commits", 0) >= 5 and member.get("pr_count", 0) == 0:
            add(
                "low",
                "Code Review",
                f"{member.get('username')} commits but never opens PRs",
                "Commits directly without pull requests, so changes bypass review.",
                "Encourage opening early PRs so the team can review as you build.",
                affected=member.get("username", ""),
            )

    # --- Pull request hygiene ---
    merged = sum(1 for pr in pull_requests if pr.get("merged"))
    closed_unmerged = sum(
        1 for pr in pull_requests if pr.get("state") == "closed" and not pr.get("merged")
    )
    open_prs = sum(1 for pr in pull_requests if pr.get("state") == "open")
    total_prs = overview.get("total_prs", len(pull_requests)) or 0

    if total_prs and merged and open_prs == 0 and closed_unmerged == 0:
        add(
            "low",
            "Pull Requests",
            "All pull requests are merged",
            "Healthy flow: every PR reviewed and merged.",
            "Keep the review process consistent as the team grows.",
            affected="repository",
        )
    elif total_prs and open_prs / total_prs >= 0.5:
        add(
            "medium",
            "Pull Requests",
            f"{open_prs} of {total_prs} pull requests are still open",
            "Half or more of the PRs are open, which can indicate stalled reviews.",
            "Set a review SLA and explicitly close stale PRs.",
            affected="repository",
        )

    # --- Issue backlog ---
    open_issues = overview.get("open_issues", 0) or 0
    if open_issues >= 5:
        add(
            "medium",
            "Issues",
            f"{open_issues} open issues",
            "A growing backlog of open issues with no visible triage.",
            "Schedule triage and label issues by priority and area.",
            affected="repository",
        )

    # --- Commit quality (from pushes detail) ---
    large_commits = [p for p in pushes if len(p.get("files") or []) > 10]
    if len(large_commits) > 1:
        add(
            "medium",
            "Code Quality",
            f"{len(large_commits)} oversized commits ({len(large_commits[0].get('files') or [])}+ files)",
            "Large multi-file commits make reviews and rollbacks harder.",
            "Encourage smaller, single-purpose commits.",
            affected="repository",
        )

    touched = [f.get("filename", "") for p in pushes for f in (p.get("files") or [])]
    has_code = [f for f in touched if not f.startswith(("test", "tests"))]
    has_tests = [f for f in touched if "test" in f.lower()]
    if has_code and not has_tests:
        add(
            "low",
            "Code Quality",
            "No test files touched in recent commits",
            "Recent commits changed source code but no test files.",
            "Add or update tests alongside feature work.",
            affected="repository",
        )

    # --- Bus factor / contributor balance ---
    contributors = report.get("contributors") or []
    if contributors:
        sorted_contrib = sorted(
            contributors, key=lambda c: c.get("contributions", 0), reverse=True
        )
        top = sorted_contrib[0]
        total_contrib = sum(c.get("contributions", 0) for c in sorted_contrib)
        if total_contrib and top.get("contributions", 0) / total_contrib >= 0.7:
            add(
                "medium",
                "Bus Factor",
                f"{top.get('username')} contributes {top.get('contributions', 0)} of {total_contrib} commits",
                "A single contributor is responsible for most of the code, creating a bus factor risk.",
                "Pair the top contributor with teammates and rotate ownership of critical modules.",
                affected=top.get("username", ""),
            )

    # --- Health score ---
    severity_penalty = {"critical": 25, "high": 15, "medium": 8, "low": 3}
    health_score = max(
        0, 100 - sum(severity_penalty.get(f["severity"], 3) for f in findings)
    )
    if not findings:
        health_score = 100

    return {
        "health_score": health_score,
        "health_label": _health_label(health_score),
        "summary": _health_summary(health_score, len(findings)),
        "findings": findings,
        "engine": "rule-based",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _health_label(score: int) -> str:
    """Map a 0-100 health score to a human-readable rating."""
    if score >= 90:
        return "Excellent"
    if score >= 70:
        return "Good"
    if score >= 45:
        return "Needs Attention"
    return "Critical"


def _health_summary(score: int, finding_count: int) -> str:
    if score >= 90:
        return "Repository is in excellent health. Keep up the good review and commit hygiene."
    if score >= 70:
        return (
            "Repository is generally healthy with a few areas worth attention "
            f"({finding_count} finding{'s' if finding_count != 1 else ''})."
        )
    if score >= 45:
        return (
            f"Repository shows {finding_count} findings that need attention before they become problems."
        )
    return (
        f"Repository health is at risk ({finding_count} findings). Prioritize the high-severity items."
    )


# ----------------------------------------------------------------------
# Commit-level analysis (risk classification)
# ----------------------------------------------------------------------

# Keywords that hint a commit might be risky / bug-prone.
_RISKY_MESSAGE_TOKENS = (
    "revert",
    "hotfix",
    "temp",
    "temporary",
    "hack",
    "wip",
    "asap",
    "urgent",
    "quick fix",
    "workaround",
    "broken",
    "fix later",
)
_BUG_PRONE_MESSAGE_TOKENS = (
    "fix",
    "bug",
    "crash",
    "error",
    "failing",
    "debug",
    "typo",
    "regression",
)
# File basenames / substrings that make a change suspicious (secrets, config).
_SUSPICIOUS_FILE_TOKENS = (
    ".env",
    "secret",
    "password",
    "credential",
    "token",
    "api_key",
    "apikey",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    "id_rsa",
    "htpasswd",
    "npmrc",
    "netrc",
    "settings.local",
)


def _commit_files(commit: dict) -> list[dict]:
    """Normalize the file list of a commit regardless of source shape."""
    files = commit.get("files") or []
    if not files and commit.get("detail"):
        files = commit["detail"].get("files") or []
    return [f for f in files if isinstance(f, dict)]


def _commit_stats(commit: dict) -> tuple[int, int]:
    """Return (additions, deletions) for a commit."""
    stats = commit.get("stats")
    if isinstance(stats, dict):
        return int(stats.get("additions") or 0), int(stats.get("deletions") or 0)
    additions = sum(int(f.get("additions") or 0) for f in _commit_files(commit))
    deletions = sum(int(f.get("deletions") or 0) for f in _commit_files(commit))
    return additions, deletions


def _commit_message(commit: dict) -> str:
    """Return the full commit message (first line for short usage)."""
    message = commit.get("message") or (commit.get("detail") or {}).get("message") or ""
    full = commit.get("full_message") or message
    return str(message), str(full)


def _explain(lines: list[str]) -> str:
    """Join the plain-language explanations for a commit into one sentence."""
    return " ".join(dict.fromkeys(l for l in lines if l))


def _flags_for_commit(commit: dict) -> list[tuple[str, str]]:
    """
    Deterministic per-commit checks. Returns (flag_id, plain explanation)
    pairs, lowest risk first, so the caller can pick the highest-priority
    classification.
    """
    first_line, full_message = _commit_message(commit)
    lowered = f"{first_line}\n{full_message}".lower()
    files = _commit_files(commit)
    additions, deletions = _commit_stats(commit)
    total_changes = additions + deletions
    flags: list[tuple[str, str]] = []

    # --- Suspicious: secrets / config files or obvious security red flags.
    suspicious_files = [
        f.get("filename", "") for f in files
        if any(tok in (f.get("filename", "") or "").lower() for tok in _SUSPICIOUS_FILE_TOKENS)
    ]
    if suspicious_files:
        flags.append(
            (
                "suspicious",
                f"Commit touches files that may hold secrets or local config "
                f"({', '.join(sorted(suspicious_files)[:3])}). Review carefully "
                "to ensure no credentials were committed.",
            )
        )
    if any(tok in lowered for tok in ("password", "apikey", "api_key", "secret", "token")):
        flags.append(
            (
                "suspicious",
                "The commit message mentions secrets or credentials; verify no "
                "sensitive value was hard-coded.",
            )
        )

    # --- Bug-prone: fix/regression keywords combined with real code changes.
    if any(tok in lowered for tok in _BUG_PRONE_MESSAGE_TOKENS) and total_changes > 0:
        flags.append(
            (
                "bug-prone",
                "The commit message suggests bug-fixing work; check the change "
                "handles edge cases and includes a regression test.",
            )
        )

    # --- Risky: large removals, force-style changes, destructive signals.
    if deletions > 0 and additions > 0 and deletions > additions * 2:
        flags.append(
            (
                "risky",
                f"Large deletion ratio ({additions}+ / {deletions}-): most of the "
                "change removes code, which can break callers that still rely on it.",
            )
        )
    if any(tok in lowered for tok in ("revert", "force push", "delete", "remove")):
        flags.append(
            (
                "risky",
                "Commit message signals a revert or removal; confirm the behaviour "
                "change is intentional and tracked.",
            )
        )

    # --- Large change: size heuristics.
    file_count = len(files)
    if file_count > 20 or total_changes > 1000:
        flags.append(
            (
                "large",
                f"Large change detected: {file_count} file(s) modified with "
                f"{total_changes:,} lines changed. This commit may require "
                "additional review.",
            )
        )
    elif file_count > 10 or total_changes > 400:
        flags.append(
            (
                "large",
                f"Large change detected: {file_count} file(s) modified with "
                f"{total_changes:,} lines changed. Consider whether it can be "
                "split into smaller, reviewable commits.",
            )
        )

    # --- Needs review: unclear/auto-generated messages.
    if not first_line.strip():
        flags.append(
            ("needs-review", "Commit has an empty message, making the change hard to audit.")
        )
    elif len(first_line.strip()) < 10:
        flags.append(
            (
                "needs-review",
                "Commit message is very short; a clearer description would help review.",
            )
        )
    if any(tok in lowered for tok in ("merge branch", "merge pull request", "auto-merge")):
        flags.append(
            (
                "needs-review",
                "Merge commit: verify the branch was reviewed and CI passed before merge.",
            )
        )

    return flags


# Classification priority (highest first). One commit may carry several flags;
# we report the most severe one and keep the rest as secondary details.
_FLAG_PRIORITY = ["suspicious", "bug-prone", "large", "risky", "needs-review"]

_CLASSIFICATION_LABELS = {
    "suspicious": "Suspicious",
    "bug-prone": "Bug-Prone",
    "large": "Large Change",
    "risky": "Risky",
    "needs-review": "Needs Review",
}

_SEVERITY_BY_CLASSIFICATION = {
    "Suspicious": "high",
    "Bug-Prone": "high",
    "Large Change": "medium",
    "Risky": "high",
    "Needs Review": "low",
    "Normal": "low",
}


def _classify(flags: list[tuple[str, str]]) -> tuple[str, str]:
    """Return (classification, primary_reason) from the detected flags."""
    if not flags:
        return (
            "Normal",
            "Commit size, message and file set look normal. No risk signals detected.",
        )
    for flag_id in _FLAG_PRIORITY:
        for flag, reason in flags:
            if flag == flag_id:
                return _CLASSIFICATION_LABELS.get(flag_id, "Normal"), reason
    return "Normal", flags[0][1]


def _severity_for_classification(classification: str) -> str:
    return _SEVERITY_BY_CLASSIFICATION.get(classification, "low")


def analyze_commit(commit: dict, context: str = "") -> dict:
    """
    Analyze a single commit and classify it as Normal / Risky / Bug-prone /
    Large change / Suspicious / Needs review, with a plain-language reason.

    Deterministic rules always produce output. When Anthropic is configured
    the classification is enriched with an AI narrative (best-effort).
    """
    flags = _flags_for_commit(commit)
    classification, primary_reason = _classify(flags)

    result: dict = {
        "sha": commit.get("full_sha") or commit.get("sha") or "",
        "short_sha": (commit.get("sha") or commit.get("full_sha") or "")[:10],
        "author": commit.get("author") or commit.get("author_login") or "",
        "date": commit.get("date") or "",
        "classification": classification,
        "severity": _severity_for_classification(classification),
        "reason": primary_reason,
        "details": [reason for _, reason in flags],
        "flags": [flag for flag, _ in flags],
        "files_changed": len(_commit_files(commit)),
        "additions": _commit_stats(commit)[0],
        "deletions": _commit_stats(commit)[1],
        "engine": "rule-based",
    }

    if settings.anthropic_configured:
        enriched = _analyze_commit_ai(commit, classification, primary_reason)
        if enriched:
            result["reason"] = enriched.get("reason") or result["reason"]
            result["suggestion"] = enriched.get("suggestion", "")
            result["engine"] = "ai"

    return result


def _analyze_commit_ai(commit: dict, classification: str, reason: str) -> Optional[dict]:
    """Ask Claude to confirm/refine the commit classification (best-effort)."""
    first_line, full_message = _commit_message(commit)
    files = _commit_files(commit)
    additions, deletions = _commit_stats(commit)
    file_names = [f.get("filename", "") for f in files][:15]

    prompt = (
        "Review this single git commit and confirm or refine its risk "
        "classification. Respond with a single JSON object only:\n"
        '{"reason": "<plain-language why, 1-2 sentences>", '
        '"suggestion": "<one concrete review suggestion>"}\n'
        f"Current rule-based classification: {classification}\n"
        f"Rule-based reason: {reason}\n"
        f"Author: {commit.get('author') or commit.get('author_login') or 'unknown'}\n"
        f"Message: {first_line}\n"
        f"Full message: {full_message[:500]}\n"
        f"Additions: {additions} | Deletions: {deletions}\n"
        f"Files ({len(file_names)}): {', '.join(file_names)}\n"
    )
    return _ai_result_or_none(
        prompt,
        system=(
            "You are a senior code reviewer analyzing commit risk. Only output "
            "the requested JSON object."
        ),
    )


def analyze_commits(commits: list[dict], context: str = "") -> list[dict]:
    """
    Analyze a batch of commits and return per-commit results, most recent
    first (the input order is preserved).
    """
    return [analyze_commit(c, context=context) for c in commits]


def member_activity_analysis(member: dict) -> dict:
    """
    Simple, fair, technical analysis of one member's project activity.
    Never makes personal judgments - only describes commits, PRs and recency.

    Returns {"status": str, "text": str, "level": str} where level is one of
    high|medium|low|none (used for styling).
    """
    username = member.get("username", "unknown")
    commits = int(member.get("commits") or 0)
    prs = int(member.get("pr_count") or 0)
    issues = int(member.get("issue_count") or 0)
    reviews = int(member.get("prs_reviewed") or 0)
    last_active_days = member.get("last_active_days")
    score = int(member.get("activity_score") or 0)

    if last_active_days is None:
        return {
            "status": "no-activity",
            "level": "none",
            "text": "No activity detected for this member in the analyzed window.",
        }
    if last_active_days > 30:
        return {
            "status": "no-activity",
            "level": "none",
            "text": f"No recent activity detected ({username} last active {last_active_days} days ago).",
        }

    if commits == 0 and prs == 0 and reviews == 0:
        return {
            "status": "no-activity",
            "level": "none",
            "text": f"No commits, PRs or reviews detected in the analyzed window.",
        }

    if score >= 80 and last_active_days <= 7:
        return {
            "status": "high-activity",
            "level": "high",
            "text": (
                f"High activity — active contributor during the last 7 days "
                f"({commits} commits, {prs} PRs, {reviews} reviews)."
            ),
        }
    if score >= 60 or last_active_days <= 7:
        return {
            "status": "active",
            "level": "medium",
            "text": (
                f"Good activity — {username} was active {last_active_days} day(s) ago "
                f"({commits} commits, {prs} PRs, {reviews} reviews)."
            ),
        }
    return {
        "status": "low-activity",
        "level": "low",
        "text": (
            f"Moderate activity — {username} last active {last_active_days} days ago "
            f"({commits} commits, {prs} PRs)."
        ),
    }


def analyze_repository_ai(report: dict) -> Optional[dict]:
    """
    Optional Claude-based repository narrative.

    Only called when ANTHROPIC_API_KEY is configured. Returns None on any
    failure so the rule-based analysis always stands on its own.
    """
    if not settings.anthropic_configured:
        return None

    overview = report.get("overview") or {}
    members = report.get("members") or []
    compact = [
        {
            "username": m.get("username"),
            "commits": m.get("commits"),
            "pr_count": m.get("pr_count"),
            "issue_count": m.get("issue_count"),
            "last_active_days": m.get("last_active_days"),
            "activity_score": m.get("activity_score"),
        }
        for m in members
    ]
    prompt = (
        "You are a senior engineering manager. Summarize the health of this "
        "GitHub repository in 2-3 crisp sentences and give the top 3 "
        "priorities for the next sprint. Be concrete and constructive.\n"
        f"Repo: {report.get('repo') or report.get('owner')}/{report.get('repo')}\n"
        f"Overview: {json.dumps(overview, default=str)}\n"
        f"Members: {json.dumps(compact, default=str)}\n"
        "Respond with a single JSON object only:\n"
        '{"narrative": "<2-3 sentences>", "priorities": ["<priority 1>", "<priority 2>", "<priority 3>"]}'
    )
    payload = _ai_result_or_none(
        prompt,
        system=(
            "You are a senior engineering manager coach. Only output the requested JSON object."
        ),
    )
    if not payload:
        return None
    priorities = payload.get("priorities")
    if not isinstance(priorities, list):
        priorities = []
    return {
        "narrative": str(payload.get("narrative", "")),
        "priorities": [str(p) for p in priorities[:3]],
    }
