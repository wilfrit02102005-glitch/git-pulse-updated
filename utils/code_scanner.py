"""
GitPulse - rule-based static code scanner.

A lightweight, dependency-free code scanner built on regular expressions.
It is intentionally simple: it flags suspicious patterns and always lets
a human make the final call. It is NOT a replacement for real SAST tools
like Semgrep, Bandit or CodeQL.

Scan targets:
  * Local directories (walked recursively).
  * GitHub repositories (files fetched via the git trees API).

Each finding includes: file name, line number, severity, description and
a concrete recommendation, exactly as required by the dashboard.
"""

from __future__ import annotations

import ast
import builtins
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional, Union

from config.logging_setup import get_logger

logger = get_logger("scanner")

# File extensions we know how to analyze.
SCANNABLE_EXTENSIONS = {".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rb"}

# GitHub repos are huge; cap the amount of code we pull.
MAX_GITHUB_FILES = 300
MAX_FILE_SIZE_BYTES = 512 * 1024  # skip files larger than 512 KB

# Files whose *contents define the rules* must never be scanned: their rule
# patterns and recommendation text literally contain the trigger strings
# (e.g. `eval(`, `except:`, "TODO") and would always self-flag. Excluded by
# basename so the same protection applies to local and GitHub scans.
SELF_SCAN_EXCLUSIONS = frozenset({os.path.basename(__file__)})

# Test files/directories are skipped by default: fixtures legitimately contain
# fake secrets and trigger strings, so findings there are almost always noise
# (real SAST tools apply the same default). Both can be overridden per call.
TEST_DIR_NAMES = frozenset({"tests", "test", "spec", "testing"})
DEFAULT_EXCLUDE_DIRS = frozenset({"node_modules", "venv", ".venv", "dist", "build"})


def _is_test_file(filename: str) -> bool:
    """True for pytest-style test modules and test config."""
    name = filename.lower()
    return (
        name == "conftest.py"
        or name == "test.py"
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


@dataclass(frozen=True)
class Rule:
    """A single detection rule."""

    rule_id: str
    severity: str  # CRITICAL | HIGH | MEDIUM | LOW
    extensions: set[str]
    pattern: re.Pattern
    description: str
    recommendation: str

    def describe(self, filename: str, line_no: int) -> str:
        return f"{filename}:{line_no} | {self.rule_id} | {self.severity}"


@dataclass
class Finding:
    """One detection result."""

    rule_id: str
    severity: str
    filename: str
    line_number: int
    line_content: str
    description: str
    recommendation: str

    # Keys whose values look like credentials. Any of these on a matched
    # line gets masked so real secrets never reach the UI or the API.
    _SECRET_KEY_RE = re.compile(
        r"(?i)\b(password|passwd|secret|api[_-]?key|client[_-]?secret|"
        r"access[_-]?token|auth[_-]?token|token|secret[_-]?key|apikey|"
        r"private[_-]?key|connection[_-]?string)\b\s*"
    )

    @staticmethod
    def _mask_line(line: str) -> str:
        """
        Redact credential-looking values on a single line.

        `API_KEY = "sk-abc123"` becomes `API_KEY = "********"`. Any
        remainder of the line after the value is also dropped because
        following tokens could belong to a secret (e.g. a comma list).
        """
        match = Finding._SECRET_KEY_RE.search(line)
        if not match:
            return line
        value_start = match.end()
        rest = line[value_start:]
        # Skip an assignment operator and optional whitespace/quotes.
        rest = rest.lstrip()
        if rest.startswith(("=", ":")):
            rest = rest[1:].lstrip()
            if rest.startswith(('"', "'", "`")):
                rest = rest[1:]
        if not rest:
            return line
        # Only mask when a plausible value is present (>=4 chars), so
        # lines like `password = ""` or `token = None` stay readable.
        raw = rest.rstrip()
        if len(raw) < 4 or raw.startswith(("None", "null", "False", "True", "0", "1")):
            return line
        return line[:value_start] + "= \"********\""

    def to_dict(self) -> dict:
        """Serialize for templates / JSON endpoints (secrets masked)."""
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "filename": self.filename,
            "line_number": self.line_number,
            "line_content": self._mask_line(self.line_content).strip()[:120],
            "description": self.description,
            "recommendation": self.recommendation,
        }


# ----------------------------------------------------------------------
# Rule definitions
# ----------------------------------------------------------------------
def _compile(pattern: str, flags: int = re.IGNORECASE) -> re.Pattern:
    return re.compile(pattern, flags)


RULES: list[Rule] = [
    Rule(
        rule_id="HARDCODED_SECRET",
        severity="CRITICAL",
        extensions={".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rb"},
        pattern=_compile(
            r"(?i)(password|passwd|secret|api[_-]?key|client[_-]?secret|"
            r"access[_-]?token|auth[_-]?token)\s*[:=]\s*['\"][^'\"]{8,}['\"]"
        ),
        description="Possible hard-coded secret (password / API key / token).",
        recommendation="Move the value to environment variables or a secret manager, and rotate the leaked value immediately.",
    ),
    Rule(
        rule_id="SQL_INJECTION",
        severity="HIGH",
        extensions={".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rb"},
        pattern=_compile(
            r"\.execute\s*\(\s*[\"']SELECT|f['\"]SELECT.*\bWHERE\b.*\{|"
            r"query\s*[:=]\s*[\"'].*\bwhere\b.*%\s*\(|"
            r"(\+\s*|%\()\s*\w+\s*(\))?\s*\)?\s*\)\s*;?\s*$",
            re.IGNORECASE,
        ),
        description="Potential SQL injection: user input may be concatenated into a query.",
        recommendation="Use parameterized queries / prepared statements and an ORM instead of string interpolation.",
    ),
    Rule(
        rule_id="EVAL_USAGE",
        severity="HIGH",
        extensions={".py", ".js", ".ts", ".jsx", ".tsx"},
        pattern=_compile(r"\beval\s*\("),
        description="Use of eval(): executes arbitrary code at runtime.",
        recommendation="Remove eval(). Use safer alternatives (json.loads, ast.literal_eval, Function constructor only for trusted data).",
    ),
    Rule(
        rule_id="SHELL_INJECTION",
        severity="CRITICAL",
        extensions={".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rb"},
        pattern=_compile(
            r"os\.system\s*\(|subprocess\.(Popen|call|run)\s*\([^)]*\bshell\s*=\s*True|"
            r"child_process\.exec(File)?\s*\(|Runtime\.getRuntime\(\)\.exec|"
            r"system\(|\`.*\$\{.*\}\`"
        ),
        description="Potential shell injection: command constructed from untrusted input.",
        recommendation="Avoid shell=True / shell interpreters. Pass arguments as a list and validate input strictly.",
    ),
    Rule(
        rule_id="BARE_EXCEPT",
        severity="MEDIUM",
        extensions={".py"},
        pattern=_compile(r"\bexcept\s*:"),
        description="Bare except: silently swallows all exceptions including SystemExit/KeyboardInterrupt.",
        recommendation="Catch specific exception types and handle/log them explicitly.",
    ),
    Rule(
        rule_id="MUTABLE_DEFAULT",
        severity="MEDIUM",
        extensions={".py"},
        pattern=_compile(
            r"def\s+\w+\s*\([^)]*=\s*(\[\s*\]|\{\s*\}|set\(\))"
        ),
        description="Mutable default argument: shared across calls and can leak state.",
        recommendation="Use None as the default and initialize the mutable inside the function body.",
    ),
    Rule(
        rule_id="DANGEROUSLYSETHTML",
        severity="HIGH",
        extensions={".js", ".ts", ".jsx", ".tsx"},
        pattern=_compile(r"\.innerHTML\s*=|dangerouslySetInnerHTML\s*="),
        description="Rendering raw HTML: opens the door to XSS attacks.",
        recommendation="Use textContent / React's children instead, or sanitize the HTML with a library like DOMPurify.",
    ),
    Rule(
        rule_id="CONSOLE_LOG",
        severity="LOW",
        extensions={".js", ".ts", ".jsx", ".tsx"},
        pattern=_compile(r"console\.(log|debug|info)\s*\("),
        description="Debug logging left in production code.",
        recommendation="Remove debug logs or route them through a proper logging framework.",
    ),
    Rule(
        rule_id="PRINT_DEBUG",
        severity="LOW",
        extensions={".py", ".java", ".go", ".rb"},
        pattern=_compile(r"\bprint\s*\(|System\.out\.println|puts\s+\w+"),
        description="Debug print statement left in production code.",
        recommendation="Replace with structured logging (e.g. Python logging module).",
    ),
    Rule(
        rule_id="TODO_FIXME",
        severity="LOW",
        extensions=SCANNABLE_EXTENSIONS,
        pattern=_compile(r"TODO|FIXME|HACK|XXX"),
        description="Left-over marker comment indicating unfinished work.",
        recommendation="Resolve the task and remove the marker, or track it in your issue tracker.",
    ),
]

# Fast lookup: extension -> rules that apply to it.
_RULES_BY_EXTENSION: dict[str, list[Rule]] = {}
for rule in RULES:
    for ext in rule.extensions:
        _RULES_BY_EXTENSION.setdefault(ext, []).append(rule)


def _rules_for(extension: str) -> list[Rule]:
    """Return the rules that apply to a given file extension."""
    return _RULES_BY_EXTENSION.get(extension, [])


# ----------------------------------------------------------------------
# Deterministic Python checks (ast-based, no extra dependencies)
# ----------------------------------------------------------------------

# Names that are almost always available through a framework or stdlib import
# chain and cannot be resolved statically. They are exempted so the
# undefined-name check stays low-noise; anything else used but never bound
# in the file is flagged as a possible undefined variable.
_COMMON_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Flask / app context globals.
        "request", "session", "g", "current_app", "url_for", "redirect",
        "render_template", "flash", "jsonify", "abort", "make_response",
        "Response", "Flask", "Blueprint", "app",
        # Common stdlib modules (safe to treat as available).
        "os", "sys", "re", "json", "time", "datetime", "timedelta",
        "hashlib", "base64", "subprocess", "tempfile", "shutil", "pathlib",
        "sqlite3", "threading", "logging", "collections", "itertools",
        "functools", "typing", "dataclasses", "random", "math", "uuid",
        "io", "csv", "string", "argparse", "traceback", "contextlib",
        "copy", "glob", "signal", "asyncio", "decimal", "socket", "urllib",
        "requests", "dotenv", "pytest", "anthropic", "authlib", "flask",
        # Project-wide convenience imports used across modules.
        "settings", "logger", "get_logger",
        # Common typing names used in annotations.
        "Any", "Optional", "Dict", "List", "Tuple", "Set", "Iterable",
        "Iterator", "Callable", "Union", "Type", "Generator", "NoReturn",
        "Sequence", "Mapping", "Mapped", "Self", "override", "cast",
        # dunder-ish specials sometimes referenced explicitly.
        "__name__", "__file__", "__doc__", "__version__",
    }
)

_BUILTIN_NAMES: frozenset[str] = frozenset(builtins.__dict__.keys())


def _collect_bound_names(tree: ast.AST) -> set[str]:
    """Gather every name that is bound anywhere in the module."""
    bound: set[str] = set()

    class _Binder(ast.NodeVisitor):
        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                bound.add(node.id)

        def visit_FunctionDef(self, node) -> None:
            bound.add(node.name)
            for arg in list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs):
                bound.add(arg.arg)
            if node.args.vararg:
                bound.add(node.args.vararg.arg)
            if node.args.kwarg:
                bound.add(node.args.kwarg.arg)
            self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node) -> None:
            bound.add(node.name)
            self.generic_visit(node)

        def visit_Import(self, node) -> None:
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
            self.generic_visit(node)

        def visit_ImportFrom(self, node) -> None:
            for alias in node.names:
                bound.add(alias.asname or alias.name)
            self.generic_visit(node)

        def visit_For(self, node) -> None:
            for child in ast.walk(node.target):
                if isinstance(child, ast.Name):
                    bound.add(child.id)
            self.generic_visit(node)

        visit_AsyncFor = visit_For

        def visit_With(self, node) -> None:
            for item in node.items:
                if item.optional_vars:
                    for child in ast.walk(item.optional_vars):
                        if isinstance(child, ast.Name):
                            bound.add(child.id)
            self.generic_visit(node)

        visit_AsyncWith = visit_With

        def visit_ExceptHandler(self, node) -> None:
            if node.name:
                bound.add(node.name)
            self.generic_visit(node)

        def visit_ListComp(self, node) -> None:
            for gen in node.generators:
                for child in ast.walk(gen.target):
                    if isinstance(child, ast.Name):
                        bound.add(child.id)
            self.generic_visit(node)

        visit_SetComp = visit_ListComp
        visit_DictComp = visit_ListComp
        visit_GeneratorExp = visit_ListComp

        def visit_NamedExpr(self, node) -> None:
            for child in ast.walk(node.target):
                if isinstance(child, ast.Name):
                    bound.add(child.id)
            self.generic_visit(node)

        def visit_Global(self, node) -> None:
            bound.update(node.names)

        def visit_Nonlocal(self, node) -> None:
            bound.update(node.names)

    _Binder().visit(tree)
    return bound


def _undef_names(tree: ast.AST, bound: set[str]) -> list[tuple[str, int]]:
    """Return (name, lineno) for loads of names never bound in the module."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id not in bound
            and node.id not in _BUILTIN_NAMES
            and node.id not in _COMMON_ALLOWLIST
        ):
            found.append((node.id, node.lineno))
    return found


def _line_at(content: str, lineno: int) -> str:
    lines = content.splitlines()
    if 1 <= lineno <= len(lines):
        return lines[lineno - 1].strip()
    return ""


def analyze_python_content(filename: str, content: str) -> list[Finding]:
    """
    Deterministic Python checks using only the stdlib: syntax validation via
    `ast.parse` plus a light undefined-name / unused-import scan. Never raises.
    """
    findings: list[Finding] = []

    # 1. Syntax errors (highest signal, cheap).
    try:
        tree = ast.parse(content, filename=filename)
    except SyntaxError as exc:
        findings.append(
            Finding(
                rule_id="SYNTAX_ERROR",
                severity="HIGH",
                filename=filename,
                line_number=exc.lineno or 0,
                line_content=_line_at(content, exc.lineno or 0),
                description=f"Python syntax error: {exc.msg}",
                recommendation="Fix the reported syntax on this line so the module can be imported.",
            )
        )
        # No point running further AST checks on unparsable code.
        return findings
    except (ValueError, TypeError) as exc:
        return findings

    # 2. Undefined names (heuristic, always worded as "possible").
    bound = _collect_bound_names(tree)
    seen: set[tuple[str, int]] = set()
    for name, lineno in _undef_names(tree, bound):
        key = (name, lineno)
        if key in seen:
            continue
        seen.add(key)
        findings.append(
            Finding(
                rule_id="UNDEFINED_NAME",
                severity="MEDIUM",
                filename=filename,
                line_number=lineno,
                line_content=_line_at(content, lineno),
                description=f"Possible undefined variable: `{name}` is used but never defined or imported in this file.",
                recommendation=(
                    f"Define or import `{name}` before using it, or confirm it is "
                    "provided by the framework at runtime."
                ),
            )
        )

    # 3. Unused imports (dead code).
    used_names: set[str] = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = (alias.asname or alias.name).split(".")[0]
                if local not in used_names:
                    findings.append(
                        Finding(
                            rule_id="UNUSED_IMPORT",
                            severity="LOW",
                            filename=filename,
                            line_number=node.lineno,
                            line_content=_line_at(content, node.lineno),
                            description=f"Imported module `{alias.name}` is never used in this file.",
                            recommendation="Remove the unused import or start using it.",
                        )
                    )
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                local = alias.asname or alias.name
                if local not in used_names:
                    findings.append(
                        Finding(
                            rule_id="UNUSED_IMPORT",
                            severity="LOW",
                            filename=filename,
                            line_number=node.lineno,
                            line_content=_line_at(content, node.lineno),
                            description=f"Imported name `{local}` from `{node.module or ''}` is never used.",
                            recommendation="Remove the unused import or start using it.",
                        )
                    )

    return findings


def _scan_content(
    filename: str,
    content: str,
    rules: list[Rule],
    line_offset: int = 0,
) -> list[Finding]:
    """Run every rule against a single file's content."""
    findings: list[Finding] = []
    for rule in rules:
        for match in rule.pattern.finditer(content):
            # Compute the 1-based line number for this match.
            line_number = content.count("\n", 0, match.start()) + 1 + line_offset
            line_start = content.rfind("\n", 0, match.start()) + 1
            line_end = content.find("\n", match.end())
            if line_end == -1:
                line_end = len(content)
            line_content = content[line_start:line_end]

            findings.append(
                Finding(
                    rule_id=rule.rule_id,
                    severity=rule.severity,
                    filename=filename,
                    line_number=line_number,
                    line_content=line_content,
                    description=rule.description,
                    recommendation=rule.recommendation,
                )
            )
    return findings


class CodeScanner:
    """Scans local paths and GitHub repositories for risky patterns."""

    # ------------------------------------------------------------------
    # Local filesystem scanning
    # ------------------------------------------------------------------
    def scan_path(
        self,
        root: str,
        include_hidden: bool = False,
        max_files: int = 500,
        exclude_files: Optional[set[str]] = None,
        exclude_tests: bool = True,
    ) -> list[Finding]:
        """
        Recursively scan a local directory.

        Args:
            root:           Absolute path to scan.
            include_hidden: Whether to include dot-directories (e.g. .git).
            max_files:      Hard cap to avoid runaway scans.
            exclude_files:  Basenames to skip (rule-definition files that
                            would otherwise always self-flag).
            exclude_tests:  Skip test files and directories (default True).
        """
        exclude_files = set(exclude_files or SELF_SCAN_EXCLUSIONS)
        findings: list[Finding] = []
        scanned = 0

        for dirpath, dirnames, filenames in os.walk(root):
            if not include_hidden:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            dirnames[:] = [d for d in dirnames if d not in DEFAULT_EXCLUDE_DIRS]
            if exclude_tests:
                dirnames[:] = [d for d in dirnames if d not in TEST_DIR_NAMES]

            for filename in filenames:
                if filename in exclude_files:
                    continue
                if exclude_tests and _is_test_file(filename):
                    continue
                ext = os.path.splitext(filename)[1].lower()
                if ext not in SCANNABLE_EXTENSIONS:
                    continue

                full_path = os.path.join(dirpath, filename)
                try:
                    if os.path.getsize(full_path) > MAX_FILE_SIZE_BYTES:
                        continue
                    with open(full_path, "r", encoding="utf-8", errors="ignore") as fh:
                        content = fh.read()
                except OSError as exc:
                    logger.warning("Skipping %s: %s", full_path, exc)
                    continue

                findings.extend(_scan_content(full_path, content, _rules_for(ext)))
                if ext == ".py":
                    findings.extend(analyze_python_content(full_path, content))
                scanned += 1
                if scanned >= max_files:
                    logger.warning("Reached max_files (%d); stopping scan.", max_files)
                    break
            if scanned >= max_files:
                break

        logger.info(
            "Scanned %d files under %s -> %d findings",
            scanned, root, len(findings),
        )
        return findings

    # ------------------------------------------------------------------
    # GitHub repository scanning
    # ------------------------------------------------------------------
    def scan_github_repo(
        self,
        api: object,
        owner: str,
        repo: str,
        branch: str = "HEAD",
        max_files: int = MAX_GITHUB_FILES,
        exclude_files: Optional[set[str]] = None,
        exclude_tests: bool = True,
    ) -> list[Finding]:
        """
        Scan a remote GitHub repository using the git trees API.

        Args:
            api:           A GitHubAPI instance (used to fetch the tree + blobs).
            owner:         Repository owner.
            repo:          Repository name.
            branch:        Branch ref to scan (defaults to the default branch).
            exclude_files: Basenames to skip (rule-definition files that
                           would otherwise always self-flag).
            exclude_tests: Skip test files and directories (default True).
        """
        exclude_files = set(exclude_files or SELF_SCAN_EXCLUSIONS)
        findings: list[Finding] = []
        try:
            # 1. Get the recursive file tree for the branch.
            tree = api._request(
                "GET", f"/repos/{owner}/{repo}/git/trees/{branch}", params={"recursive": "1"}
            ).get("tree", [])
        except Exception as exc:  # noqa: BLE001 - scanner must never crash a route
            logger.error("Could not fetch git tree for %s/%s: %s", owner, repo, exc)
            return findings

        blobs = [
            item
            for item in tree
            if item.get("type") == "blob"
            and item.get("path", "").split("/")[-1] not in {"package-lock.json", "yarn.lock", "poetry.lock", "Pipfile.lock"}
        ][:max_files]

        for item in blobs:
            path = item.get("path", "")
            path_segments = path.split("/")
            if os.path.basename(path) in exclude_files:
                continue
            if exclude_tests and (
                _is_test_file(path_segments[-1])
                or any(seg in TEST_DIR_NAMES for seg in path_segments[:-1])
            ):
                continue
            ext = os.path.splitext(path)[1].lower()
            if ext not in SCANNABLE_EXTENSIONS:
                continue
            if (item.get("size") or 0) > MAX_FILE_SIZE_BYTES:
                continue

            try:
                blob = api._request("GET", item["url"])
                content = blob.get("content", "")
                if blob.get("encoding") == "base64":
                    import base64
                    content = base64.b64decode(content).decode("utf-8", errors="ignore")
            except Exception as exc:  # noqa: BLE001
                logger.debug("Skipping %s: %s", path, exc)
                continue

            findings.extend(_scan_content(path, content, _rules_for(ext)))
            if ext == ".py":
                findings.extend(analyze_python_content(path, content))

        logger.info("Scanned GitHub repo %s/%s -> %d findings", owner, repo, len(findings))
        return findings

    # ------------------------------------------------------------------
    # Reporting helpers
    # ------------------------------------------------------------------
    @staticmethod
    def summarize(findings: list[Finding]) -> dict[str, int]:
        """Count findings per severity for the dashboard summary cards."""
        summary = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "TOTAL": len(findings)}
        for finding in findings:
            severity = finding.severity
            if severity in summary:
                summary[severity] += 1
        return summary

    @staticmethod
    def severity_sort_key(finding: Finding) -> int:
        """Sort so CRITICAL appears first in tables."""
        order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
        return order.get(finding.severity, 99)
