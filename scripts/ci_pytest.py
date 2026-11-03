"""Record individual phases and reject incomplete/omitted required tests. No test selection."""

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

STATUSES = ("passed", "failed", "error", "skipped", "xfail", "xpass", "not_executed")
PRIORITY = {
    "passed": 0,
    "not_executed": 1,
    "skipped": 2,
    "xfail": 3,
    "xpass": 4,
    "failed": 5,
    "error": 6,
}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--ci-outcomes", help="Write individual CI outcomes and enforce full execution"
    )


def pytest_configure(config: pytest.Config) -> None:
    if destination := config.getoption("--ci-outcomes"):
        config.pluginmanager.register(Recorder(Path(destination)), "psp-ci-outcomes")


class Recorder:
    def __init__(self, destination: Path) -> None:
        self.destination = destination
        self.collected: list[str] = []
        self.phases: dict[str, list[dict[str, str]]] = {}
        self.collection_errors: list[str] = []
        self.deselected = 0

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.collected = [item.nodeid for item in session.items]

    def pytest_deselected(self, items: list[pytest.Item]) -> None:
        self.deselected += len(items)

    @pytest.hookimpl(tryfirst=True)
    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            self.collection_errors.append(report.nodeid)
            report.longrepr = "Collection error; values omitted from CI output."
        report.sections = []

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        strict_xpass = report.failed and str(report.longrepr).startswith("[XPASS(strict)]")
        if strict_xpass or (report.passed and hasattr(report, "wasxfail")):
            status = "xpass"
        elif report.failed:
            status = "failed" if report.when == "call" else "error"
        elif report.skipped:
            status = "xfail" if hasattr(report, "wasxfail") else "skipped"
        else:
            status = "passed"
        self.phases.setdefault(report.nodeid, []).append({"phase": report.when, "status": status})
        # JUnit/terminal consume the same report after this hook; do not publish token values.
        if report.failed:
            report.longrepr = f"{status} during {report.when}; values omitted from CI output."
        elif report.skipped:
            report.longrepr = (
                report.location[0],
                report.location[1] or 0,
                "Skipped: values omitted",
            )
        if hasattr(report, "wasxfail"):
            report.wasxfail = "expected-failure metadata; values omitted"
        report.sections = []

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        tests: list[dict[str, Any]] = []
        for nodeid in self.collected:
            phases = self.phases.get(nodeid, [])
            status = max(
                (p["status"] for p in phases), key=PRIORITY.__getitem__, default="not_executed"
            )
            if status == "passed" and {p["phase"] for p in phases} != {"setup", "call", "teardown"}:
                status = "not_executed"
            tests.append({"nodeid": nodeid, "status": status, "phases": phases})
        counts = dict.fromkeys(STATUSES, 0) | dict(Counter(t["status"] for t in tests))
        complete = (
            exitstatus == 0
            and bool(tests)
            and counts["passed"] == len(tests)
            and not self.collection_errors
            and not self.deselected
            and len(set(self.collected)) == len(self.collected)
            and set(self.phases) == set(self.collected)
        )
        if not complete and exitstatus == 0:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
        payload = {
            "schema_version": 1,
            "status": "passed" if complete else "failed",
            "pytest_exit_code": int(exitstatus),
            "gate_exit_code": int(session.exitstatus),
            "collected": len(tests),
            "deselected": self.deselected,
            "collection_errors": self.collection_errors,
            "counts": counts,
            "tests": tests,
        }
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        self.destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
