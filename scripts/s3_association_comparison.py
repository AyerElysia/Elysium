#!/usr/bin/env python3
"""Run the bounded synthetic S3 protocol matrix and print its measured results.

Run only in the repository's approved test window. This invokes 30 temporary
SQLite/real-reader tests, never a model or a formal consciousness instance.
It does not read production state, reset live histories, or infer recall gain.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST = "test/plugins/life_engine/test_s3_association_controls.py"
TEST_TIMEOUT_SECONDS = 180


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        help="Optional JSON artifact path; stdout always contains the report.",
    )
    args = parser.parse_args()
    environment = dict(os.environ)
    environment.pop("PYTEST_ADDOPTS", None)
    with tempfile.TemporaryDirectory(prefix="s3-protocol-") as folder:
        junit = Path(folder) / "results.xml"
        timed_out = False
        try:
            run = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "--no-cov",
                    "-n",
                    "0",
                    "-o",
                    "addopts=",
                    "-o",
                    "junit_family=xunit1",
                    TEST,
                    f"--junitxml={junit}",
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=TEST_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            run = subprocess.CompletedProcess(
                args=exc.cmd,
                returncode=124,
                stdout=stdout.decode("utf-8", errors="replace")
                if isinstance(stdout, bytes)
                else stdout,
                stderr=stderr.decode("utf-8", errors="replace")
                if isinstance(stderr, bytes)
                else stderr,
            )
        rows = []
        failures = []
        testcases = []
        if junit.is_file():
            try:
                testcases = list(ET.parse(junit).iter("testcase"))
            except ET.ParseError:
                failures.append("JUnitParseError")
        if testcases:
            for testcase in testcases:
                for prop in testcase.findall("./properties/property"):
                    if prop.get("name") == "s3_protocol_result":
                        rows.append(json.loads(prop.attrib["value"]))
                if (
                    testcase.find("failure") is not None
                    or testcase.find("error") is not None
                ):
                    failures.append(testcase.attrib.get("name", "unknown"))
        by_case: dict[str, set[str]] = {}
        for row in rows:
            by_case.setdefault(row["case"], set()).add(row["material_sha256"])
        equal_material = len(by_case) == 3 and all(
            len(values) == 1 for values in by_case.values()
        )
        success = (
            run.returncode == 0 and not failures and len(rows) == 30 and equal_material
        )
        report = {
            "schema": "s3-synthetic-protocol-comparison-v1",
            "passed": success,
            "evidence_class": "synthetic_protocol_only",
            "subject_recall_benefit": None,
            "model": None,
            "model_calls": 0,
            "tokens": None,
            "expected_runs": 30,
            "measured_runs": len(rows),
            "same_material_across_arms_and_repetitions": equal_material,
            "test_exit_code": run.returncode,
            "serial_execution": True,
            "test_timeout_seconds": TEST_TIMEOUT_SECONDS,
            "timed_out": timed_out,
            "failed_tests": failures,
            "limits": [
                "Reserved synthetic fixtures, not a blind or independent subject evaluation.",
                "Two recorded seeds repeat a deterministic controller; not statistical model replicates.",
                "Source confusion counts exact identity/hash mismatches, not model attribution errors.",
                "Distractor exposure is not an error and accessibility is not truth or importance.",
                "Boundary comparison uses the same preselected scope and measures transport only.",
                "No automatic default change follows from these results.",
            ],
            "runs": rows,
        }
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.report is not None:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(rendered, encoding="utf-8")
        sys.stdout.write(rendered)
        if not success:
            sys.stderr.write(run.stdout[-12000:] + run.stderr[-4000:])
        return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
