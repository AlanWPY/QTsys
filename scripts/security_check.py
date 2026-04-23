from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "runtime",
    "tmp",
    "test-results",
    "logs",
}

PATTERNS: dict[str, re.Pattern[str]] = {
    "openai_like_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "bearer_token": re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}", re.IGNORECASE),
    "hardcoded_secret_field": re.compile(
        r"(?i)\b(api[_-]?key|apikey|token|secret|password)\b\s*[:=]\s*['\"][^'\"]{8,}['\"]"
    ),
    "credential_url": re.compile(r"\b\w+://[^\s/:{}]+:[^\s@{}]+@[^\s{}]+"),
}


def should_skip(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)


def iter_text_files():
    for path in ROOT.rglob("*"):
        if path.is_dir() or should_skip(path):
            continue
        try:
            yield path, path.read_text(encoding="utf-8")
        except Exception:
            continue


def main() -> int:
    findings: list[tuple[str, int, str, str]] = []
    for path, text in iter_text_files():
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append((str(path.relative_to(ROOT)), lineno, name, line.strip()[:220]))
    if not findings:
        print("No obvious secrets found.")
        return 0

    print("Potential secrets found:")
    for path, lineno, name, line in findings:
        print(f"- {path}:{lineno} [{name}] {line}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
