"""
GitPulse - safe AI auto-fix workflow.

Rules enforced here (never violated by design):
    * AI-generated code is NEVER pushed to the default branch.
    * AI-generated code is NEVER merged automatically.
    * Files are NEVER deleted automatically.
    * Only the target file's content is changed, via the Git Data API.
    * No shell commands are executed from AI output. Validation, when
      enabled, runs only the whitelisted commands from the environment.

Flow:
    1. Analyze the issue / generate a fix (AI or rule-based).
    2. Create a feature branch  ai-fix/<slug>-<timestamp>  from the
       default branch.
    3. Commit the fixed file to that branch (Git Data API).
    4. If AI_FIX_LOCAL_VALIDATION=1 and git is available: clone the
       branch to a temp dir, apply the fix and run the configured
       test/lint commands. If they fail, the workflow stops and NO PR is
       created.
    5. Open a Pull Request describing the problem and the fix.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from typing import Any, Optional

from config.logging_setup import get_logger
from config.settings import settings

logger = get_logger("app")


def _slugify(text: str, max_len: int = 40) -> str:
    """Turn a title into a safe branch slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if not slug:
        slug = "fix"
    return slug[:max_len].rstrip("-")


def build_fix_branch_name(issue_label: str) -> str:
    """Branch name for a fix, e.g. ai-fix/null-check-1698000000."""
    return f"ai-fix/{_slugify(issue_label)}-{int(time.time())}"


def generate_fix(api: object, owner: str, repo: str, path: str, base_ref: str) -> dict[str, Any]:
    """
    Fetch a file from GitHub, run AI analysis on it and produce the fix.

    Returns a dict with the analysis result, the fixed content (if any),
    and the base branch SHA to branch off from.
    """
    from utils import ai_analyzer

    content = api.fetch_file_content(owner, repo, path, ref=base_ref)
    analysis = ai_analyzer.analyze_code(path, content, context=f"{owner}/{repo}")
    fixed_code = analysis.get("fixed_code") or ""

    return {
        "path": path,
        "base_ref": base_ref,
        "original_content": content,
        "analysis": analysis,
        "fixed_code": fixed_code,
    }


def validate_locally(
    owner: str,
    repo: str,
    branch: str,
    path: str,
    fixed_content: str,
    api: Optional[object] = None,
) -> dict[str, Any]:
    """
    Optionally validate a fix by cloning the branch into a temp dir,
    applying the fix and running the configured test/lint commands.

    Disabled unless AI_FIX_LOCAL_VALIDATION=1. Returns a dict with
    ok, command(s) run, output, and a human-readable summary.
    """
    if not settings.AI_FIX_LOCAL_VALIDATION:
        return {
            "ok": True,
            "skipped": True,
            "detail": "Local validation is disabled (set AI_FIX_LOCAL_VALIDATION=1 to enable).",
        }

    git = shutil.which("git")
    if not git:
        return {
            "ok": False,
            "skipped": True,
            "detail": "git executable not found; cannot run local validation.",
        }

    commands = [settings.AI_FIX_TEST_CMD]
    if settings.AI_FIX_LINT_CMD:
        commands.append(settings.AI_FIX_LINT_CMD)

    workdir = tempfile.mkdtemp(prefix="gitpulse-fix-")
    try:
        repo_url = f"https://github.com/{owner}/{repo}.git"
        clone_env = os.environ.copy()
        clone_env["GIT_TERMINAL_PROMPT"] = "0"

        token = getattr(api, "token", "") if api is not None else ""
        if token:
            # Authenticate private-repository clones through askpass instead
            # of putting the token in the URL or process arguments.
            askpass = os.path.join(workdir, ".gitpulse-askpass")
            with open(askpass, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\nprintf '%s\n' \"$GITPULSE_GIT_TOKEN\"\n")
            os.chmod(askpass, 0o700)
            clone_env["GIT_ASKPASS"] = askpass
            clone_env["GITPULSE_GIT_TOKEN"] = token

        subprocess.run(
            [git, "clone", "--depth", "1", "--branch", branch, repo_url, workdir],
            check=True,
            capture_output=True,
            timeout=120,
            env=clone_env,
        )

        target = os.path.abspath(os.path.join(workdir, path))
        workdir_real = os.path.realpath(workdir) + os.sep
        if not os.path.realpath(target).startswith(workdir_real):
            return {
                "ok": False,
                "skipped": False,
                "detail": "Refusing to write outside the checkout.",
            }
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(fixed_content)

        results = []
        for command in commands:
            try:
                proc = subprocess.run(
                    shlex.split(command),
                    cwd=workdir,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                results.append(
                    {
                        "command": command,
                        "returncode": proc.returncode,
                        "output": (proc.stdout or proc.stderr)[-2000:],
                    }
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                results.append(
                    {"command": command, "returncode": -1, "output": f"Failed to run: {exc}"}
                )

        ok = all(r["returncode"] == 0 for r in results)
        return {
            "ok": ok,
            "skipped": False,
            "detail": "Validation passed." if ok else "Validation failed - see command output.",
            "results": results,
        }
    except Exception as exc:  # noqa: BLE001 - never let validation crash the flow
        logger.warning("Local validation could not run: %s", exc)
        return {"ok": False, "skipped": True, "detail": f"Local validation error: {exc}"}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def create_fix_pull_request(
    api: object,
    owner: str,
    repo: str,
    path: str,
    issue_label: str,
    analysis: dict[str, Any],
    fixed_content: str,
) -> dict[str, Any]:
    """
    Run the full safe auto-fix workflow and return the outcome.

    Returns a dict with status, branch, pr_url (if created), validation
    info, and any error message.
    """
    from utils.store import get_store

    try:
        default_branch = api.get_default_branch(owner, repo)
    except Exception as exc:  # noqa: BLE001
        get_store().save_fix_attempt(
            status="error", path=path, error=f"Could not read default branch: {exc}"
        )
        return {"status": "error", "error": f"Could not read default branch: {exc}"}

    branch = build_fix_branch_name(issue_label or path)

    try:
        base_sha = api.get_branch_sha(owner, repo, default_branch)
        api.create_branch(owner, repo, branch, base_sha)
    except Exception as exc:  # noqa: BLE001
        get_store().save_fix_attempt(
            status="error", branch=branch, path=path, error=f"Branch creation failed: {exc}"
        )
        return {"status": "error", "branch": branch, "error": f"Branch creation failed: {exc}"}

    try:
        commit_message = (
            f"AI Fix: {issue_label}\n\n"
            f"{analysis.get('explanation', '')}\n\n"
            "Generated by GitPulse AI. Reviewed by a human before merge."
        )
        api.commit_file_via_api(owner, repo, branch, path, fixed_content, commit_message)
    except Exception as exc:  # noqa: BLE001
        get_store().save_fix_attempt(
            status="error", branch=branch, path=path, error=f"Commit failed: {exc}"
        )
        return {"status": "error", "branch": branch, "error": f"Commit failed: {exc}"}

    # Optional local validation. When it fails, the workflow stops and no
    # pull request is created - AI code must stay reviewable.
    validation = validate_locally(owner, repo, branch, path, fixed_content, api=api)
    if not validation.get("ok", False):
        get_store().save_fix_attempt(
            status="validation_failed",
            branch=branch,
            path=path,
            validation=validation.get("detail", ""),
        )
        return {
            "status": "validation_failed",
            "branch": branch,
            "error": validation.get("detail", "Validation failed."),
            "validation": validation,
        }

    try:
        body = (
            "## Problem\n"
            f"{analysis.get('problem', '')}\n\n"
            "## Root Cause\n"
            f"{analysis.get('explanation', '')}\n\n"
            "## AI Suggested Fix\n"
            f"{analysis.get('suggested_fix', '')}\n\n"
            "## Validation\n"
            f"{validation.get('detail', 'No local validation run.')}\n\n"
            "> Generated by GitPulse AI. AI-generated code must always remain "
            "reviewable by humans. **No automated merge is performed.**"
        )
        pr = api.create_pull_request(
            owner, repo, title=f"AI Fix: {issue_label}", head=branch, base=default_branch, body=body
        )
        pr_url = pr.get("html_url", "")
        get_store().save_fix_attempt(
            status="created",
            branch=branch,
            pr_url=pr_url,
            path=path,
            validation=validation.get("detail", ""),
        )
        return {
            "status": "created",
            "branch": branch,
            "pr_url": pr_url,
            "pr_number": pr.get("number"),
            "validation": validation,
            "title": f"AI Fix: {issue_label}",
        }
    except Exception as exc:  # noqa: BLE001
        get_store().save_fix_attempt(
            status="error",
            branch=branch,
            path=path,
            error=f"Pull request creation failed: {exc}",
        )
        return {
            "status": "error",
            "branch": branch,
            "error": f"Pull request creation failed: {exc}",
        }
