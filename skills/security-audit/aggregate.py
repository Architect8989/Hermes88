#!/usr/bin/env python3
"""
Aggregate bandit + safety JSON outputs into a unified Rhodawk audit report.
Usage: python3 aggregate.py --bandit <file> --safety <file> --output <file>
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone


SEVERITY_MAP = {
    "HIGH":   "high",
    "MEDIUM": "medium",
    "LOW":    "low",
    "ERROR":  "critical",
    "WARNING":"medium",
    "INFO":   "low",
}


def load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def parse_bandit(data: dict) -> list[dict]:
    findings = []
    for r in data.get("results", []):
        findings.append({
            "tool":     "bandit",
            "severity": SEVERITY_MAP.get(r.get("issue_severity", "LOW").upper(), "low"),
            "title":    r.get("issue_text", "unknown"),
            "file":     r.get("filename", ""),
            "line":     r.get("line_number", 0),
            "cwe":      r.get("issue_cwe", {}).get("id", ""),
            "detail":   r.get("more_info", ""),
        })
    return findings


def parse_safety(data: dict) -> list[dict]:
    findings = []
    vulns = data if isinstance(data, list) else data.get("vulnerabilities", [])
    for v in vulns:
        findings.append({
            "tool":     "safety",
            "severity": v.get("severity", "unknown"),
            "title":    f"{v.get('package_name', 'unknown')} {v.get('vulnerable_spec', '')}",
            "file":     "requirements",
            "line":     0,
            "cwe":      "",
            "detail":   v.get("advisory", v.get("more_info_url", "")),
        })
    return findings


def bucket_counts(findings: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for f in findings:
        sev = f.get("severity", "low")
        if sev in counts:
            counts[sev] += 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bandit",  required=True)
    parser.add_argument("--safety",  required=True)
    parser.add_argument("--output",  required=True)
    parser.add_argument("--repo",    default="unknown")
    args = parser.parse_args()

    bandit_data = load_json(args.bandit)
    safety_data = load_json(args.safety)

    findings = parse_bandit(bandit_data) + parse_safety(safety_data)
    counts   = bucket_counts(findings)

    report = {
        "repo":           args.repo,
        "timestamp":      datetime.now(timezone.utc).isoformat(),
        "total_findings": len(findings),
        "critical":       counts["critical"],
        "high":           counts["high"],
        "medium":         counts["medium"],
        "low":            counts["low"],
        "findings":       findings,
    }

    parent = os.path.dirname(args.output)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(report, f, indent=2)

    print(
        f"Audit report written: {args.output} | "
        f"total={len(findings)} critical={counts['critical']} "
        f"high={counts['high']} medium={counts['medium']} low={counts['low']}"
    )


if __name__ == "__main__":
    main()
