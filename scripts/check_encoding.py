from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECK_FILES = [
    ROOT / "static" / "index.html",
    ROOT / "README.md",
]

MOJIBAKE_TOKENS = [
    "\ufffd",
    "锟",
    "鍥",
    "鎸",
    "鏆",
    "閲",
    "绯荤粺",
    "璁剧疆",
    "鍥犲瓙",
    "鎸栨帢",
]


def main() -> int:
    failed = False
    for path in CHECK_FILES:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        hits = [(token, text.count(token)) for token in MOJIBAKE_TOKENS if text.count(token)]
        if hits:
            failed = True
            rel = path.relative_to(ROOT)
            summary = ", ".join(f"{token!r}={count}" for token, count in hits)
            print(f"[FAIL] {rel}: detected possible mojibake: {summary}")
        else:
            print(f"[OK] {path.relative_to(ROOT)}: UTF-8 Chinese text looks clean")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
