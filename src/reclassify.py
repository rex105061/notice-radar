#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按最新分类规则，重新给库里所有公告打标签。

改完 src/config.py 的关键词表后跑这个——不用重新抓数据，
几秒钟就能把 6893 条全部重判一遍。

用法：
    python src/reclassify.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import DB_PATH, classify_title  # noqa: E402


def main() -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT announcement_id, title, category, is_high_value FROM notices"
        ).fetchall()

        changed = 0
        for aid, title, old_cat, old_hv in rows:
            cat, hv = classify_title(title or "")
            if cat != old_cat or hv != old_hv:
                conn.execute(
                    "UPDATE notices SET category = ?, is_high_value = ? "
                    "WHERE announcement_id = ?", (cat, hv, aid))
                changed += 1
        conn.commit()

        total = len(rows)
        high = conn.execute(
            "SELECT COUNT(*) FROM notices WHERE is_high_value = 1").fetchone()[0]
        print(f"扫描 {total} 条，标签变更 {changed} 条")
        print(f"高价值 {high} 条（占比 {high / total * 100 if total else 0:.1f}%）\n")

        print(f"{'':2}{'类别':<16}{'条数':>7}")
        for cat, hv, n in conn.execute(
                "SELECT category, is_high_value, COUNT(*) FROM notices "
                "GROUP BY category, is_high_value ORDER BY COUNT(*) DESC"):
            print(f"{'★' if hv else ' '} {cat:<16}{n:>7}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
