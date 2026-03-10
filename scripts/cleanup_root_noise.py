"""清理根目录噪音文件并迁移到 `tmp/`。"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORPHAN_DIR = ROOT / "tmp" / "root_orphans" / "blat_markers"
TEST_DIR = ROOT / "tmp" / "test_artifacts"


def move_if_possible(src: Path, dst: Path) -> bool:
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return True
    except Exception:
        return False


def collect_blat_markers() -> int:
    moved = 0
    for path in ROOT.iterdir():
        if not path.is_file():
            continue
        if path.stat().st_size != 4:
            continue
        try:
            if path.read_bytes() != b"blat":
                continue
        except Exception:
            continue
        if move_if_possible(path, ORPHAN_DIR / path.name):
            moved += 1
    return moved


def collect_test_artifacts() -> int:
    moved = 0
    for name in ["tmp_factor_board_test.db", "tmp_factor_board_test.db-journal"]:
        path = ROOT / name
        if path.exists() and move_if_possible(path, TEST_DIR / path.name):
            moved += 1
    return moved


def main() -> None:
    moved_blat = collect_blat_markers()
    moved_tests = collect_test_artifacts()
    print(f"moved_blat={moved_blat}")
    print(f"moved_test_artifacts={moved_tests}")


if __name__ == "__main__":
    main()
