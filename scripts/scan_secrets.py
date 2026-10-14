#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import re
import subprocess
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAFE_FILES = {Path(".env.example")}
FORBIDDEN_NAMES = {".env", "secrets.env", "tunnel-token", "tunnel-token.txt"}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".csv",
    ".iif",
    ".mt940",
    ".numbers",
    ".ofx",
    ".qfx",
    ".qif",
    ".pem",
    ".p12",
    ".nsbak",
    ".token",
    ".tsv",
    ".txf",
    ".xls",
    ".xlsx",
}
SENSITIVE_EXPORT_SUFFIXES = {".json", ".pdf", ".txt", ".zip"}
SENSITIVE_EXPORT_NAME = re.compile(
    r"(?i)(?:^|[-_. ])(?:account[-_. ]*)?(?:statement|positions?|transactions?|holdings?|"
    r"tax[-_. ]*lots?|balances?|account[-_. ]*activity|realized[-_. ]*gains?)(?:$|[-_. ])"
)
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
TOKEN_PATTERN = re.compile(r"(?:gh[opsu]_[A-Za-z0-9]{30,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,})")
ASSIGNMENT = re.compile(
    r"(?i)(?:client_secret|access_token|refresh_token|api_key|tunnel_token|session_key)"
    r"\s*[:=]\s*['\"]?([A-Za-z0-9_./+=-]{20,})"
)
PLACEHOLDERS = ("replace", "example", "placeholder", "dummy", "fake", "test-only", "not-for-production")


def git_paths(mode: str) -> list[Path]:
    if mode == "staged":
        command = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"]
    else:
        command = ["git", "ls-files", "-z"]
    output = subprocess.check_output(command, cwd=ROOT)  # noqa: S603
    return [Path(part.decode()) for part in output.split(b"\0") if part]


def all_paths() -> list[Path]:
    ignored = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
    return [
        path.relative_to(ROOT)
        for path in ROOT.rglob("*")
        if path.is_file() and not ignored.intersection(path.parts)
    ]


def history_items() -> list[tuple[Path, bytes]]:
    objects = subprocess.check_output(  # noqa: S603
        ["git", "rev-list", "--objects", "--all"],  # noqa: S607
        cwd=ROOT,
    )
    items: list[tuple[Path, bytes]] = []
    seen: set[tuple[str, str]] = set()
    for line in objects.decode("utf-8", errors="surrogateescape").splitlines():
        object_id, separator, raw_path = line.partition(" ")
        if not separator or not raw_path or (object_id, raw_path) in seen:
            continue
        object_type = subprocess.check_output(  # noqa: S603
            ["git", "cat-file", "-t", object_id],  # noqa: S607
            cwd=ROOT,
        )
        if object_type.strip() != b"blob":
            continue
        seen.add((object_id, raw_path))
        payload = subprocess.check_output(  # noqa: S603
            ["git", "cat-file", "blob", object_id],  # noqa: S607
            cwd=ROOT,
        )
        items.append((Path(raw_path), payload))
    return items


def entropy(value: str) -> float:
    counts = Counter(value)
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def scan_bytes(path: Path, raw: bytes) -> list[str]:
    findings: list[str] = []
    if path in SAFE_FILES:
        return findings
    suffix = path.suffix.lower()
    sensitive_export_name = suffix in SENSITIVE_EXPORT_SUFFIXES and SENSITIVE_EXPORT_NAME.search(
        path.stem
    )
    if (
        path.name in FORBIDDEN_NAMES
        or suffix in FORBIDDEN_SUFFIXES
        or sensitive_export_name
    ):
        findings.append("forbidden sensitive filename")
        return findings
    try:
        if b"\0" in raw[:4096]:
            return findings
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return findings

    if PRIVATE_KEY.search(text):
        findings.append("private key material")
    if TOKEN_PATTERN.search(text):
        findings.append("token-like credential")
    for match in ASSIGNMENT.finditer(text):
        value = match.group(1)
        lowered = value.lower()
        if not any(marker in lowered for marker in PLACEHOLDERS) and entropy(value) >= 3.5:
            findings.append("high-entropy value assigned to a sensitive field")
            break
    return findings


def scan(path: Path) -> list[str]:
    try:
        raw = (ROOT / path).read_bytes()
    except OSError:
        return []
    return scan_bytes(path, raw)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail when source contains credentials or financial artifacts."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--staged", action="store_true")
    group.add_argument("--tracked", action="store_true")
    group.add_argument("--all", action="store_true")
    group.add_argument("--history", action="store_true")
    args = parser.parse_args()

    if args.history:
        items = history_items()
        failures = [(path, scan_bytes(path, raw)) for path, raw in items]
        checked = len(items)
    else:
        mode = "staged" if args.staged else "tracked"
        paths = all_paths() if args.all else git_paths(mode)
        failures = [(path, scan(path)) for path in paths]
        checked = len(paths)
    failures = [(path, reasons) for path, reasons in failures if reasons]
    if failures:
        print("Secret scan blocked the operation:")
        for index, (_, reasons) in enumerate(failures, start=1):
            print(f"- source item {index}: {', '.join(reasons)}")
        return 1
    print(f"Secret scan passed ({checked} files checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
