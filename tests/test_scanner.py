"""Tests for the rule-based static code scanner."""

import base64

from utils.code_scanner import CodeScanner, Finding, analyze_python_content


class TestScanPath:
    def test_detects_hardcoded_secret(self, tmp_path):
        target = tmp_path / "app.py"
        target.write_text('password = "s3cr3t-value-123"\n', encoding="utf-8")

        findings = CodeScanner().scan_path(str(tmp_path))

        assert len(findings) == 1
        assert findings[0].rule_id == "HARDCODED_SECRET"
        assert findings[0].severity == "CRITICAL"
        assert findings[0].filename == str(target)
        assert findings[0].line_number == 1

    def test_skips_hidden_and_venv_dirs(self, tmp_path):
        (tmp_path / ".hidden").mkdir()
        (tmp_path / ".hidden" / "app.py").write_text(
            'password = "abcdefgh123456"\n', encoding="utf-8"
        )
        (tmp_path / ".venv").mkdir()
        (tmp_path / ".venv" / "app.py").write_text(
            'password = "abcdefgh123456"\n', encoding="utf-8"
        )

        assert CodeScanner().scan_path(str(tmp_path)) == []

    def test_excludes_rule_definition_files(self, tmp_path):
        (tmp_path / "code_scanner.py").write_text(
            'recommendation = "Remove eval() usage"\n', encoding="utf-8"
        )

        assert CodeScanner().scan_path(str(tmp_path)) == []

    def test_respects_max_files(self, tmp_path):
        for i in range(5):
            (tmp_path / f"f{i}.py").write_text(
                'password = "abcdefgh123456"\n', encoding="utf-8"
            )

        findings = CodeScanner().scan_path(str(tmp_path), max_files=2)

        assert len(findings) == 2

    def test_excludes_test_files_by_default(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_a.py").write_text(
            'password = "abcdefgh123456"\n', encoding="utf-8"
        )
        (tmp_path / "test_b.py").write_text(
            'password = "abcdefgh123456"\n', encoding="utf-8"
        )
        (tmp_path / "app.py").write_text(
            'password = "abcdefgh123456"\n', encoding="utf-8"
        )

        default_findings = CodeScanner().scan_path(str(tmp_path))
        assert len(default_findings) == 1
        assert default_findings[0].filename.endswith("app.py")

        all_findings = CodeScanner().scan_path(str(tmp_path), exclude_tests=False)
        assert len(all_findings) == 3


class TestScanGitHubRepo:
    def _api(self, tree):
        class FakeAPI:
            def _request(self, method, path, params=None, retries=3):
                if "trees" in path:
                    return {"tree": tree}
                content = b'password = "hunter2secretvalue"\n'
                return {"encoding": "base64", "content": base64.b64encode(content).decode()}

        return FakeAPI()

    def test_scans_scannable_blobs(self):
        tree = [
            {"type": "blob", "path": "app.py", "url": "b1", "size": 100},
            {"type": "blob", "path": "notes.txt", "url": "b2", "size": 100},
        ]

        findings = CodeScanner().scan_github_repo(self._api(tree), "o", "r")

        assert [f.rule_id for f in findings] == ["HARDCODED_SECRET"]
        assert findings[0].filename == "app.py"

    def test_github_scan_skips_rule_files(self):
        tree = [{"type": "blob", "path": "code_scanner.py", "url": "b1", "size": 100}]

        assert CodeScanner().scan_github_repo(self._api(tree), "o", "r") == []

    def test_github_scan_skips_test_paths_by_default(self):
        tree = [
            {"type": "blob", "path": "tests/test_x.py", "url": "b1", "size": 100},
            {"type": "blob", "path": "src/app.py", "url": "b2", "size": 100},
        ]

        findings = CodeScanner().scan_github_repo(self._api(tree), "o", "r")

        assert [f.filename for f in findings] == ["src/app.py"]

        all_findings = CodeScanner().scan_github_repo(
            self._api(tree), "o", "r", exclude_tests=False
        )
        assert len(all_findings) == 2

    def test_tolerates_tree_fetch_failure(self, monkeypatch):
        api = self._api([])

        def boom(method, path, params=None, retries=3):
            raise RuntimeError("network down")

        monkeypatch.setattr(api, "_request", boom)

        assert CodeScanner().scan_github_repo(api, "o", "r") == []


class TestReporting:
    def _finding(self, rule_id="R1", severity="LOW"):
        return Finding(
            rule_id=rule_id,
            severity=severity,
            filename="f.py",
            line_number=1,
            line_content="x",
            description="d",
            recommendation="r",
        )

    def test_summarize_counts_by_severity(self):
        findings = [
            self._finding("A", "CRITICAL"),
            self._finding("B", "HIGH"),
            self._finding("C", "LOW"),
            self._finding("D", "MEDIUM"),
        ]

        summary = CodeScanner.summarize(findings)

        assert summary["CRITICAL"] == 1
        assert summary["HIGH"] == 1
        assert summary["MEDIUM"] == 1
        assert summary["LOW"] == 1
        assert summary["TOTAL"] == 4

    def test_severity_sort_key_orders_critical_first(self):
        findings = [self._finding("A", "LOW"), self._finding("B", "CRITICAL")]
        findings.sort(key=CodeScanner.severity_sort_key)
        assert findings[0].rule_id == "B"

    def test_to_dict_shapes_output(self):
        data = self._finding().to_dict()
        assert set(data) == {
            "rule_id",
            "severity",
            "filename",
            "line_number",
            "line_content",
            "description",
            "recommendation",
        }


class TestAnalyzePythonContent:
    def test_flags_syntax_error(self):
        findings = analyze_python_content("bad.py", "def foo(:\n    pass\n")

        assert len(findings) == 1
        assert findings[0].rule_id == "SYNTAX_ERROR"
        assert findings[0].severity == "HIGH"
        assert findings[0].line_number == 1

    def test_flags_undefined_name(self):
        findings = analyze_python_content("u.py", "x = 1\nprint(github_data)\n")

        assert [f.rule_id for f in findings] == ["UNDEFINED_NAME"]
        assert findings[0].line_number == 2
        assert "github_data" in findings[0].description

    def test_flags_unused_imports(self):
        findings = analyze_python_content("i.py", "import os\nimport json\nx = 1\n")

        assert [f.rule_id for f in findings] == ["UNUSED_IMPORT", "UNUSED_IMPORT"]
        assert all(f.severity == "LOW" for f in findings)
        assert all("os" in f.description or "json" in f.description for f in findings)

    def test_clean_file_has_no_findings(self):
        content = (
            "import math\n"
            "def area(r):\n"
            "    return math.pi * r * r\n"
            "print(area(2))\n"
        )
        assert analyze_python_content("ok.py", content) == []

    def test_does_not_report_defined_names(self):
        content = (
            "def build():\n"
            "    x = 1\n"
            "    return x\n"
            "result = build()\n"
            "print(result)\n"
        )
        assert analyze_python_content("d.py", content) == []

    def test_scan_path_includes_python_syntax_error(self, tmp_path):
        target = tmp_path / "app.py"
        target.write_text("def broken(:\n    pass\n", encoding="utf-8")

        findings = CodeScanner().scan_path(str(tmp_path))

        assert any(f.rule_id == "SYNTAX_ERROR" for f in findings)
