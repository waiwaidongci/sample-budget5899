"""命令行入口：批次创建/提交/查看/重跑/修正，以及一次性求解 JSON 文件。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .batches import BatchError, BatchStore
from .solver import CellInput, build_problem, solve
from .batches import serialize_result


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="点击审计批次分析 CLI")
    parser.add_argument("--data", default="/workspace/data", help="批次存储根目录")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出批次")

    p_create = sub.add_parser("create", help="创建批次")
    p_create.add_argument("--note", default="")

    p_submit = sub.add_parser("submit", help="提交一份数据")
    p_submit.add_argument("batch_id")
    p_submit.add_argument("kind", choices=("cells", "channel_audit", "date_audit"))
    p_submit.add_argument("json_path")

    p_show = sub.add_parser("show", help="查看批次详情（含分析结果）")
    p_show.add_argument("batch_id")

    p_rerun = sub.add_parser("rerun", help="重跑并验证结果一致性")
    p_rerun.add_argument("batch_id")

    p_fix = sub.add_parser("correct", help="基于旧批次开修正批次")
    p_fix.add_argument("parent_batch_id")
    p_fix.add_argument("--note", default="")

    p_solve = sub.add_parser("solve-once", help="不建批次，直接求解三份 JSON")
    p_solve.add_argument("cells_json")
    p_solve.add_argument("channel_audit_json")
    p_solve.add_argument("date_audit_json")

    args = parser.parse_args(argv)
    store = BatchStore(args.data)

    try:
        if args.cmd == "list":
            print(json.dumps(store.list_batches(), ensure_ascii=False, indent=2))
        elif args.cmd == "create":
            print(json.dumps(store.create_batch(args.note), ensure_ascii=False, indent=2))
        elif args.cmd == "submit":
            meta = store.submit(args.batch_id, args.kind, _load(args.json_path))
            print(json.dumps(meta, ensure_ascii=False, indent=2))
        elif args.cmd == "show":
            print(json.dumps(store.get_detail(args.batch_id), ensure_ascii=False, indent=2))
        elif args.cmd == "rerun":
            print(json.dumps(store.rerun(args.batch_id), ensure_ascii=False, indent=2))
        elif args.cmd == "correct":
            print(json.dumps(store.create_correction(
                args.parent_batch_id, args.note), ensure_ascii=False, indent=2))
        elif args.cmd == "solve-once":
            cells_doc = _load(args.cells_json)
            ch_doc = _load(args.channel_audit_json)
            da_doc = _load(args.date_audit_json)
            cells = [CellInput(**r) for r in cells_doc["rows"]]
            ca = {r["channel"]: r["valid_clicks"] for r in ch_doc["entries"]}
            da = {r["date"]: r["valid_clicks"] for r in da_doc["entries"]}
            problem = build_problem(cells, ca, da)
            print(json.dumps(serialize_result(problem, solve(problem)),
                             ensure_ascii=False, indent=2))
        return 0
    except (BatchError, Exception) as e:  # 顶层统一失败输出
        print(f"错误：{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
