#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按需抓取公告正文：从库里挑出还没有正文的公告，下载 PDF 并解析入库。

与 fetch_notices.py 的分工：
    fetch_notices.py  按日期区间抓「列表」（元数据）
    fetch_body.py     针对已有元数据，补抓「正文」（PDF → 纯文本）

用法：
    python src/fetch_body.py --code 300750              # 某公司全部未抓正文的公告
    python src/fetch_body.py --high-value --limit 100   # 高价值类型里抓 100 条
    python src/fetch_body.py --top-companies 10         # 公告量最大的 10 家公司
    python src/fetch_body.py --stats                     # 只看现状，不下载
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import CNINFO_STATIC_BASE, DB_PATH  # noqa: E402
from fetch_notices import build_session, parse_pdf  # noqa: E402


def candidates(code=None, high_value=False, limit=None, top_companies=0) -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    sql = ("SELECT * FROM notices WHERE adjunct_url != '' "
           "AND (content_text IS NULL OR content_text = '')")
    params: list = []

    if code:
        sql += " AND sec_code = ?"
        params.append(code)
    if high_value:
        sql += " AND is_high_value = 1"
    if top_companies:
        top = [r[0] for r in conn.execute(
            "SELECT sec_code FROM notices WHERE adjunct_url != '' "
            "GROUP BY sec_code ORDER BY COUNT(*) DESC LIMIT ?", (top_companies,))]
        if top:
            sql += f" AND sec_code IN ({','.join('?' * len(top))})"
            params += top
    # 优先抓体积小的（扫描件少、解析成功率高、下载快）
    sql += " ORDER BY adjunct_size ASC, notice_date DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))

    rows = [dict(r) for r in conn.execute(sql, params)]
    conn.close()
    return rows


def show_stats() -> None:
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0]
    with_text = conn.execute(
        "SELECT COUNT(*) FROM notices WHERE content_text IS NOT NULL "
        "AND content_text != ''").fetchone()[0]
    rng = conn.execute(
        "SELECT MIN(notice_date), MAX(notice_date) FROM notices").fetchone()
    top = conn.execute(
        "SELECT sec_code, sec_name, COUNT(*) n, "
        "SUM(CASE WHEN is_high_value=1 THEN 1 ELSE 0 END) hv "
        "FROM notices GROUP BY sec_code, sec_name "
        "ORDER BY hv DESC, n DESC LIMIT 12").fetchall()
    conn.close()

    print(f"公告总数：{total}    已有正文：{with_text}    覆盖率："
          f"{with_text / total * 100 if total else 0:.1f}%")
    print(f"日期范围：{rng[0]} ~ {rng[1]}")
    print(f"\n{'代码':<8}{'简称':<12}{'公告数':>6}{'高价值':>7}")
    for code, name, n, hv in top:
        print(f"{code:<8}{name:<12}{n:>6}{hv:>7}")


def main() -> int:
    ap = argparse.ArgumentParser(description="按需抓取公告正文（PDF → 文本）")
    ap.add_argument("--code", help="只抓指定股票代码")
    ap.add_argument("--high-value", action="store_true", help="只抓高价值类型")
    ap.add_argument("--top-companies", type=int, default=0,
                    help="抓公告量最大的 N 家公司")
    ap.add_argument("--limit", type=int, help="最多抓多少条")
    ap.add_argument("--max-mb", type=float, default=15.0,
                    help="跳过大于该体积(MB)的 PDF，默认 15（多半是扫描件）")
    ap.add_argument("--stats", action="store_true", help="只查看现状")
    args = ap.parse_args()

    if args.stats:
        show_stats()
        return 0

    rows = candidates(args.code, args.high_value, args.limit, args.top_companies)
    if not rows:
        print("没有需要补正文的公告。")
        show_stats()
        return 0

    print(f"待抓正文 {len(rows)} 条，开始……（跳过 >{args.max_mb}MB 的 PDF）")
    session = build_session()
    conn = sqlite3.connect(DB_PATH)
    ok = skip = fail = 0
    try:
        for i, row in enumerate(rows, 1):
            aid, title = row["announcement_id"], row["title"][:34]
            size_kb = row.get("adjunct_size") or 0
            if size_kb / 1024 > args.max_mb:
                skip += 1
                print(f"[{i}/{len(rows)}] 跳过 {size_kb/1024:.1f}MB  {title}")
                continue
            try:
                resp = session.get(CNINFO_STATIC_BASE + row["adjunct_url"], timeout=45)
                resp.raise_for_status()
                text = parse_pdf(resp.content)
                if text:
                    conn.execute(
                        "UPDATE notices SET content_text = ? WHERE announcement_id = ?",
                        (text, aid))
                    conn.commit()
                    ok += 1
                    print(f"[{i}/{len(rows)}] OK {len(text):>6}字  {title}")
                else:
                    fail += 1
                    print(f"[{i}/{len(rows)}] 空（扫描件？）  {title}")
            except Exception as exc:  # noqa: BLE001
                fail += 1
                print(f"[{i}/{len(rows)}] 失败 {type(exc).__name__}  {title}")
            time.sleep(0.3)
    finally:
        conn.close()

    print(f"\n=== 完成：成功 {ok}，跳过 {skip}，失败 {fail} ===")
    show_stats()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
