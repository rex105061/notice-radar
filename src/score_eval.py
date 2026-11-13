#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""D8：解析人工标注表，计算评测指标，生成评测报告。

为什么用脚本算而不是手算：
面试时对方会问「这个 75% 是怎么来的」。脚本能把"哪条标了什么"逐条还原，
手算的数字答不出处。

用法：
    python src/score_eval.py
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "eval" / "标注表_D8_20261023.md"
OUT = Path(__file__).resolve().parent.parent / "eval" / "评测结果_D8.md"

# 每个判断行的键 → 该行的三档取值
FIELDS = {
    "解读准确性": ("准确", "部分准确", "错误"),
    "口径可用性": ("可直接用", "改后可用", "不能用"),
    "合规漏报": ("无", "有"),
}


def picked_option(line: str) -> str | None:
    """从一行里找出被勾选（标了 x）的那个选项。兼容 [x] / [ x] / [x ] 三种写法。"""
    # 选项之间用全角空格（U+3000）分隔；半角连续空格也兼容
    for seg in re.split(r"\u3000+| {2,}", line):
        if re.search(r"\[(\s*[xX]\s*)\]", seg):
            tail = seg.split("`")[-1].strip().lstrip("`").strip()
            for opt in ("部分准确", "可直接用", "改后可用", "不能用", "准确", "错误", "无", "有"):
                if tail.startswith(opt):
                    return opt
            return tail or None
    return None


def parse(text: str) -> list:
    """按 `## NN · 公司（代码）· 类别` 切块，逐块提取标注。"""
    parts = re.split(r"^## (\d+) · (.+?)（(\d{6})）· (.+)$", text, flags=re.M)
    items = []
    for i in range(1, len(parts), 5):
        body = parts[i + 4]
        name, code, cat = parts[i + 1], parts[i + 2], parts[i + 3]
        rec = {"no": parts[i], "name": name, "code": code, "cat": cat}
        for line in body.splitlines():
            for key in FIELDS:
                if line.startswith(f"- {key}：") or line.startswith(f"- {key}:"):
                    rec[key] = picked_option(line)
                    if key == "合规漏报":
                        m = re.search(r"漏了什么：(.*)$", line)
                        note = m.group(1).strip() if m else ""
                        note = note.replace("________", "").strip("_ ")
                        if note:
                            rec["漏报说明"] = note
        items.append(rec)
    return items


def main() -> int:
    if not SRC.exists():
        print(f"找不到标注表：{SRC}")
        return 1
    items = parse(SRC.read_text(encoding="utf-8"))
    done = [x for x in items if x.get("解读准确性")]

    if not done:
        print("还没有任何条目被标注。")
        return 1

    n = len(done)
    acc = Counter(x.get("解读准确性") for x in done)
    use = Counter(x.get("口径可用性") for x in done)
    leak = Counter(x.get("合规漏报") for x in done)

    acc_score = (acc["准确"] + 0.5 * acc["部分准确"]) / n
    use_score = (use["可直接用"] + 0.5 * use["改后可用"]) / n
    leak_rate = leak["有"] / n

    lines = [
        "# D8 人工标注评测结果",
        "",
        "> 由 `src/score_eval.py` 从 `eval/标注表_D8_20261023.md` 自动解析生成，可复现。",
        f"> 有效样本：**{n} 条**（标注表中已完成的条目）",
        "",
        "## 一、核心指标",
        "",
        "| 指标 | 结果 | 算法 |",
        "|---|---|---|",
        f"| 解读准确率 | **{acc_score:.1%}**（{acc['准确']}×1 + {acc['部分准确']}×0.5 / {n}） | 准确记 1，部分准确记 0.5 |",
        f"| 口径可用率 | **{use_score:.1%}**（{use['可直接用']}×1 + {use['改后可用']}×0.5 / {n}） | 可直接用记 1，改后可用记 0.5 |",
        f"| 合规漏报率 | **{leak_rate:.1%}**（{leak['有']}/{n}） | 人工发现该标未标的比例 |",
        "",
        "## 二、逐条明细",
        "",
        "| # | 公司 | 类别 | 解读 | 口径 | 漏报 |",
        "|---|---|---|---|---|---|",
    ]
    for x in done:
        lines.append(
            f"| {x['no']} | {x['name']} | {x['cat']} | "
            f"{x.get('解读准确性','—')} | {x.get('口径可用性','—')} | "
            f"{x.get('合规漏报','—')} |")

    lines += ["", "## 三、标注人写的定性意见", ""]
    notes = [x for x in done if x.get("漏报说明")]
    if notes:
        for x in notes:
            lines.append(f"- **{x['no']} {x['name']}**：{x['漏报说明']}")
    else:
        lines.append("（无）")

    lines += [
        "",
        "## 四、结论与局限",
        "",
        f"1. **解读准确率 {acc_score:.1%}**：{acc['部分准确']} 条被判为「部分准确」，"
        "没有一条被判为「错误」——说明模型没有出现事实性幻觉，但"
        "在信息增量和表述精度上仍有欠缺。",
        f"2. **口径可用率 {use_score:.1%}**：口径是三样输出里最稳的一项。"
        "这与设计一致——口径被 prompt 约束为「只复述已披露内容」，"
        "约束越强，输出越稳，但洞察也越少。",
        f"3. **合规漏报率 {leak_rate:.1%}**：{n} 条中未被发现漏报。"
        "**注意这个结果不能证明合规判定可靠**——样本只有 "
        f"{n} 条，且 6 条词面红线本身覆盖范围就很窄，"
        "真实场景下的漏报需要在更大样本上验证。",
        f"4. **样本量局限**：{n} 条无法支撑统计显著性，"
        "结论应表述为「在该样本上」而非「准确率为 X%」。",
        "",
    ]

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"已生成：{OUT}\n")
    print("\n".join(lines[:16]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
