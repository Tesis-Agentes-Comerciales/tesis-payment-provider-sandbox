"""Real pytest subprocess regressions for the CI gate, separate from payment behavior."""

import copy
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from scripts import ci_evidence

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("source", "options", "expected"),
    [
        ("def test_case(): pass", [], "passed"),
        ("def test_case(): assert False, 'synthetic-token-value'", [], "failed"),
        (
            "import pytest\n@pytest.fixture\ndef value(): raise RuntimeError('setup')\n"
            "def test_case(value): pass",
            [],
            "error",
        ),
        (
            "import pytest\n@pytest.fixture\ndef value():\n yield\n"
            " raise RuntimeError('teardown')\n"
            "def test_case(value): pass",
            [],
            "error",
        ),
        ("import pytest\n@pytest.mark.skip\ndef test_case(): pass", [], "skipped"),
        (
            "import pytest\n@pytest.mark.xfail(strict=True)\ndef test_case(): assert False",
            [],
            "xfail",
        ),
        ("import pytest\n@pytest.mark.xfail\ndef test_case(): pass", [], "xpass"),
        ("import pytest\n@pytest.mark.xfail(strict=True)\ndef test_case(): pass", [], "xpass"),
        ("# empty", [], "empty"),
        ("def test_case(:", [], "collection_error"),
        ("def test_case(): pass", ["-k", "absent"], "deselected"),
        ("def test_case(): pass", ["--collect-only"], "not_executed"),
    ],
    ids=[
        "passed",
        "assertion-failed",
        "setup-error",
        "teardown-error",
        "skipped",
        "xfail",
        "xpass",
        "strict-xpass",
        "empty",
        "collection-error",
        "deselected",
        "collect-only",
    ],
)
def test_gate_observes_real_pytest_and_refuses_incomplete_runs(
    tmp_path: Path, source: str, options: list[str], expected: str
) -> None:
    (tmp_path / "test_subject.py").write_text(source + "\n", encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    outcomes = tmp_path / "outcomes.json"
    junit = tmp_path / "junit.xml"
    env = dict(
        os.environ, PYTHONPATH=str(ROOT), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTEST_ADDOPTS=""
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            "pytest.ini",
            "--noconftest",
            "-p",
            "scripts.ci_pytest",
            f"--ci-outcomes={outcomes}",
            f"--junitxml={junit}",
            "--tb=no",
            "--show-capture=no",
            *options,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    data = json.loads(outcomes.read_text(encoding="utf-8"))
    violations = ci_evidence.test_violations(data, ET.parse(junit).getroot())
    assert (result.returncode == 0) == (expected == "passed")
    assert (not violations) == (expected == "passed")
    expected_exit = {"passed": 0, "empty": 5, "deselected": 5, "collection_error": 2}.get(
        expected, 1
    )
    assert result.returncode == expected_exit
    if expected in {"skipped", "xfail"} or (expected == "xpass" and "strict=True" not in source):
        assert data["pytest_exit_code"] == 0  # The gate, not a pytest internal error, refuses it.
    assert "synthetic-token-value" not in result.stdout + result.stderr + junit.read_text()
    if expected in data["counts"]:
        assert data["counts"][expected] == 1
    elif expected == "collection_error":
        assert data["collection_errors"]
    elif expected == "deselected":
        assert data["deselected"] == 1
    else:
        assert data["collected"] == 0


@pytest.mark.parametrize("outcome", ["not_executed", "skipped", "failure", "success"])
def test_final_gate_requires_every_quality_step_and_writes_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in (".python-version", "pyproject.toml", "uv.lock"):
        (tmp_path / name).write_text("3.12.14\n", encoding="utf-8")
    (tmp_path / "dist").mkdir()
    for name in ("package.whl", "package.tar.gz"):
        (tmp_path / "dist" / name).write_bytes(b"synthetic build artifact")
    steps = {name: {"outcome": "success"} for name in ci_evidence.REQUIRED["quality"]}
    if outcome == "not_executed":
        del steps["types"]
    else:
        steps["types"]["outcome"] = outcome
    monkeypatch.setenv("CI_STEPS_JSON", json.dumps(steps))
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(sys, "argv", ["ci_evidence.py", "quality"])

    def command(*args: str) -> str:
        return "a" * 40 if args[0] == "git" else "Python 3.12.14"

    monkeypatch.setattr(ci_evidence, "command", command)
    assert ci_evidence.main() == (0 if outcome == "success" else 1)
    directory = tmp_path / ".local/ci/quality"
    assert (directory / "SHA256SUMS").exists()
    report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == ("passed" if outcome == "success" else "failed")


def test_junit_mismatch_and_omitted_phase_cannot_validate_a_report() -> None:
    data: dict[str, Any] = {
        "tests": [
            {
                "nodeid": "case",
                "status": "passed",
                "phases": [
                    {"phase": phase, "status": "passed"} for phase in ("setup", "call", "teardown")
                ],
            }
        ],
        "collected": 1,
        "status": "passed",
        "gate_exit_code": 0,
        "pytest_exit_code": 0,
        "collection_errors": [],
        "deselected": 0,
        "counts": {"passed": 1},
    }
    junit = ET.fromstring('<testsuites><testsuite tests="1"><testcase /></testsuite></testsuites>')
    assert not ci_evidence.test_violations(data, junit)
    changed = copy.deepcopy(data)
    changed["tests"][0]["phases"].pop()
    assert "required_tests_not_passed" in ci_evidence.test_violations(changed, junit)
    junit.find("testsuite").set("tests", "2")  # type: ignore[union-attr]
    assert "junit_header_not_passed_or_inconsistent" in ci_evidence.test_violations(data, junit)


def test_published_junit_retains_outcomes_without_assertion_values() -> None:
    root = ET.fromstring(
        '<testsuites><testsuite tests="1"><testcase name="case">'
        '<failure message="synthetic-token">synthetic-token</failure>'
        "<system-out>synthetic-token</system-out></testcase></testsuite></testsuites>"
    )
    ci_evidence.sanitize_junit(root)
    assert len(list(root.iter("failure"))) == 1
    assert b"synthetic-token" not in ET.tostring(root)
