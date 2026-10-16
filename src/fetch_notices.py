#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
D1 · A 股公告抓取与入库

数据源：巨潮资讯网 http://www.cninfo.com.cn
        深交所指定的法定信息披露平台 —— 公开、免费、无需登录。

它做三件事：
  1. 按日期区间分页拉取公告列表（沪市 + 深市，覆盖全 A）
  2. 按标题规则给每条公告打上「高价值 / 噪音」标签
  3. 把结果写入 SQLite；可选把公告正文 PDF 下载并解析成纯文本一并存入

用法：
    python src/fetch_notices.py --days 3                     # 抓最近 3 天列表
    python src/fetch_notices.py --days 3 --with-pdf --pdf-limit 50
    python src/fetch_notices.py --start 2026-10-13 --end 2026-10-15 --with-pdf
    python src/fetch_notices.py --days 3 --high-value-only   # 只入库高价值类型
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (  # noqa: E402
    CNINFO_HOME,
    CNINFO_QUERY_URL,
    CNINFO_STATIC_BASE,
    DB_PATH,
    HEADERS,
    MAX_CONTENT_CHARS,
    MAX_RETRIES,
    PAGE_SIZE,
    PLATES,
    REQUEST_INTERVAL,
    classify_title,
)

DDL = """
CREATE TABLE IF NOT EXISTS notices (
    announcement_id   TEXT PRIMARY KEY,   -- 巨潮公告唯一 ID
    sec_code          TEXT,               -- 证券代码
    sec_name          TEXT,               -- 证券简称
    title             TEXT,               -- 公告标题
    notice_ts         INTEGER,            -- 披露时间（毫秒时间戳）
    notice_date       TEXT,               -- 披露日期 YYYY-MM-DD
    plate             TEXT,               -- sh / sz（来源板块）
    page_column       TEXT,               -- 巨潮板块编码，如 SZCY / SSE
    announcement_type TEXT,               -- 巨潮原始分类编码
    category          TEXT,               -- 标题规则归类
    is_high_value     INTEGER DEFAULT 0,  -- 1=高价值定向子集
    adjunct_url       TEXT,               -- 正文 PDF 相对路径
    adjunct_size      INTEGER,            -- PDF 大小 KB
    content_text      TEXT,               -- 解析出的正文纯文本
    fetched_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_notice_date  ON notices(notice_date);
CREATE INDEX IF NOT EXISTS idx_sec_code     ON notices(sec_code);
CREATE INDEX IF NOT EXISTS idx_high_value   ON notices(is_high_value);
CREATE INDEX IF NOT EXISTS idx_category     ON notices(category);
"""


def build_session() -> requests.Session:
    """建立会话。

    两个关键点：
      * trust_env=False —— 本机环境变量里配了 127.0.0.1 代理，
        对国内站点走代理会 502 / 超时。这里强制直连。
      * 先 GET 一次页面 —— 拿 JSESSIONID cookie，接口才稳定返回数据。
    """
    s = requests.Session()
    s.trust_env = False
    s.headers.update(HEADERS)
    try:
        s.get(CNINFO_HOME, timeout=15)
    except requests.RequestException as exc:
        print(f"[警告] 取 cookie 失败，继续尝试：{exc}")
    return s


def fetch_page(session: requests.Session, plate: str, page_num: int,
               start: str, end: str) -> dict | None:
    """拉取一页公告。失败自动重试，最终失败返回 None。"""
    payload = {
        "pageNum": page_num,
        "pageSize": PAGE_SIZE,
        "column": "szse",          # 实测该参数无效，但服务端要求存在
        "plate": plate,            # 真正决定市场范围的是它
        "tabName": "fulltext",
        "seDate": f"{start}~{end}",
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.post(CNINFO_QUERY_URL, data=payload, timeout=25)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            print(f"    [重试 {attempt}/{MAX_RETRIES}] {type(exc).__name__}: {exc}")
            time.sleep(1.5 * attempt)
    return None


def normalize(item: dict, plate: str) -> dict:
    """把巨潮返回的一条记录整理成入库用的字典。"""
    ts = item.get("announcementTime") or 0
    try:
        dt = datetime.fromtimestamp(ts / 1000)
        notice_date = dt.strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        notice_date = ""

    title = (item.get("announcementTitle") or "").strip()
    category, is_high = classify_title(title)

    return {
        "announcement_id": str(item.get("announcementId") or ""),
        "sec_code": item.get("secCode") or "",
        "sec_name": item.get("secName") or "",
        "title": title,
        "notice_ts": ts,
        "notice_date": notice_date,
        "plate": plate,
        "page_column": item.get("pageColumn") or "",
        "announcement_type": item.get("announcementType") or "",
        "category": category,
        "is_high_value": is_high,
        "adjunct_url": item.get("adjunctUrl") or "",
        "adjunct_size": item.get("adjunctSize") or 0,
        "content_text": None,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def fetch_plate(session: requests.Session, plate: str, start: str, end: str) -> list:
    """抓完某一个板块在日期区间内的全部公告。"""
    rows, page = [], 1
    while True:
        data = fetch_page(session, plate, page, start, end)
        if not data:
            print(f"  [{plate}] 第 {page} 页拉取失败，停止该板块")
            break

        items = data.get("announcements") or []
        if not items:
            break

        rows.extend(normalize(it, plate) for it in items)
        total = data.get("totalRecordNum") or data.get("totalAnnouncement") or 0
        print(f"  [{plate}] 第 {page} 页 +{len(items)} 条（累计 {len(rows)}/{total}）")

        if page * PAGE_SIZE >= total:
            break
        page += 1
        time.sleep(REQUEST_INTERVAL)
    return rows


def parse_pdf(content: bytes) -> str:
    """把 PDF 字节流解析成纯文本。pymupdf 解析失败时返回空串（不中断流程）。"""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print("[错误] 未安装 pymupdf，请先 pip install pymupdf")
        return ""

    try:
        with fitz.open(stream=content, filetype="pdf") as doc:
            text = "\n".join(page.get_text() for page in doc)
    except Exception as exc:  # noqa: BLE001
        print(f"    [PDF解析失败] {type(exc).__name__}: {exc}")
        return ""

    text = text.strip()
    return text[:MAX_CONTENT_CHARS]


def attach_pdf(session: requests.Session, rows: list, limit: int | None,
               high_value_only: bool) -> None:
    """给已入库的公告补抓正文。就地修改 rows。"""
    targets = [r for r in rows if r["adjunct_url"]]
    if high_value_only:
        targets = [r for r in targets if r["is_high_value"]]
    if limit:
        targets = targets[:limit]
    if not targets:
        return

    print(f"\n开始下载并解析正文 PDF，共 {len(targets)} 条……")
    ok = 0
    for i, row in enumerate(targets, 1):
        url = CNINFO_STATIC_BASE + row["adjunct_url"]
        try:
            resp = session.get(url, timeout=40)
            resp.raise_for_status()
            text = parse_pdf(resp.content)
            if text:
                row["content_text"] = text
                ok += 1
                print(f"  [{i}/{len(targets)}] {row['sec_name']} {row['title'][:30]} "
                      f"-> {len(text)} 字")
            else:
                print(f"  [{i}/{len(targets)}] {row['title'][:30]} -> 解析为空")
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i}/{len(targets)}] 下载失败 {type(exc).__name__}: {exc}")
        time.sleep(0.3)

    print(f"正文抓取完成：成功 {ok}/{len(targets)} 条")


def save(conn: sqlite3.Connection, rows: list) -> int:
    """写入 SQLite（按 announcement_id 去重覆盖）。"""
    if not rows:
        return 0
    cols = list(rows[0].keys())
    sql = (
        f"INSERT OR REPLACE INTO notices ({','.join(cols)}) "
        f"VALUES ({','.join('?' * len(cols))})"
    )
    conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
    conn.commit()
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="抓取 A 股公开公告并入库（数据源：巨潮资讯网）")
    ap.add_argument("--days", type=int, default=3, help="抓最近 N 天（含今天）")
    ap.add_argument("--start", help="起始日期 YYYY-MM-DD，覆盖 --days")
    ap.add_argument("--end", help="结束日期 YYYY-MM-DD，覆盖 --days")
    ap.add_argument("--with-pdf", action="store_true", help="同时下载并解析正文 PDF")
    ap.add_argument("--pdf-limit", type=int, default=None, help="最多解析多少条正文")
    ap.add_argument("--high-value-only", action="store_true",
                    help="只入库高价值类型公告（默认全量入库）")
    args = ap.parse_args()

    end = args.end or date.today().strftime("%Y-%m-%d")
    start = args.start or (date.today() - timedelta(days=args.days - 1)).strftime("%Y-%m-%d")

    print(f"=== 公告雷达 D1 ===  区间：{start} ~ {end}  市场：{'/'.join(PLATES)}")

    session = build_session()
    all_rows = []
    for plate in PLATES:
        print(f"\n--- 抓取 {plate} ---")
        all_rows.extend(fetch_plate(session, plate, start, end))
        time.sleep(REQUEST_INTERVAL)

    # 按公告 ID 去重（同一公告可能被沪/深两个板块同时带出）
    dedup = {r["announcement_id"]: r for r in all_rows if r["announcement_id"]}
    rows = list(dedup.values())
    high = [r for r in rows if r["is_high_value"]]
    print(f"\n去重后共 {len(rows)} 条，其中高价值类型 {len(high)} 条")

    if args.high_value_only:
        rows = high

    if args.with_pdf:
        attach_pdf(session, rows, args.pdf_limit, high_value_only=False)

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(DDL)
        n = save(conn, rows)
        db_total = conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0]
        db_high = conn.execute(
            "SELECT COUNT(*) FROM notices WHERE is_high_value=1").fetchone()[0]
        db_text = conn.execute(
            "SELECT COUNT(*) FROM notices WHERE content_text IS NOT NULL").fetchone()[0]
    finally:
        conn.close()

    print("\n=== 完成 ===")
    print(f"本次写入        : {n} 条")
    print(f"数据库累计      : {db_total} 条（高价值 {db_high} 条）")
    print(f"已解析正文      : {db_text} 条")
    print(f"数据库位置      : {DB_PATH}")

    print("\n各类型分布：")
    conn = sqlite3.connect(DB_PATH)
    try:
        for cat, cnt, hv in conn.execute(
            "SELECT category, COUNT(*), is_high_value FROM notices "
            "GROUP BY category, is_high_value ORDER BY COUNT(*) DESC"
        ):
            flag = "★" if hv else " "
            print(f"  {flag} {cat:<14} {cnt:>5} 条")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
