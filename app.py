"""

GitPulse - GitHub Team Intelligence Platform
Entry point: creates the Flask application and wires every module together.

Run locally with:
    python app.py
Or via gunicorn (production):
    gunicorn --bind 0.0.0.0:8000 --workers 4 'app:create_app()'
"""

import os
from datetime import datetime, timezone

from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

# Local modules
from config.logging_setup import setup_logging
from config.settings import settings
from utils import ai_analyzer, fixer, webhooks
from utils.ai_analyzer import generate_suggestions
from utils.auth import (
    clear_session_token,
    create_github_oauth,
    get_selected_repo,
    get_session_token,
    is_login_rate_limited,
    is_logged_in,
    is_user_allowed,
    login_required,
    record_login_attempt,
    set_selected_repo,
    store_session_token,
)
from utils.code_scanner import CodeScanner
from utils.github_api import GitHubAPI, GitHubError
from utils.store import get_store


def _normalize_stored_activity(events: list[dict]) -> list[dict]:
    """Turn persisted webhook rows back into the dashboard feed shape."""
    normalized: list[dict] = []
    for event in events:
        try:
            payload = event.get("payload_json")
            if isinstance(payload, str):
                import json
                payload = json.loads(payload)
            payload = payload if isinstance(payload, dict) else {}
            record = webhooks.handle_event(event.get("event", "unknown"), payload)
            created_at = event.get("created_at", "")
            record["created_at"] = created_at
            record["date"] = created_at
            record["type"] = record.get("type") or record.get("event", "unknown")
            record["actor"] = record.get("actor") or record.get("sender") or ""
            record["title"] = record.get("title") or record.get("summary") or ""
            normalized.append(record)
        except Exception as exc:  # noqa: BLE001
            from config.logging_setup import get_logger
            get_logger("app").warning("Could not normalize stored webhook event: %s", exc)
    return normalized


def create_app() -> Flask:
    """Application factory - Flask's recommended app structure."""
    app = Flask(__name__)
    app.config["SECRET_KEY"] = settings.SECRET_KEY
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = not settings.is_development
    app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 8  # 8 hours
    app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024  # 1 MB request cap

    # Logging (idempotent - safe to call multiple times in tests).
    setup_logging(log_dir=settings.LOG_DIR, level=settings.LOG_LEVEL)

    # --- OAuth provider -------------------------------------------------
    oauth = create_github_oauth(app)
    app.extensions["gitpulse_oauth"] = oauth

    # --- Runtime state ---------------------------------------------------
    # Simple in-process caches so we don't hammer the GitHub API or rescan
    # the whole repo on every dashboard refresh.
    app.extensions["scan_cache"] = {"data": None}
    # Persistent SQLite store for AI analyses, fix attempts, webhook events.
    app.extensions["store"] = get_store()
    # Recent webhook events (newest first) for the dashboard activity feed.
    app.extensions["recent_activity"] = _normalize_stored_activity(
        app.extensions["store"].list_webhook_events(limit=20)
    )

    # --- Routes ---------------------------------------------------------
    register_routes(app)
    register_api(app)

    # --- Error handlers -------------------------------------------------
    register_error_handlers(app)

    # Startup configuration check: report whether the critical GitHub
    # settings are configured. The token value itself is never printed.
    for name, status in settings.config_status().items():
        app.logger.info("%s: %s", name, status)

    # Surface configuration problems (warn, don't crash).
    for problem in settings.validate():
        app.logger.warning("Configuration warning: %s", problem)

    return app


# ======================================================================
# Route helpers
# ======================================================================
def get_api() -> GitHubAPI:
    """
    Build a GitHubAPI from the session token, falling back to the
    server-configured GITHUB_TOKEN so the dashboard works without a
    login when a token is present in .env.
    """
    return GitHubAPI(get_session_token() or settings.GITHUB_TOKEN)


def _current_repo() -> tuple[str, str]:
    """
    Return (owner, repo) from the repository selected in the session.

    Falls back to the optional GITHUB_OWNER / GITHUB_REPO bootstrap
    defaults from .env (used only when the user has not picked a repo).
    Returns ("", "") when no repository is available.
    """
    selected = get_selected_repo()
    if "/" in selected:
        owner, repo = selected.split("/", 1)
        return owner.strip(), repo.strip()
    return settings.GITHUB_OWNER.strip(), settings.GITHUB_REPO.strip()


def _repo_full_name() -> str:
    """Return the 'owner/repo' of the currently selected repository, or ''."""
    owner, repo = _current_repo()
    if owner and repo:
        return f"{owner}/{repo}"
    return ""


def _render_unauthorized(username: str, reason: str):
    """Render the 403 page with an audit log line."""
    from config.logging_setup import get_logger

    get_logger("auth").warning(
        "Access denied for '%s' (reason: %s)", username, reason
    )
    return (
        render_template("unauthorized.html", username=username, reason=reason, _bare=True),
        403,
    )


def _render_login(status_code: int | None = None):
    """Render the login page as a bare, full-viewport page."""
    kwargs: dict = {"oauth_enabled": settings.oauth_configured, "_bare": True}
    if status_code is None:
        return render_template("login.html", **kwargs)
    return render_template("login.html", **kwargs), status_code


def _load_report() -> tuple[dict | None, str | None]:
    """
    Load the team report for the currently selected repository.

    Returns (report, error). `error` is None on success. Handles
    misconfiguration and GitHub errors so routes never crash.
    """
    owner, repo = _current_repo()
    if not owner or not repo:
        return None, "No repository selected. Pick a repository from the selector in the top bar."
    if not get_session_token() and not settings.GITHUB_TOKEN:
        return None, "No GitHub token available: set GITHUB_TOKEN in .env or log in."

    api = get_api()
    try:
        report = api.build_team_report(owner, repo)
        return report, None
    except GitHubError as exc:
        return None, exc.message
    except Exception as exc:  # noqa: BLE001 - never crash the dashboard
        from config.logging_setup import get_logger

        get_logger("app").exception("Unexpected error while building team report: %s", exc)
        return None, "Unexpected error while loading GitHub data."


def _compute_health_score(report: dict) -> dict:
    """
    Compute a transparent heuristic repository health score (0-100) from
    available report data. Deterministic and auditable.
    """
    if not report:
        return {"score": 0, "summary": "No data available.", "breakdown": {}}

    overview = report.get("overview", {})
    members = report.get("members", [])
    pushes = report.get("pushes", [])
    prs = report.get("pull_requests", [])
    issues = report.get("issues", [])

    # 1. Commit activity (0-25): more commits = better, capped
    total_commits = overview.get("total_commits", 0)
    commit_score = min(25, total_commits * 2)

    # 2. Contributor activity (0-25): more active contributors = better
    active_members = overview.get("active_members", 0)
    total_members = max(1, overview.get("members", 1))
    contributor_score = min(25, int((active_members / total_members) * 25))

    # 3. Issue management (0-25): fewer open issues = better
    open_issues = overview.get("open_issues", 0)
    if open_issues == 0:
        issue_score = 25
    elif open_issues <= 3:
        issue_score = 20
    elif open_issues <= 10:
        issue_score = 15
    elif open_issues <= 20:
        issue_score = 10
    else:
        issue_score = 5

    # 4. PR hygiene (0-25): merged PRs vs open = better
    open_prs = overview.get("open_prs", 0)
    merged_prs = overview.get("merged_prs", 0)
    total_prs = open_prs + merged_prs
    if total_prs == 0:
        pr_score = 15  # neutral if no PRs
    else:
        merge_ratio = merged_prs / total_prs
        pr_score = min(25, int(merge_ratio * 25) + (5 if open_prs <= 5 else 0))

    total_score = commit_score + contributor_score + issue_score + pr_score

    # Determine health label
    if total_score >= 80:
        health_label = "Excellent"
    elif total_score >= 60:
        health_label = "Good"
    elif total_score >= 40:
        health_label = "Fair"
    else:
        health_label = "Needs Attention"

    breakdown = {
        "commit_activity": {"score": commit_score, "max": 25, "label": "Commit Activity"},
        "contributors": {"score": contributor_score, "max": 25, "label": "Contributor Activity"},
        "issue_management": {"score": issue_score, "max": 25, "label": "Issue Management"},
        "pr_hygiene": {"score": pr_score, "max": 25, "label": "PR Hygiene"},
    }

    return {
        "score": total_score,
        "health_label": health_label,
        "summary": f"Repository health is {health_label} ({total_score}/100).",
        "breakdown": breakdown,
    }


def _load_report_view() -> tuple[dict | None, str | None]:
    """
    Build the presentation-ready Team Reports view for the selected range.

    Uses the same `_load_report` source as the dashboard, then shapes it
    with `reports.build_view` and attaches the AI / rule-based analysis.
    Returns (view, error) - `error` is None on success.
    """
    from utils.reports import build_ai_summary, build_view, resolve_range

    report, error = _load_report()
    if error or not report:
        return None, error or "No repository selected. Pick a repository from the selector in the top bar."

    rng = resolve_range(
        period=(request.args.get("period") or "30d"),
        from_str=(request.args.get("from") or ""),
        to_str=(request.args.get("to") or ""),
    )
    view = build_view(report, rng["since"], rng["until"], rng["label"], rng["period"])
    view["ai"] = build_ai_summary(view, rng["label"])
    return view, None


# ======================================================================
# Route definitions
# ======================================================================
def register_routes(app: Flask) -> None:
    # --- Root -----------------------------------------------------------
    @app.route("/")
    def index():
        if is_logged_in():
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    # --- Login (GET form + POST PAT) ------------------------------------
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if is_logged_in():
            return redirect(url_for("dashboard"))

        if request.method == "GET":
            return _render_login()

        # POST: Personal Access Token login.
        client_ip = request.remote_addr or "unknown"
        if is_login_rate_limited(client_ip):
            flash("Too many login attempts. Please try again later.", "danger")
            return _render_login(429)

        record_login_attempt(client_ip)
        token = (request.form.get("token") or "").strip()
        if not token:
            flash("Please paste a GitHub token.", "danger")
            return _render_login()

        api = GitHubAPI(token)
        try:
            user = api.validate_token()
        except GitHubError as exc:
            flash(f"Sign-in failed: {exc.message}", "danger")
            return _render_login()

        username = (user or {}).get("login", "")
        if not is_user_allowed(username):
            return _render_unauthorized(username, "not on the ALLOWED_GITHUB_USERS list")

        store_session_token(token, "pat")
        session["github_user"] = username
        session["github_avatar"] = (user or {}).get("avatar_url", "")
        # A different account may be signing in - do not carry over a
        # repository selection that may belong to the previous user. The
        # dashboard lets the user pick from THIS account's repositories.
        set_selected_repo("")
        flash(f"Welcome back, {username}!", "success")
        return redirect(url_for("dashboard"))

    # --- OAuth start -----------------------------------------------------
    @app.route("/auth/login")
    def auth_login():
        if not settings.oauth_configured:
            flash("GitHub OAuth is not configured. Use a Personal Access Token instead.", "warning")
            return redirect(url_for("login"))
        oauth = app.extensions["gitpulse_oauth"]
        return oauth.github.authorize_redirect(settings.GITHUB_REDIRECT_URI)

    # --- OAuth callback --------------------------------------------------
    @app.route("/auth/callback")
    def auth_callback():
        from config.logging_setup import get_logger

        if not settings.oauth_configured:
            return redirect(url_for("login"))

        oauth = app.extensions["gitpulse_oauth"]
        try:
            token = oauth.github.authorize_access_token()
            access_token = token.get("access_token")
        except Exception as exc:  # noqa: BLE001 - bad / forged callback
            get_logger("auth").warning("OAuth callback failed: %s", exc)
            flash("GitHub sign-in failed. Please try again.", "danger")
            return redirect(url_for("login"))

        if not access_token:
            flash("GitHub did not return an access token.", "danger")
            return redirect(url_for("login"))

        # Fetch the profile to check against the allow-list.
        api = GitHubAPI(access_token)
        try:
            user = api.validate_token()
        except GitHubError as exc:
            get_logger("auth").warning("OAuth token validation failed: %s", exc.message)
            flash("Could not validate your GitHub account.", "danger")
            return redirect(url_for("login"))

        username = (user or {}).get("login", "")
        if not is_user_allowed(username):
            return _render_unauthorized(username, "not on the ALLOWED_GITHUB_USERS list")

        store_session_token(access_token, "oauth")
        session["github_user"] = username
        session["github_avatar"] = (user or {}).get("avatar_url", "")
        # Never carry a previous account's repository selection across logins.
        set_selected_repo("")
        flash(f"Signed in as {username}.", "success")
        return redirect(url_for("dashboard"))

    # --- Logout ----------------------------------------------------------
    @app.route("/logout")
    def logout():
        clear_session_token()
        flash("You have been logged out.", "info")
        return redirect(url_for("login"))

    # --- Dashboard -------------------------------------------------------
    @app.route("/dashboard")
    @login_required
    def dashboard():
        report, error = _load_report()

        suggestions = []
        scan_findings = []
        scan_summary = CodeScanner.summarize([])
        if report:
            suggestions = generate_suggestions(report["members"])

        # Attach cached scan results (if any) so the tab shows data.
        scan_cache = app.extensions["scan_cache"]
        scan_findings = scan_cache.get("data") or []
        scan_summary = CodeScanner.summarize(scan_findings)

        store = app.extensions["store"]
        ai_analyses = store.list_analyses(limit=30)
        fix_attempts = store.list_fix_attempts(limit=30)
        recent_activity = list(app.extensions["recent_activity"])

        ai_errors_count = sum(
            1
            for a in ai_analyses
            if a["result"].get("severity") in ("high", "critical", "medium")
        )
        ai_fixed_count = sum(1 for f in fix_attempts if f.get("status") == "created")

        return render_template(
            "dashboard.html",
            report=report,
            suggestions=[s.to_dict() for s in suggestions],
            scan_findings=sorted(scan_findings, key=CodeScanner.severity_sort_key),
            scan_summary=scan_summary,
            error=error,
            selected_repo=_repo_full_name(),
            ai_enabled=settings.anthropic_configured,
            ai_analyses=ai_analyses,
            fix_attempts=fix_attempts,
            recent_activity=recent_activity,
            activity_window=settings.ACTIVITY_WINDOW_DAYS,
            ai_errors_count=ai_errors_count,
            ai_fixed_count=ai_fixed_count,
            webhook_configured=bool(settings.GITHUB_WEBHOOK_SECRET),
            health=_compute_health_score(report) if report else None,
        )

    # --- Dashboard refresh (re-fetch collaborators + activity) ------------
    @app.route("/dashboard/refresh", methods=["POST"])
    @login_required
    def refresh_dashboard():
        """Force a fresh fetch of collaborators, commits, PRs and issues."""
        # The report itself is rebuilt from the GitHub API on every dashboard
        # load; refreshing only needs to clear the cached scan findings, then
        # reload the page.
        app.extensions["scan_cache"] = {"data": None}
        from config.logging_setup import get_logger

        get_logger("app").info(
            "Dashboard refresh requested by %s", session.get("github_user")
        )
        flash("Dashboard refreshed - GitHub data re-fetched.", "success")
        return redirect(url_for("dashboard"))
    # --- Member profile --------------------------------------------------
    @app.route("/member/<username>")
    @login_required
    def member_profile(username):
        owner, repo = _current_repo()
        if not owner or not repo:
            flash("No repository selected. Choose one from the selector above.", "warning")
            return redirect(url_for("dashboard"))

        api = get_api()
        try:
            profile = api.build_member_profile(owner, repo, username)
        except GitHubError as exc:
            flash(f"Could not load member profile: {exc.message}", "danger")
            return redirect(url_for("dashboard"))
        except Exception as exc:  # noqa: BLE001
            from config.logging_setup import get_logger

            get_logger("app").exception("Member profile error for %s: %s", username, exc)
            flash("Could not load member profile.", "danger")
            return redirect(url_for("dashboard"))

        member_suggestions = generate_suggestions([profile["member"]])
        return render_template(
            "member.html",
            profile=profile,
            suggestions=[s.to_dict() for s in member_suggestions],
            ai_enabled=settings.anthropic_configured,
            selected_repo=_repo_full_name(),
        )

    # --- Team Members (status-filterable) --------------------------------
    @app.route("/team-members")
    @login_required
    def team_members():
        """
        List every team member, optionally filtered by activity status.

        The filter uses the exact same `is_active` flag that the dashboard
        uses to derive its Active/Inactive counts, so the numbers always
        match. The flag is computed in build_team_report via
        activity_mod.enrich_member (status == "ACTIVE").

        ?status=active    -> only ACTIVE members
        ?status=inactive  -> every non-ACTIVE member
        no parameter      -> all members
        """
        report, error = _load_report()

        status = (request.args.get("status") or "").strip().lower()
        members = list((report or {}).get("members") or [])

        filter_title = "All Members"
        if status == "active":
            members = [m for m in members if m.get("is_active")]
            filter_title = "Active Members"
        elif status == "inactive":
            members = [m for m in members if not m.get("is_active")]
            filter_title = "Inactive Members"
        else:
            status = ""

        return render_template(
            "team_members.html",
            report=report,
            members=members,
            error=error,
            filter_status=status,
            filter_title=filter_title,
            activity_window=settings.ACTIVITY_WINDOW_DAYS,
            selected_repo=_repo_full_name(),
        )

    # --- Team Reports (real page) ----------------------------------------
    @app.route("/reports")
    @login_required
    def reports():
        """Full team performance report with range selection and exports."""
        view, error = _load_report_view()

        range_presets = [
            ("today", "Today"),
            ("7d", "Last 7 Days"),
            ("30d", "Last 30 Days"),
            ("month", "This Month"),
            ("custom", "Custom Range"),
        ]
        range_query = {
            k: v
            for k, v in request.args.items()
            if k in ("period", "from", "to") and v
        }

        return render_template(
            "reports.html",
            view=view,
            error=error,
            repo_name=_repo_full_name(),
            range_presets=range_presets,
            range_query=range_query,
            from_str=(request.args.get("from") or ""),
            to_str=(request.args.get("to") or ""),
            selected_repo=_repo_full_name(),
        )

    @app.route("/reports/export/<fmt>")
    @login_required
    def reports_export(fmt):
        """Download the current report as CSV or PDF."""
        view, error = _load_report_view()
        if error or not view:
            flash(error or "No report available to export.", "warning")
            return redirect(url_for("reports"))

        filename_base = "gitpulse-team-report"

        if fmt == "csv":
            from utils.reports import to_csv

            payload = to_csv(view).encode("utf-8-sig")
            return Response(
                payload,
                mimetype="text/csv",
                headers={"Content-Disposition": f'attachment; filename="{filename_base}.csv"'},
            )

        if fmt == "pdf":
            from utils.reports import to_pdf

            payload = to_pdf(view)
            return Response(
                payload,
                mimetype="application/pdf",
                headers={"Content-Disposition": f'attachment; filename="{filename_base}.pdf"'},
            )

        return jsonify({"error": f"Unsupported export format: {fmt}"}), 404

    @app.route("/code-review")
    @login_required
    def code_review():
        report, error = _load_report()
        prs = []
        total_prs = 0
        needs_review = 0
        approved_count = 0
        changes_requested = 0
        if report:
            prs = report.get("pull_requests", [])
            total_prs = len(prs)
            needs_review = sum(1 for p in prs if p.get("review_status") == "no_reviews")
            approved_count = sum(1 for p in prs if p.get("review_status") == "approved")
            changes_requested = sum(1 for p in prs if p.get("review_status") == "changes_requested")
        return render_template(
            "code_review.html",
            report=report,
            error=error,
            prs=prs,
            repo_name=_repo_full_name(),
            total_prs=total_prs,
            needs_review=needs_review,
            approved_count=approved_count,
            changes_requested=changes_requested,
        )

    @app.route("/notifications")
    @login_required
    def notifications():
        store = app.extensions["store"]
        analyses = store.list_analyses(limit=20)
        recent_activity = list(app.extensions["recent_activity"])
        scan_cache = app.extensions["scan_cache"]
        scan_findings = scan_cache.get("data") or []
        return render_template(
            "notifications.html",
            analyses=analyses,
            recent_activity=recent_activity,
            scan_findings=scan_findings,
            selected_repo=_repo_full_name(),
        )

    @app.route("/settings")
    @login_required
    def settings_page():
        return render_template(
            "settings.html",
            selected_repo=_repo_full_name(),
            github_user=session.get("github_user", ""),
            auth_method=session.get("auth_method", "pat"),
            ai_enabled=settings.anthropic_configured,
            webhook_configured=bool(settings.GITHUB_WEBHOOK_SECRET),
            activity_window=settings.ACTIVITY_WINDOW_DAYS,
            current_repo=_repo_full_name(),
        )

    # --- AI: analyze a file (dashboard form) -----------------------------
    @app.route("/dashboard/analyze", methods=["POST"])
    @login_required
    def analyze_file():
        path = (request.form.get("path") or "").strip()
        ref = (request.form.get("ref") or "").strip() or None
        if not path:
            flash("A file path is required for analysis.", "warning")
            return redirect(url_for("dashboard"))

        owner, repo = _current_repo()
        if not owner or not repo:
            flash("No repository selected. Choose one from the selector above.", "warning")
            return redirect(url_for("dashboard"))

        api = get_api()
        try:
            ref = ref or api.get_default_branch(owner, repo)
            content = api.fetch_file_content(owner, repo, path, ref=ref)
            result = ai_analyzer.analyze_code(path, content, context=f"{owner}/{repo}")
        except GitHubError as exc:
            flash(f"Analysis failed: {exc.message}", "danger")
            return redirect(url_for("dashboard"))

        app.extensions["store"].save_analysis("code", path, result, author=session.get("github_user"))
        if result["severity"] in ("high", "critical"):
            flash(f"AI found a {result['severity']} issue in {path}: {result['problem']}", "warning")
        else:
            flash(f"AI analysis complete for {path}: {result['problem']}", "info")
        return redirect(url_for("dashboard"))

    # --- AI: create a fix PR (dashboard form) ----------------------------
    @app.route("/dashboard/ai-fix", methods=["POST"])
    @login_required
    def ai_fix_route():
        path = (request.form.get("path") or "").strip()
        issue_label = (request.form.get("issue_label") or "").strip() or path
        if not path:
            flash("A file path is required to generate a fix.", "warning")
            return redirect(url_for("dashboard"))

        owner, repo = _current_repo()
        if not owner or not repo:
            flash("No repository selected. Choose one from the selector above.", "warning")
            return redirect(url_for("dashboard"))

        api = get_api()
        try:
            ref = api.get_default_branch(owner, repo)
            content = api.fetch_file_content(owner, repo, path, ref=ref)
            analysis = ai_analyzer.analyze_code(path, content, context=f"{owner}/{repo}")
            fixed_code = analysis.get("fixed_code") or ""
            if not fixed_code:
                flash("AI did not produce a fix for this file.", "warning")
                return redirect(url_for("dashboard"))
            outcome = fixer.create_fix_pull_request(
                api,
                owner,
                repo,
                path,
                issue_label,
                analysis,
                fixed_code,
            )
        except GitHubError as exc:
            flash(f"Fix failed: {exc.message}", "danger")
            return redirect(url_for("dashboard"))

        if outcome.get("status") == "created":
            flash(f"AI fix PR created: {outcome['pr_url']}", "success")
        elif outcome.get("status") == "validation_failed":
            flash(f"AI fix was NOT merged - validation failed: {outcome.get('error', '')}", "danger")
        else:
            flash(f"AI fix failed: {outcome.get('error', 'unknown error')}", "danger")
        return redirect(url_for("dashboard"))

    # --- GitHub webhook --------------------------------------------------
    @app.route("/webhook/github", methods=["POST"])
    def github_webhook():
        body = request.get_data()
        signature = request.headers.get("X-Hub-Signature-256") or request.headers.get(
            "X-Hub-Signature"
        )
        if not webhooks.verify_signature(body, signature):
            return jsonify({"status": "rejected"}), 403

        event = request.headers.get("X-GitHub-Event", "unknown")
        payload = request.get_json(silent=True) or {}
        record = webhooks.handle_event(event, payload)
        record["date"] = datetime.now(timezone.utc).isoformat()
        record["type"] = record.get("type") or record.get("event", "unknown")
        record["actor"] = record.get("actor") or record.get("sender") or ""
        record["title"] = record.get("title") or record.get("summary") or ""
        store = app.extensions["store"]
        store.save_webhook_event(
            event=record["event"],
            action=record.get("action"),
            sender=record.get("sender"),
            repo=record.get("repo"),
            payload=payload,
        )
        # Keep the in-memory feed fresh (newest first).
        feed = [record] + list(app.extensions["recent_activity"])
        app.extensions["recent_activity"] = feed[:20]

        from config.logging_setup import get_logger

        get_logger("app").info("Webhook received: %s", record.get("summary", event))
        return jsonify({"status": "ok"}), 200

    # --- Security scan (POST triggers, GET returns cached) ----------------
    @app.route("/dashboard/scan", methods=["POST"])
    @login_required
    def run_scan():
        scan_cache = app.extensions["scan_cache"]
        api = get_api()
        scanner = CodeScanner()

        target = (request.form.get("target") or "repo").strip().lower()
        try:
            if target == "local":
                # Scan this project's own source tree (useful for demos).
                local_path = os.path.dirname(os.path.abspath(__file__))
                findings = scanner.scan_path(local_path)
                source_label = f"local project ({local_path})"
            else:
                # Scan the selected GitHub repository via the API.
                owner, repo = _current_repo()
                if not owner or not repo:
                    flash("No repository selected. Choose one from the selector above.", "warning")
                    return redirect(url_for("dashboard"))
                findings = scanner.scan_github_repo(api, owner, repo)
                source_label = f"{owner}/{repo}"
        except GitHubError as exc:
            flash(f"Scan failed: {exc.message}", "danger")
            return redirect(url_for("dashboard"))

        scan_cache["data"] = findings
        flash(
            f"Scan complete ({source_label}): {len(findings)} findings.",
            "success",
        )
        return redirect(url_for("dashboard"))

    # --- Health check (for deployment platforms) ---------------------------
    @app.route("/healthz")
    def healthz():
        # Health probes are public; do not expose user or private repository data.
        return jsonify({"status": "ok"})


# ======================================================================
# JSON API (reused by the frontend)
# ======================================================================
def register_api_routes(app: Flask) -> None:
    """REST-style endpoints powering the dashboard and integrations."""

    def _report_or_error():
        """Return (report, error) or abort with 500 JSON."""
        report, error = _load_report()
        return report, error

    # --- GitHub account + repository selection ---------------------------
    @app.route("/api/github/user")
    @login_required
    def api_github_user():
        """Return the currently authenticated GitHub user (never the token)."""
        api = get_api()
        try:
            user = api.validate_token()
        except GitHubError as exc:
            code = 401 if exc.status_code in (401, 403) else 400
            return jsonify({"error": exc.message}), code
        return jsonify(
            {
                "login": user.get("login", ""),
                "avatar_url": user.get("avatar_url", ""),
                "selected_repo": get_selected_repo(),
            }
        )

    @app.route("/api/github/repos")
    @login_required
    def api_github_repos():
        """Return the repositories accessible to the authenticated user."""
        api = get_api()
        try:
            repos = api.get_user_repos()
        except GitHubError as exc:
            code = 401 if exc.status_code in (401, 403) else 400
            return jsonify({"error": exc.message}), code
        return jsonify({"repos": repos, "selected_repo": get_selected_repo()})

    @app.route("/api/github/select-repo", methods=["POST"])
    @login_required
    def api_github_select_repo():
        """
        Select the repository to monitor for this session.

        The repo is validated against GitHub with the user's own token
        before it is stored, so access-denied / not-found errors are
        caught here with a friendly message.
        """
        data = request.get_json(silent=True) or {}
        full_name = (data.get("repo") or "").strip().strip("/")
        if not full_name or "/" not in full_name:
            return jsonify({"error": "A repository in 'owner/name' format is required."}), 400
        owner, repo = full_name.split("/", 1)
        if not owner or not repo:
            return jsonify({"error": "A repository in 'owner/name' format is required."}), 400

        api = get_api()
        try:
            meta = api.get_repository(owner, repo)
        except GitHubError as exc:
            code = 404 if exc.status_code == 404 else 400
            return jsonify({"error": exc.message}), code

        selected = meta.get("full_name", f"{owner}/{repo}")
        set_selected_repo(selected)
        app.logger.info("Repository selected: %s", selected)
        return jsonify({"ok": True, "repo": selected})

    @app.route("/api/team")
    @login_required
    def api_team():
        report, error = _report_or_error()
        if error:
            return jsonify({"error": error}), 400
        return jsonify(
            {
                "owner": report["owner"],
                "repo": report["repo"],
                "team_name": report["team_name"],
                "overview": report["overview"],
            }
        )

    @app.route("/api/team/members")
    @login_required
    def api_team_members():
        report, error = _report_or_error()
        if error:
            return jsonify({"error": error}), 400
        return jsonify({"members": report["members"]})

    @app.route("/api/team/collaborators")
    @login_required
    def api_team_collaborators():
        """
        Return the repository's actual collaborators.

        The list comes from GET /repos/{owner}/{repo}/collaborators so it
        includes members who have been granted access even if they have
        never committed. Each collaborator carries username, avatar, role,
        permissions and per-member activity metrics.
        """
        report, error = _report_or_error()
        if error:
            return jsonify({"error": error}), 400
        return jsonify(
            {
                "collaborators": report["members"],
                "overview": report["overview"],
            }
        )

    @app.route("/api/team/activity")
    @login_required
    def api_team_activity():
        report, error = _report_or_error()
        if error:
            return jsonify({"error": error}), 400
        return jsonify(
            {
                "members": report["members"],
                "pushes": report["pushes"],
                "recent_activity": app.extensions["recent_activity"],
            }
        )

    @app.route("/api/team/member/<username>")
    @login_required
    def api_team_member(username):
        owner, repo = _current_repo()
        if not owner or not repo:
            return jsonify({"error": "No repository selected."}), 400
        api = get_api()
        try:
            profile = api.build_member_profile(owner, repo, username)
        except GitHubError as exc:
            return jsonify({"error": exc.message}), 400
        return jsonify(profile)

    @app.route("/api/commits")
    @login_required
    def api_commits():
        report, error = _report_or_error()
        if error:
            return jsonify({"error": error}), 400
        return jsonify({"pushes": report["pushes"]})

    @app.route("/api/pull-requests")
    @login_required
    def api_pull_requests():
        report, error = _report_or_error()
        if error:
            return jsonify({"error": error}), 400
        return jsonify({"pull_requests": report["pull_requests"]})

    @app.route("/api/issues")
    @login_required
    def api_issues():
        report, error = _report_or_error()
        if error:
            return jsonify({"error": error}), 400
        return jsonify({"issues": report["issues"]})

    @app.route("/api/issues/list")
    @login_required
    def api_issues_list():
        """
        Return the selected repository's real issues (pull requests
        excluded) fetched live from GitHub, shaped for the Issues page.

        Also attaches each issue's real AI status from the local store so
        the AI column shows 'Not analyzed' or the actual saved severity
        instead of invented results.
        """
        owner, repo = _current_repo()
        if not owner or not repo:
            return jsonify({"error": "No repository selected."}), 400
        api = get_api()
        try:
            issues = api.list_issues(owner, repo, state="all")
        except GitHubError as exc:
            return jsonify({"error": exc.message}), 400
        except Exception as exc:  # noqa: BLE001 - never leak stack traces
            app.logger.exception("Failed to list issues for %s/%s: %s", owner, repo, exc)
            return jsonify({"error": "Unable to load issues from GitHub."}), 500

        store = app.extensions["store"]
        for issue in issues:
            analysis = store.find_analysis("issue", f"#{issue['number']}")
            if analysis and analysis.get("result"):
                result = analysis["result"]
                issue["ai"] = {
                    "analyzed": True,
                    "severity": result.get("severity", ""),
                    "engine": result.get("engine", "rule-based"),
                }
            else:
                issue["ai"] = {"analyzed": False}

        total = len(issues)
        open_count = sum(1 for i in issues if i.get("state") == "open")
        return jsonify(
            {
                "issues": issues,
                "total": total,
                "open": open_count,
                "closed": total - open_count,
                "repo": f"{owner}/{repo}",
            }
        )

    @app.route("/api/repository")
    @login_required
    def api_repository():
        """Return the currently selected repository's metadata."""
        report, error = _report_or_error()
        if error:
            return jsonify({"error": error}), 400
        return jsonify({"repo": report["repo"], "team_name": report["team_name"]})

    @app.route("/api/overview")
    @login_required
    def api_overview():
        """Return the dashboard overview metrics."""
        report, error = _report_or_error()
        if error:
            return jsonify({"error": error}), 400
        return jsonify(
            {
                "owner": report["owner"],
                "repo": report["repo"],
                "overview": report["overview"],
                "languages": report["languages"],
            }
        )

    @app.route("/api/activity")
    @login_required
    def api_activity():
        """
        Return the unified activity feed.

        Query params: category (commit|pull_request|issue|member|other),
        author, q (substring match on title/action), limit (default 50).
        """
        report, error = _report_or_error()
        if error:
            return jsonify({"error": error}), 400
        feed = list(report.get("activity_feed") or [])
        category = (request.args.get("category") or "").strip().lower()
        author = (request.args.get("author") or "").strip().lower()
        query = (request.args.get("q") or "").strip().lower()
        try:
            limit = max(1, min(int(request.args.get("limit") or 50), 200))
        except (TypeError, ValueError):
            limit = 50

        if category:
            feed = [item for item in feed if (item.get("category") or "").lower() == category]
        if author:
            feed = [item for item in feed if (item.get("actor") or "").lower() == author]
        if query:
            feed = [
                item
                for item in feed
                if query in (item.get("title") or "").lower()
                or query in (item.get("action") or "").lower()
            ]
        return jsonify({"activity": feed[:limit]})

    @app.route("/api/commit/<sha>")
    @login_required
    def api_commit_detail(sha):
        """Return a single commit's detail (files, stats, message)."""
        owner, repo = _current_repo()
        if not owner or not repo:
            return jsonify({"error": "No repository selected."}), 400
        api = get_api()
        try:
            detail = api.build_commit_detail(owner, repo, sha)
        except GitHubError as exc:
            return jsonify({"error": exc.message}), 400
        return jsonify(detail)

    @app.route("/api/pull-request/<int:number>")
    @login_required
    def api_pull_request_detail(number):
        """Return a single pull request's rich detail view."""
        owner, repo = _current_repo()
        if not owner or not repo:
            return jsonify({"error": "No repository selected."}), 400
        api = get_api()
        try:
            detail = api.build_pr_detail(owner, repo, number)
        except GitHubError as exc:
            return jsonify({"error": exc.message}), 400
        return jsonify(detail)

    @app.route("/api/issue/<int:number>")
    @login_required
    def api_issue_detail(number):
        """Return a single issue's rich detail view."""
        owner, repo = _current_repo()
        if not owner or not repo:
            return jsonify({"error": "No repository selected."}), 400
        api = get_api()
        try:
            detail = api.build_issue_detail(owner, repo, number)
        except GitHubError as exc:
            return jsonify({"error": exc.message}), 400
        except Exception as exc:  # noqa: BLE001
            app.logger.exception("Failed to load issue #%s: %s", number, exc)
            return jsonify({"error": "Unable to load issue details from GitHub."}), 500
        analysis = app.extensions["store"].find_analysis("issue", f"#{number}")
        detail["ai"] = analysis.get("result") if analysis and analysis.get("result") else None
        return jsonify(detail)

    @app.route("/api/code-review/pr/<int:number>")
    @login_required
    def api_code_review_pr_detail(number):
        """Return a single pull request's detail for the Code Review page."""
        owner, repo = _current_repo()
        if not owner or not repo:
            return jsonify({"error": "No repository selected."}), 400
        api = get_api()
        try:
            detail = api.build_pr_detail(owner, repo, number)
        except GitHubError as exc:
            return jsonify({"error": exc.message}), 400
        except Exception as exc:  # noqa: BLE001
            app.logger.exception("Failed to load PR #%s for code review: %s", number, exc)
            return jsonify({"error": "Unable to load PR details from GitHub."}), 500
        try:
            files = api.get_pr_files(owner, repo, number)
            detail["files"] = [
                {
                    "filename": f.get("filename", ""),
                    "status": f.get("status", ""),
                    "additions": f.get("additions", 0),
                    "deletions": f.get("deletions", 0),
                }
                for f in files[:50]
            ]
        except GitHubError:
            detail["files"] = []
        reviews = detail.get("reviews", [])
        review_states = [r.get("state", "").upper() for r in reviews]
        if "APPROVED" in review_states:
            detail["review_status"] = "approved"
        elif "CHANGES_REQUESTED" in review_states:
            detail["review_status"] = "changes_requested"
        elif review_states:
            detail["review_status"] = "reviewed"
        else:
            detail["review_status"] = "no_reviews"
        analysis = app.extensions["store"].find_analysis("pr", f"#{number}")
        detail["ai"] = {"result": analysis["result"]} if analysis and analysis.get("result") else None
        return jsonify(detail)

    @app.route("/api/refresh", methods=["POST"])
    @login_required
    def api_refresh():
        """
        AJAX refresh: clear the in-memory GitHub HTTP cache and the cached
        scan results, then reload the webhook activity list.
        """
        from utils.github_api import clear_http_cache

        clear_http_cache()
        app.extensions["scan_cache"] = {"data": None}
        app.extensions["recent_activity"] = list(
            app.extensions["store"].list_webhook_events(limit=20)
        )
        app.logger.info("API refresh requested by %s", session.get("github_user"))
        return jsonify(
            {
                "ok": True,
                "message": "GitHub data cache cleared. The next request re-fetches from GitHub.",
            }
        )

    @app.route("/api/errors")
    @login_required
    def api_errors():
        store = app.extensions["store"]
        analyses = [
            {
                "id": a["id"],
                "created_at": a["created_at"],
                "kind": a["kind"],
                "target": a["target"],
                "author": a["author"],
                "result": a["result"],
            }
            for a in store.list_analyses(limit=50)
            if a["result"].get("severity") in ("high", "critical", "medium")
        ]
        return jsonify({"errors": analyses})

    @app.route("/api/ai/analyze", methods=["POST"])
    @login_required
    def api_ai_analyze():
        data = request.get_json(silent=True) or {}
        path = (data.get("path") or "").strip()
        if not path:
            return jsonify({"error": "A file path is required."}), 400
        owner, repo = _current_repo()
        if not owner or not repo:
            return jsonify({"error": "No repository selected."}), 400
        api = get_api()
        try:
            ref = (data.get("ref") or "").strip() or api.get_default_branch(owner, repo)
            content = api.fetch_file_content(owner, repo, path, ref=ref)
            result = ai_analyzer.analyze_code(path, content, context=f"{owner}/{repo}")
        except GitHubError as exc:
            return jsonify({"error": exc.message}), 400
        app.extensions["store"].save_analysis("code", path, result, author=session.get("github_user"))
        return jsonify(result)

    @app.route("/api/ai/fix", methods=["POST"])
    @login_required
    def api_ai_fix():
        data = request.get_json(silent=True) or {}
        path = (data.get("path") or "").strip()
        if not path:
            return jsonify({"error": "A file path is required."}), 400
        owner, repo = _current_repo()
        if not owner or not repo:
            return jsonify({"error": "No repository selected."}), 400
        api = get_api()
        try:
            ref = (data.get("ref") or "").strip() or api.get_default_branch(owner, repo)
            content = api.fetch_file_content(owner, repo, path, ref=ref)
            result = ai_analyzer.analyze_code(path, content, context=f"{owner}/{repo}")
        except GitHubError as exc:
            return jsonify({"error": exc.message}), 400
        return jsonify(
            {
                "path": path,
                "analysis": result,
                "has_fixed_code": bool(result.get("fixed_code")),
                "note": "Call /api/ai/fix-pr to open a reviewable pull request.",
            }
        )

    @app.route("/api/ai/analyze-pr", methods=["POST"])
    @login_required
    def api_ai_analyze_pr():
        data = request.get_json(silent=True) or {}
        try:
            number = int(data.get("number") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "A valid PR number is required."}), 400
        if number <= 0:
            return jsonify({"error": "A valid PR number is required."}), 400
        owner, repo = _current_repo()
        if not owner or not repo:
            return jsonify({"error": "No repository selected."}), 400
        api = get_api()
        try:
            pr = api.get_pull_request(owner, repo, number)
            files = api.get_pr_files(owner, repo, number)
            diff = "\n".join(
                (f.get("patch") or f"# {f.get('filename')}") for f in files[:30]
            )
            result = ai_analyzer.analyze_pull_request(pr, diff=diff)
        except GitHubError as exc:
            return jsonify({"error": exc.message}), 400
        app.extensions["store"].save_analysis(
            "pr", f"#{number}", result, author=session.get("github_user")
        )
        return jsonify(result)

    @app.route("/api/ai/analyze-issue", methods=["POST"])
    @login_required
    def api_ai_analyze_issue():
        data = request.get_json(silent=True) or {}
        try:
            number = int(data.get("number") or 0)
        except (TypeError, ValueError):
            return jsonify({"error": "A valid issue number is required."}), 400
        owner, repo = _current_repo()
        if not owner or not repo:
            return jsonify({"error": "No repository selected."}), 400
        if number <= 0:
            return jsonify({"error": "A valid issue number is required."}), 400
        api = get_api()
        try:
            issue = api.get_issue(owner, repo, number)
            if issue.get("pull_request"):
                return jsonify({"error": f"#{number} is a pull request, not an issue."}), 400
            result = ai_analyzer.analyze_issue(
                {
                    "title": issue.get("title", ""),
                    "body": issue.get("body", ""),
                    "labels": [label.get("name", "") for label in issue.get("labels", [])],
                }
            )
        except GitHubError as exc:
            return jsonify({"error": exc.message}), 400
        app.extensions["store"].save_analysis(
            "issue", f"#{number}", result, author=session.get("github_user")
        )
        return jsonify(result)

    @app.route("/api/ai/analyze-repo", methods=["POST"])
    @login_required
    def api_ai_analyze_repo():
        """
        Repository-level health analysis + fix recommendations.

        Rule-based by default (always available). When ANTHROPIC_API_KEY is
        configured an AI narrative is added on top. Results are saved to
        the store for the AI Fixes tab.
        """
        report, error = _load_report()
        if error:
            return jsonify({"error": error}), 400
        result = ai_analyzer.analyze_repository(report)
        if settings.anthropic_configured:
            narrative = ai_analyzer.analyze_repository_ai(report)
            if narrative:
                result["ai_narrative"] = narrative
                result["engine"] = "ai"
        owner, repo = _current_repo()
        app.extensions["store"].save_analysis(
            "repo", f"{owner}/{repo}",
            result,
            author=session.get("github_user"),
        )
        return jsonify(result)

    @app.route("/api/health")
    @login_required
    def api_health():
        """Return a computed repository health score from available data."""
        report, error = _load_report()
        if error:
            return jsonify({"error": error}), 400
        health = _compute_health_score(report)
        return jsonify(health)

    @app.route("/api/notifications")
    @login_required
    def api_notifications():
        """Return recent notifications derived from analysis, scans, and webhooks."""
        store = app.extensions["store"]
        notifications = []
        for a in store.list_analyses(limit=20):
            sev = a.get("result", {}).get("severity", "")
            if sev in ("high", "critical"):
                notifications.append({
                    "type": "critical",
                    "title": f"Security finding: {a.get('target', '')}",
                    "detail": a.get("result", {}).get("problem", a.get("result", {}).get("summary", "")),
                    "time": a.get("created_at", ""),
                    "icon": "\u26a0",
                })
            elif sev == "medium":
                notifications.append({
                    "type": "warning",
                    "title": f"Issue detected in {a.get('target', '')}",
                    "detail": a.get("result", {}).get("problem", a.get("result", {}).get("summary", "")),
                    "time": a.get("created_at", ""),
                    "icon": "\u2699",
                })
        for evt in app.extensions["recent_activity"]:
            evt_type = evt.get("type", "")
            if evt_type == "pull_request":
                notifications.append({
                    "type": "info",
                    "title": f"PR {evt.get('action', '')}: {evt.get('title', '')}",
                    "detail": f"@{evt.get('actor', '')} updated a pull request",
                    "time": evt.get("date", ""),
                    "icon": "\u21c4",
                })
            elif evt_type == "push":
                notifications.append({
                    "type": "success",
                    "title": f"Push by @{evt.get('actor', '')}",
                    "detail": evt.get("title", ""),
                    "time": evt.get("date", ""),
                    "icon": "\u2318",
                })
        scan_cache = app.extensions["scan_cache"]
        scan_findings = scan_cache.get("data") or []
        critical_findings = [f for f in scan_findings if f.get("severity") == "CRITICAL"]
        if critical_findings:
            notifications.append({
                "type": "critical",
                "title": f"{len(critical_findings)} critical security findings detected",
                "detail": "Run a security scan to review findings.",
                "time": "",
                "icon": "\u26a0",
            })
        notifications.sort(key=lambda n: n.get("time", ""), reverse=True)
        return jsonify({"notifications": notifications[:50]})

    @app.route("/api/ai/fix-pr", methods=["POST"])
    @login_required
    def api_ai_fix_pr():
        data = request.get_json(silent=True) or {}
        path = (data.get("path") or "").strip()
        issue_label = (data.get("issue_label") or "").strip() or path
        if not path:
            return jsonify({"error": "A file path is required."}), 400
        owner, repo = _current_repo()
        if not owner or not repo:
            return jsonify({"error": "No repository selected."}), 400
        api = get_api()
        try:
            ref = (data.get("ref") or "").strip() or api.get_default_branch(owner, repo)
            content = api.fetch_file_content(owner, repo, path, ref=ref)
            analysis = ai_analyzer.analyze_code(path, content, context=f"{owner}/{repo}")
            fixed_code = analysis.get("fixed_code") or ""
            if not fixed_code:
                return jsonify({"error": "AI did not produce a fix for this file."}), 400
            outcome = fixer.create_fix_pull_request(
                api,
                owner,
                repo,
                path,
                issue_label,
                analysis,
                fixed_code,
            )
        except GitHubError as exc:
            return jsonify({"error": exc.message}), 400
        code = 201 if outcome.get("status") == "created" else 400
        return jsonify(outcome), code


def register_api(app: Flask) -> None:
    """Attach the JSON API routes to the app."""
    register_api_routes(app)


# ======================================================================
# Error handlers
# ======================================================================
def register_error_handlers(app: Flask) -> None:
    from config.logging_setup import get_logger

    logger = get_logger("app")

    @app.errorhandler(404)
    def not_found(error):
        return render_template("base.html", _error_page=True, _code=404, _message="Page not found"), 404

    @app.errorhandler(403)
    def forbidden(error):
        return _render_unauthorized("unknown", "forbidden")

    @app.errorhandler(500)
    def internal_error(error):
        logger.exception("Unhandled exception: %s", error)
        return render_template("base.html", _error_page=True, _code=500, _message="Internal server error"), 500


# ======================================================================
# Entry point
# ======================================================================
app = create_app()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
