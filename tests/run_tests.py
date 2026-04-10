#!/usr/bin/env python
"""
AI-friendly test runner — produces structured JSON output.

Usage:
    python tests/run_tests.py             # JSON to stdout + tests/test_results.json
    python tests/run_tests.py --pretty    # pretty-printed JSON
    pytest tests/ -v --tb=long            # standard pytest flow (also works)

Output schema:
{
    "summary": {"total": N, "passed": N, "failed": N, "errors": N, "skipped": N},
    "tests": [
        {
            "name": "test_single_leg_lmt_order",
            "module": "test_order_placement",
            "status": "PASSED" | "FAILED" | "ERROR" | "SKIPPED",
            "duration_ms": 12.3,
            "error": "...",       // only if FAILED/ERROR
            "traceback": "..."    // only if FAILED/ERROR
        }
    ]
}
"""

import json
import os
import sys
import time

# Ensure project root is on sys.path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest


class JSONResultCollector:
    """Pytest plugin that collects results into a JSON-friendly structure."""

    def __init__(self):
        self.results = []
        self.summary = {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}

    def pytest_runtest_logreport(self, report):
        if report.when != "call" and not (report.when == "setup" and report.failed):
            return

        self.summary["total"] += 1

        entry = {
            "name": report.nodeid.split("::")[-1],
            "module": report.nodeid.split("::")[0].replace("tests/", "").replace("tests\\", "").replace(".py", ""),
            "duration_ms": round(report.duration * 1000, 1),
        }

        if report.passed:
            entry["status"] = "PASSED"
            self.summary["passed"] += 1
        elif report.failed:
            if report.when == "setup":
                entry["status"] = "ERROR"
                self.summary["errors"] += 1
            else:
                entry["status"] = "FAILED"
                self.summary["failed"] += 1
            entry["error"] = str(report.longrepr).split("\n")[-1] if report.longrepr else ""
            entry["traceback"] = str(report.longrepr) if report.longrepr else ""
        elif report.skipped:
            entry["status"] = "SKIPPED"
            self.summary["skipped"] += 1

        self.results.append(entry)

    def get_report(self) -> dict:
        return {
            "summary": self.summary,
            "tests": self.results,
        }


def main():
    pretty = "--pretty" in sys.argv

    collector = JSONResultCollector()

    # Run pytest with our collector plugin
    tests_dir = os.path.dirname(os.path.abspath(__file__))
    exit_code = pytest.main(
        [tests_dir, "-v", "--tb=short", "-q"],
        plugins=[collector],
    )

    report = collector.get_report()

    # Write to file
    results_path = os.path.join(tests_dir, "test_results.json")
    indent = 2 if pretty else None
    with open(results_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    # Print to stdout
    print(json.dumps(report, indent=(2 if pretty else None), default=str))

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
