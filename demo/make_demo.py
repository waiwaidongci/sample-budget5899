"""生成演示数据：可解批次 + 总数相等但无解批次 + 修正批次。

运行：python3 -I demo/make_demo.py
输出：demo/data/*.json 与 demo/expected.txt
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).parent / "data"
OUT.mkdir(parents=True, exist_ok=True)

CHANNELS = ["搜索", "信息流", "社交"]
DATES = ["2026-11-17", "2026-11-18", "2026-11-19"]


def write(name, doc):
    (OUT / name).write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                            encoding="utf-8")


def make_cells(reach_table, log_table):
    rows = []
    for g in ("candidate", "control"):
        for ci, c in enumerate(CHANNELS):
            for di, d in enumerate(DATES):
                rows.append({
                    "group": g, "channel": c, "date": d,
                    "reach": reach_table[g][ci][di],
                    "log_clicks": log_table[g][ci][di],
                })
    return {"rows": rows}


# ---- 批次 A：正常可解，日志含误触（日志点击 > 审计有效点击；部分格 cap 受限）----
reach_a = {
    "candidate": [[1000, 1000, 1000], [800, 800, 800], [600, 600, 600]],
    "control":   [[1000, 1000, 1000], [800, 800, 800], [600, 600, 600]],
}
# 日志点击：渠道“搜索”在 11-17 误触严重，真实可用 cap 很小
log_a = {
    "candidate": [[120, 900, 950], [700, 750, 760], [500, 520, 540]],
    "control":   [[110, 880, 930], [690, 740, 750], [490, 510, 530]],
}
write("A_cells.json", make_cells(reach_a, log_a))
write("A_channel.json", {"entries": [
    {"channel": "搜索", "valid_clicks": 1500},
    {"channel": "信息流", "valid_clicks": 3200},
    {"channel": "社交", "valid_clicks": 2100},
]})
write("A_date.json", {"entries": [
    {"date": "2026-11-17", "valid_clicks": 2000},
    {"date": "2026-11-18", "valid_clicks": 2300},
    {"date": "2026-11-19", "valid_clicks": 2500},
]})

# ---- 批次 B：渠道合计 == 日期合计（都是 3000），但结构冲突，无解 ----
log_b = {
    # 搜索渠道 11-17 两格 cap 均为 0；而日期 11-17 审计要求过高
    "candidate": [[0, 900, 950], [700, 750, 760], [500, 520, 540]],
    "control":   [[0, 880, 930], [690, 740, 750], [490, 510, 530]],
}
write("B_cells.json", make_cells(reach_a, log_b))
write("B_channel.json", {"entries": [
    {"channel": "搜索", "valid_clicks": 1600},
    {"channel": "信息流", "valid_clicks": 900},
    {"channel": "社交", "valid_clicks": 500},
]})
# 日期 11-17 要 2600，但当天跨组格子容量：
# 搜索 0+0，信息流 700+690，社交 500+490 => 仅 2380 < 2600
write("B_date.json", {"entries": [
    {"date": "2026-11-17", "valid_clicks": 2600},
    {"date": "2026-11-18", "valid_clicks": 200},
    {"date": "2026-11-19", "valid_clicks": 200},
]})

# ---- 批次 C：修正 B（放宽 11-17 的搜索日志 cap），应可解 ----
log_c = {
    # 搜索 11-17 放宽到每组 700（合计 1400），配合 18/19 日各 200 的额度可承载 1600
    "candidate": [[700, 900, 950], [700, 750, 760], [500, 520, 540]],
    "control":   [[700, 880, 930], [690, 740, 750], [490, 510, 530]],
}
write("C_cells.json", make_cells(reach_a, log_c))
write("C_channel.json", {"entries": [
    {"channel": "搜索", "valid_clicks": 1600},
    {"channel": "信息流", "valid_clicks": 900},
    {"channel": "社交", "valid_clicks": 500},
]})
write("C_date.json", {"entries": [
    {"date": "2026-11-17", "valid_clicks": 2600},
    {"date": "2026-11-18", "valid_clicks": 200},
    {"date": "2026-11-19", "valid_clicks": 200},
]})

print("演示数据已写入", OUT)
