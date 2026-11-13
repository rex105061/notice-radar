# -*- coding: utf-8 -*-
"""公告雷达 · 网页版（Streamlit）

启动：
    streamlit run src/app.py

设计取舍：
- 列表从 SQLite 读，秒开；AI 解读按需触发（点按钮），避免一进页面就烧 token
- 已分析过的结果直接读库，不重复调用模型
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyze import DDL_ANALYSIS, analyze_notice, save_analysis  # noqa: E402
from config import ALLOW_LIVE_ANALYSIS, CNINFO_STATIC_BASE, DB_PATH, LIVE_QUOTA  # noqa: E402

DISCLAIMER = (
    "**免责声明**：本页内容由 AI 生成，仅供参考，**不构成任何投资建议**。"
    "所有结论使用前须经人工复核。数据来源为巨潮资讯网公开披露信息。"
)

st.set_page_config(page_title="公告雷达", layout="wide")


# ------------------------------------------------------------------ 数据
@st.cache_data(ttl=30)
def query_notices(keyword: str, days: int, high_only: bool) -> list:
    sql = ("SELECT * FROM notices WHERE 1=1")
    params: list = []
    if keyword:
        sql += " AND (sec_code = ? OR sec_code LIKE ? OR sec_name LIKE ?)"
        params += [keyword, f"{keyword}%", f"%{keyword}%"]
    if days:
        since = (date.today() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        sql += " AND notice_date >= ?"
        params.append(since)
    if high_only:
        sql += " AND is_high_value = 1"
    sql += " ORDER BY notice_date DESC, is_high_value DESC, adjunct_size ASC LIMIT 60"
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def load_analysis(aid: str) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(DDL_ANALYSIS)
        row = conn.execute(
            "SELECT extracted_json, interpreted_json, score_detail, score_total "
            "FROM analysis WHERE announcement_id = ?", (aid,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "extracted": json.loads(row[0] or "{}"),
        "interpreted": json.loads(row[1] or "{}"),
        "score": json.loads(row[2] or "{}"),
        "total": row[3],
    }


@st.cache_resource
def _http_session():
    """复用一个 HTTP 会话（含 cookie），避免每次下载 PDF 都重新握手。"""
    from fetch_notices import build_session

    return build_session()


def ensure_content(row: dict) -> str:
    """正文缺失时，现场下载 PDF 并解析；成功后写回数据库。

    这是「按需抓取」的核心——库里 1 万条公告不可能全部预抓，
    但用户真正会看的就是那几条，缺哪条补哪条。
    """
    if row.get("content_text"):
        return row["content_text"]
    url = row.get("adjunct_url")
    if not url:
        return ""
    try:
        from fetch_notices import parse_pdf

        resp = _http_session().get(CNINFO_STATIC_BASE + url, timeout=40)
        resp.raise_for_status()
        text = parse_pdf(resp.content)
        if not text:
            return ""  # 扫描件或无文本层
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(
                "UPDATE notices SET content_text = ? WHERE announcement_id = ?",
                (text, row["announcement_id"]))
            conn.commit()
        finally:
            conn.close()
        row["content_text"] = text
        return text
    except Exception:  # noqa: BLE001
        return ""


@st.cache_data(ttl=300)
def db_stats() -> dict:
    conn = sqlite3.connect(DB_PATH)
    try:
        n = conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0]
        d = conn.execute(
            "SELECT MIN(notice_date), MAX(notice_date) FROM notices").fetchone()
        with_text = conn.execute(
            "SELECT COUNT(*) FROM notices WHERE content_text IS NOT NULL "
            "AND content_text != ''").fetchone()[0]
        analyzed = conn.execute("SELECT COUNT(*) FROM analysis").fetchone()[0]
    except sqlite3.Error:
        n, d, with_text, analyzed = 0, (None, None), 0, 0
    finally:
        conn.close()
    return {"total": n, "range": d, "with_text": with_text, "analyzed": analyzed}


# ------------------------------------------------------------------ 渲染
def render_priority(pri: dict) -> None:
    """优先级是这一页的行动入口——先告诉人「要不要现在处理」，再谈内容。"""
    level = str(pri.get("level", "P2")).upper()
    label = {"P0": "今日必处理", "P1": "三日内准备", "P2": "存档即可"}.get(level, "")
    lines = [f"**{level} · {label}**"]
    if pri.get("reason"):
        lines.append(f"依据：{pri['reason']}")
    if pri.get("action"):
        lines.append(f"建议：{pri['action']}")
    if pri.get("window"):
        lines.append(f"窗口：{pri['window']}")
    block = "  \n".join(lines)
    if level == "P0":
        st.error(block)
    elif level == "P1":
        st.warning(block)
    else:
        st.info(block)


def render_analysis(res: dict) -> None:
    it = res["interpreted"]
    ex = res["extracted"]

    if it.get("priority"):
        render_priority(it["priority"])

    st.markdown("**一句话解读**")
    st.info(it.get("one_line") or "（缺失）")

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**买方会问什么**")
        for i, q in enumerate(it.get("buy_side_questions") or [], 1):
            st.markdown(f"{i}. {q}")
    with c2:
        st.markdown("**卖方会问什么**")
        for i, q in enumerate(it.get("sell_side_questions") or [], 1):
            st.markdown(f"{i}. {q}")

    st.markdown("**建议对外口径**")
    st.success(it.get("suggested_statement") or "（缺失）")

    flags = it.get("compliance_flags") or []
    st.markdown("**建议口径的合规风险**")
    if flags:
        for f in flags:
            lvl = f.get("risk_level", "?")
            icon = {"高": "red", "中": "orange", "低": "blue"}.get(lvl, "gray")
            st.markdown(
                f":{icon}[**[{lvl}]**] 「{f.get('quote','')}」 — "
                f"{f.get('rule','')}：{f.get('reason','')}")
    else:
        st.markdown(":green[建议口径未命中合规红线]")

    caution = it.get("question_caution") or []
    if caution:
        with st.expander("哪些问题不能原样抛给公司"):
            for c in caution:
                st.markdown(f"- {c}")

    if it.get("compliance_note"):
        st.caption(it["compliance_note"])

    with st.expander("结构化抽取结果"):
        st.json(ex)


def main() -> None:
    stats = db_stats()
    st.title("公告雷达")
    st.caption("输入股票代码 → 看公告 → 读出「意味着什么、机构会问什么、口径怎么说」")
    if stats["range"][0]:
        st.caption(f"当前数据：{stats['range'][0]} 至 {stats['range'][1]}，"
                   f"共 {stats['total']} 条公告 · 已生成解读 {stats['analyzed']} 条")
    st.warning(DISCLAIMER)

    with st.sidebar:
        st.markdown("### 数据概况")
        st.metric("公告总数", stats["total"])
        st.metric("有正文", stats["with_text"])
        st.metric("已分析", stats["analyzed"])
        if stats["range"][0]:
            st.caption(f"覆盖日期：{stats['range'][0]} ~ {stats['range'][1]}")
        st.divider()
        st.markdown("### 合规红线（模型只做判定）")
        st.caption("预测性表述 / 承诺性表述 / 绝对化用语 / 比较性表述 / "
                   "投资建议 / 未披露信息暗示")

    c1, c2, c3 = st.columns([2, 1, 1])
    keyword = c1.text_input("股票代码或公司简称", placeholder="例如 300750 或 宁德时代")
    days = c2.selectbox("时间范围", [0, 3, 7, 14, 30, 90], index=0,
                        format_func=lambda d: "全部" if d == 0 else f"近 {d} 天")
    high_only = c3.checkbox("只看高价值类型", value=True)

    if not keyword:
        st.info("输入股票代码或公司简称后开始查询。")
        return

    rows = query_notices(keyword.strip(), days, high_only)
    if not rows:
        stat = db_stats()
        since, until = stat["range"]
        st.warning(
            f"没查到公告。当前数据库覆盖 **{since} 至 {until}**，共 {stat['total']} 条公告。\n\n"
            "可能的原因："
            "① 该公司在这段时间内确实没有公告；"
            "② 时间范围选得比数据还窄——先把时间切到「全部」；"
            "③ 勾了「只看高价值类型」，该公司的公告都被归为会议/制度类；"
            "④ 代码或名称不对。")
        return

    name = rows[0]["sec_name"]
    st.subheader(f"{name}（{rows[0]['sec_code']}）　共 {len(rows)} 条")

    for row in rows:
        aid = row["announcement_id"]
        flag = "★ " if row["is_high_value"] else ""
        label = (f"{flag}{row['notice_date']}　{row['category']}　"
                 f"{row['title'][:46]}")
        with st.expander(label):
            st.caption(f"公告编号 {aid}　｜　类型：{row['category']}　｜　"
                       f"PDF 大小 {row['adjunct_size']} KB")

            res = load_analysis(aid) or st.session_state.get(f"res_{aid}")

            if res:
                render_analysis(res)
            else:
                has_text = bool(row.get("content_text"))
                if not ALLOW_LIVE_ANALYSIS:
                    st.info("演示版只展示已预生成的解读结果，不提供实时调用。"
                            "如需完整体验，请在本地运行本项目。")
                else:
                    used = st.session_state.get("live_used", 0)
                    left = LIVE_QUOTA - used
                    if left <= 0:
                        st.info("本会话的实时处理次数已用完（刷新页面可重置）。"
                                "想看更多，可在本地运行本项目。")
                    else:
                        tip = "这条公告的正文还没抓取。" if not has_text else ""
                        st.caption(f"{tip}点下方按钮现场处理，约 5–10 秒"
                                   f"（本会话还可 {left} 次）")
                        label_btn = "获取正文并生成解读" if not has_text else "生成 AI 解读"
                        if st.button(label_btn, key=f"btn_{aid}"):
                            with st.spinner("正在获取正文并生成解读……"):
                                ok = True
                                if not has_text:
                                    text = ensure_content(row)
                                    if not text:
                                        st.warning("这条 PDF 解析不出文字——多半是扫描件"
                                                   "（常见于律所意见书、审计报告），"
                                                   "或链接已失效。")
                                        ok = False
                                if ok:
                                    try:
                                        result = analyze_notice(row)
                                        save_analysis(result)
                                        st.session_state[f"res_{aid}"] = {
                                            "extracted": result["extracted"],
                                            "interpreted": result["interpreted"],
                                            "score": result["score"],
                                            "total": result["score"]["total"],
                                        }
                                        st.session_state["live_used"] = used + 1
                                        st.rerun()
                                    except Exception as exc:  # noqa: BLE001
                                        st.error(f"处理失败：{exc}")


if __name__ == "__main__":
    main()
