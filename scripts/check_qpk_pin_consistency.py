#!/usr/bin/env python3
"""Check QuantStrategyLab direct git dependency pins.

Guards two failure modes:
- QuantPlatformKit refs must match the canonical QPK_PIN on main.
- Any QuantStrategyLab git dependency appearing in multiple dependency files
  must use one consistent ref across requirements/constraints/pyproject.

Usage: python scripts/check_qpk_pin_consistency.py [--fix]
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

QPK_PIN_URL = "https://raw.githubusercontent.com/QuantStrategyLab/QuantPlatformKit/main/QPK_PIN"
QPK_REF_RE = re.compile(r"QuantPlatformKit\.git@([a-f0-9]{40})")
QSL_REF_RE = re.compile(r"github\.com/QuantStrategyLab/([A-Za-z0-9_.-]+)\.git@([A-Za-z0-9._/-]+)")
PINNED_FILE_GLOBS = ("**/requirements*.txt", "**/constraints*.txt", "**/pyproject.toml")


def _extract_pin(raw: str) -> str:
    sha = raw.strip().split()[0]
    if len(sha) == 40:
        return sha
    raise RuntimeError(f"Invalid QPK_PIN: {sha}")


def fetch_pin() -> str:
    configured = os.getenv("QPK_EXPECTED_PIN")
    if configured:
        return _extract_pin(configured)

    errors: list[str] = []
    try:
        with urllib.request.urlopen(QPK_PIN_URL, timeout=10) as response:
            return _extract_pin(response.read().decode())
    except Exception as exc:  # pragma: no cover - fallback path depends on host certs
        errors.append(f"urllib: {exc}")

    try:
        output = subprocess.check_output(["curl", "-fsSL", QPK_PIN_URL], text=True, timeout=10)
        return _extract_pin(output)
    except Exception as exc:
        errors.append(f"curl: {exc}")

    raise RuntimeError("Unable to fetch QPK_PIN: " + "; ".join(errors))


def iter_pinned_files() -> list[Path]:
    paths: dict[str, Path] = {}
    for pattern in PINNED_FILE_GLOBS:
        for path in Path.cwd().glob(pattern):
            if "external" in path.parts:
                continue
            paths[str(path)] = path
    return [paths[name] for name in sorted(paths)]


def main() -> int:
    fix = "--fix" in sys.argv
    target = fetch_pin()
    print(f"QPK_PIN: {target[:12]}...")
    errors = 0
    qsl_refs: dict[str, list[tuple[Path, int, str]]] = {}

    for path in iter_pinned_files():
        content = path.read_text(encoding="utf-8")
        updated = content
        for line_no, line in enumerate(content.splitlines(), start=1):
            for repo, ref in QSL_REF_RE.findall(line):
                qsl_refs.setdefault(repo, []).append((path, line_no, ref))
        for match in QPK_REF_RE.finditer(content):
            sha = match.group(1)
            if sha != target:
                errors += 1
                print(f"  ❌ {path}: QPK@{sha[:12]} (expected {target[:12]})")
                if fix:
                    updated = updated.replace(f"QuantPlatformKit.git@{sha}", f"QuantPlatformKit.git@{target}")
                    print("     → fixed")
        if fix and updated != content:
            path.write_text(updated, encoding="utf-8")

    for repo, matches in sorted(qsl_refs.items()):
        refs = sorted({ref for _, _, ref in matches})
        if len(refs) <= 1:
            continue
        errors += 1
        print(f"  ❌ inconsistent QuantStrategyLab dependency pin for {repo}:")
        for path, line_no, ref in matches:
            print(f"     {path}:{line_no}: {ref}")

    if errors:
        print(f"\n{errors} dependency pin mismatch(es). Run with --fix to auto-fix QPK refs only.")
        return 1
    print("✅ QuantStrategyLab dependency pins are consistent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
