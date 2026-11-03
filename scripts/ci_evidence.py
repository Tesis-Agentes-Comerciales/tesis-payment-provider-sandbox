"""Stdlib-only final gate and allowlisted evidence; never reads settings, secrets or databases."""

import hashlib
import json
import os
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

REQUIRED = {
    "quality": ("checkout", "uv", "dependencies", "format", "lint", "types", "build"),
    "tests": ("checkout", "uv", "dependencies", "pytest"),
}


def test_violations(outcomes: dict[str, Any], junit: ET.Element) -> list[str]:
    tests = outcomes["tests"]
    cases = list(junit.iter("testcase"))
    violations = []
    if not tests or len(tests) != outcomes["collected"] or len(cases) != len(tests):
        violations.append("empty_or_inconsistent_collection")
    if outcomes["status"] != "passed" or outcomes["gate_exit_code"] != 0:
        violations.append("pytest_gate_failed")
    if outcomes["pytest_exit_code"] != 0 or outcomes["collection_errors"] or outcomes["deselected"]:
        violations.append("pytest_session_incomplete")
    if len({test["nodeid"] for test in tests}) != len(tests):
        violations.append("duplicate_test_identity")
    if any(
        test["status"] != "passed"
        or sorted((p["phase"], p["status"]) for p in test["phases"])
        != [("call", "passed"), ("setup", "passed"), ("teardown", "passed")]
        for test in tests
    ):
        violations.append("required_tests_not_passed")
    if any(case.find(tag) is not None for case in cases for tag in ("failure", "error", "skipped")):
        violations.append("junit_not_passed")
    suites = list(junit.iter("testsuite"))
    if sum(int(s.attrib["tests"]) for s in suites) != len(cases) or any(
        int(s.attrib.get(key, "0")) for s in suites for key in ("failures", "errors", "skipped")
    ):
        violations.append("junit_header_not_passed_or_inconsistent")
    if outcomes["counts"]["passed"] != len(tests) or sum(outcomes["counts"].values()) != len(tests):
        violations.append("inconsistent_outcome_counts")
    return violations


def command(*args: str) -> str | None:
    try:
        return subprocess.check_output(
            args, text=True, stderr=subprocess.DEVNULL, timeout=30
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sanitize_junit(root: ET.Element) -> None:
    # Preserve identities, outcomes and timing; assertion values can contain synthetic tokens.
    for parent in root.iter():
        for child in list(parent):
            if child.tag in {"system-out", "system-err", "properties"}:
                parent.remove(child)
            elif child.tag in {"failure", "error", "skipped"}:
                child.attrib.clear()
                child.set("message", f"{child.tag}; see individual phase outcomes")
                child.text = "Assertion and exception values omitted from published evidence."


def main() -> int:
    kind = sys.argv[1]
    if kind not in REQUIRED:
        raise ValueError("Unknown CI job")
    root = Path.cwd()
    directory = root / ".local" / "ci" / kind
    directory.mkdir(parents=True, exist_ok=True)
    violations: list[str] = []
    try:
        supplied = json.loads(os.environ.get("CI_STEPS_JSON", "{}"))
        steps = {
            name: supplied.get(name, {}).get("outcome", "not_executed") for name in REQUIRED[kind]
        }
    except (ValueError, AttributeError, TypeError):
        steps = dict.fromkeys(REQUIRED[kind], "not_executed")
    violations.extend(
        f"step_not_successful:{name}" for name, value in steps.items() if value != "success"
    )
    outcomes = None
    if kind == "tests":
        try:
            tree = ET.parse(directory / "junit.xml")
            sanitize_junit(tree.getroot())
            tree.write(directory / "junit.xml", encoding="utf-8", xml_declaration=True)
            outcomes = json.loads((directory / "outcomes.json").read_text(encoding="utf-8"))
            violations.extend(test_violations(outcomes, tree.getroot()))
        except (OSError, ValueError, KeyError, TypeError, AttributeError, ET.ParseError):
            outcomes = None
            violations.append("test_evidence_missing_or_invalid")
    actual_sha = command("git", "rev-parse", "HEAD")
    actions_sha = os.environ.get("GITHUB_SHA")
    if actual_sha is None or (actions_sha and actions_sha != actual_sha):
        violations.append("checkout_identity_unverified")
    interpreter = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    runtime = command(str(interpreter), "--version")
    if runtime != "Python " + (root / ".python-version").read_text().strip():
        violations.append("python_version_mismatch")
    if kind == "quality" and (
        not list((root / "dist").glob("*.whl")) or not list((root / "dist").glob("*.tar.gz"))
    ):
        violations.append("build_distributions_missing")
    source_paths = [root / p for p in (".python-version", "pyproject.toml", "uv.lock")]
    for folder in ("scripts", "tests", ".github"):
        source_paths.extend(
            p for p in (root / folder).rglob("*") if p.suffix in {".py", ".yml", ".txt"}
        )
    evidence = {
        "schema_version": 1,
        "status": "passed" if not violations else "failed",
        "job": kind,
        "violations": violations,
        "steps": steps,
        "provenance": {
            "repository": "Tesis-Agentes-Comerciales/tesis-payment-provider-sandbox",
            "checked_out_sha": actual_sha,
            "pr_head_sha": os.environ.get("CI_PR_HEAD_SHA") or None,
            "actions_sha": actions_sha,
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "workflow_ref": os.environ.get("GITHUB_WORKFLOW_REF"),
            "runtime_python": runtime,
            "uv": command("uv", "--version"),
            "platform": platform.system(),
            "input_sha256": {
                p.relative_to(root).as_posix(): digest(p) for p in sorted(source_paths)
            },
        },
        "pytest": outcomes,
        "distribution_sha256": {
            p.relative_to(root).as_posix(): digest(p)
            for p in sorted((root / "dist").glob("*"))
            if p.is_file()
        },
    }
    (directory / "report.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    files = sorted(p for p in directory.iterdir() if p.is_file() and p.name != "SHA256SUMS")
    (directory / "SHA256SUMS").write_text(
        "".join(f"{digest(p)}  {p.name}\n" for p in files), encoding="utf-8"
    )
    summary = f"### PSP {kind}: {evidence['status']}\n\n"
    summary += f"SHA: `{actual_sha}`\n\n"
    if outcomes:
        summary += f"Collected: {outcomes.get('collected')}; outcomes: {outcomes.get('counts')}\n\n"
    summary += "\n".join(f"- {reason}" for reason in violations) + "\n"
    print(summary)
    if destination := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(destination).open("a", encoding="utf-8") as output:
            output.write(summary)
    return int(bool(violations))


if __name__ == "__main__":
    raise SystemExit(main())
