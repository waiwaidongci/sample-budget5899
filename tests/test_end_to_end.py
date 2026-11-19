"""端到端测试：批次生命周期、并发补交、重跑确定性、修正、HTTP API。

运行：python3 -I tests/test_end_to_end.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, "/workspace")

from clickaudit.batches import BatchError, BatchStore  # noqa: E402
from clickaudit.server import Handler  # noqa: E402
from clickaudit.solver import CellInput, build_problem, solve  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

DEMO = Path("/workspace/demo/data")
FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)
        print("  FAIL:", msg)
    else:
        print("  ok:", msg)


def load(name):
    return json.loads((DEMO / name).read_text(encoding="utf-8"))


def fresh_store():
    tmp = Path(tempfile.mkdtemp(prefix="clickaudit-test-"))
    return tmp, BatchStore(tmp)


def submit_all(store, bid, prefix):
    store.submit(bid, "cells", load(f"{prefix}_cells.json"))
    store.submit(bid, "channel_audit", load(f"{prefix}_channel.json"))
    store.submit(bid, "date_audit", load(f"{prefix}_date.json"))


# ---------- 1. 可解批次：约束、端点、整数性 ----------
def test_feasible_bounds():
    print("[1] 可解批次 A 的上下界与端点分配")
    p = build_problem(
        [CellInput(**r) for r in load("A_cells.json")["rows"]],
        {r["channel"]: r["valid_clicks"] for r in load("A_channel.json")["entries"]},
        {r["date"]: r["valid_clicks"] for r in load("A_date.json")["entries"]},
    )
    res = solve(p)
    check(res.feasible, "A 可行")
    check(res.lower.fraction <= res.upper.fraction, "下界不超过上界")
    caps = p.caps
    for bname, b in (("lower", res.lower), ("upper", res.upper)):
        ch = {c: 0 for c in p.channels}
        da = {d: 0 for d in p.dates}
        gc = {g: 0 for g in p.groups}
        for g in p.groups:
            for ci, c in enumerate(p.channels):
                for di, d in enumerate(p.dates):
                    x = b.allocation[g][ci][di]
                    check(isinstance(x, int), f"{bname} 分配为整数 {g}/{c}/{d}")
                    check(0 <= x <= caps[(g, c, d)],
                          f"{bname} {g}/{c}/{d}={x} 不超 cap={caps[(g,c,d)]} 且非负")
                    ch[c] += x; da[d] += x; gc[g] += x
        check(ch == p.channel_audit, f"{bname} 渠道边际精确满足")
        check(da == p.date_audit, f"{bname} 日期边际精确满足")
        check(sum(gc.values()) == 6800, f"{bname} 总点击=6800")
        # 端点点击率差复核
        from fractions import Fraction
        rc = {g: p.group_reach(g) for g in p.groups}
        diff = Fraction(gc["candidate"], rc["candidate"]) - Fraction(gc["control"], rc["control"])
        check(diff == b.fraction, f"{bname} 分数口径与分配一致")
    print("    区间：", res.lower.fraction, "到", res.upper.fraction,
          f"({res.lower.decimal:.6f} ~ {res.upper.decimal:.6f})")


# ---------- 2. 总数相等却无解 ----------
def test_equal_total_infeasible():
    print("[2] 批次 B：渠道合计=日期合计=3000 但无解")
    p = build_problem(
        [CellInput(**r) for r in load("B_cells.json")["rows"]],
        {r["channel"]: r["valid_clicks"] for r in load("B_channel.json")["entries"]},
        {r["date"]: r["valid_clicks"] for r in load("B_date.json")["entries"]},
    )
    sum_c = sum(p.channel_audit.values())
    sum_d = sum(p.date_audit.values())
    check(sum_c == sum_d == 3000, "两套审计总数相等 (3000)")
    res = solve(p)
    check(not res.feasible, "B 判定无解")
    check(res.conflict is not None and res.conflict.kind == "cut_certificate",
          "给出最小割冲突证书")
    print("   ", res.conflict.message)


# ---------- 3. 三份齐全才冻结；并发补交 ----------
def test_freeze_and_concurrency():
    print("[3] 同批三份齐全才冻结 + 并发补交")
    tmp, store = fresh_store()
    try:
        bid = store.create_batch("并发测试")["batch_id"]
        docs = {
            "cells": load("A_cells.json"),
            "channel_audit": load("A_channel.json"),
            "date_audit": load("A_date.json"),
        }
        # 前两份并发
        with ThreadPoolExecutor(3) as ex:
            f1 = ex.submit(store.submit, bid, "cells", docs["cells"])
            f2 = ex.submit(store.submit, bid, "channel_audit", docs["channel_audit"])
            f1.result(); f2.result()
        meta = store.read_meta(bid)
        check(meta["status"] == "open", "只交两份时保持 open，不产出分析")
        check(not (tmp / "batches" / bid / "analysis.json").exists(),
              "未齐全时不存在 analysis.json")
        # 第三份并发重复提交尝试：恰好一个成功并触发冻结，另一个被拒
        barrier = threading.Barrier(2)

        def submit_date():
            barrier.wait()
            try:
                return ("ok", store.submit(bid, "date_audit", docs["date_audit"]))
            except BatchError as e:
                return ("rejected", str(e))

        with ThreadPoolExecutor(2) as ex:
            outcomes = list(ex.map(lambda _: submit_date(), range(2)))
        labels = sorted(label for label, _ in outcomes)
        check(labels == ["ok", "rejected"],
              "并发同批补交由文件锁串行裁决：一份成功、重复一份被拒")
        check(outcomes[0][1]["status"] if outcomes[0][0] == "ok"
              else outcomes[1][1]["status"] == "frozen",
              "第三份到达后冻结分析")
        meta = store.read_meta(bid)
        check(meta["status"] == "frozen", "批次最终 frozen")
        detail = store.get_detail(bid)
        check(detail["analysis"]["feasible"], "冻结结果可行")

        # 冻结后补交被拒
        try:
            store.submit(bid, "cells", docs["cells"])
            check(False, "冻结后补交应被拒")
        except BatchError:
            check(True, "冻结后补交被拒，要求另开修正批次")

        # 同类型重复提交被拒（open 状态新批次）
        bid2 = store.create_batch("重复提交")["batch_id"]
        store.submit(bid2, "cells", docs["cells"])
        try:
            store.submit(bid2, "cells", docs["cells"])
            check(False, "同类型重复提交应被拒")
        except BatchError:
            check(True, "同类型重复提交被拒")
    finally:
        shutil.rmtree(tmp)


# ---------- 4. 重跑一致 + 篡改拒绝 ----------
def test_rerun_determinism():
    print("[4] 重跑结果逐字节一致；外部篡改被识别")
    tmp, store = fresh_store()
    try:
        bid = store.create_batch("重跑测试")["batch_id"]
        submit_all(store, bid, "A")
        first = store.read_meta(bid)["analysis_checksum"]
        r = store.rerun(bid)
        check(r["consistent"] and r["analysis_checksum"] == first,
              "重跑 analysis_checksum 与冻结时完全一致")
        # 篡改输入文件
        target = tmp / "batches" / bid / "submissions" / "date_audit.json"
        doc = json.loads(target.read_text(encoding="utf-8"))
        doc["entries"][0]["valid_clicks"] += 1
        target.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        try:
            store.rerun(bid)
            check(False, "篡改后重跑应被拒")
        except BatchError:
            check(True, "输入校验和不匹配时拒绝重跑，历史结果保留")
        frozen = json.loads((tmp / "batches" / bid / "analysis.json").read_text())
        check(frozen["upper_bound"]["ctr_diff_fraction"] ==
              store.get_detail(bid)["analysis"]["upper_bound"]["ctr_diff_fraction"],
              "历史冻结结果未被覆盖")
    finally:
        shutil.rmtree(tmp)


# ---------- 5. 修正另开一批，旧批不变 ----------
def test_correction_chain():
    print("[5] 无解批次 B 另开修正批次 C；旧批保留")
    tmp, store = fresh_store()
    try:
        b = store.create_batch("误触冲突批")["batch_id"]
        submit_all(store, b, "B")
        check(store.read_meta(b)["status"] == "failed", "B 冻结为 failed（冲突留痕）")
        c = store.create_correction(b, "放宽搜索渠道 11-17 日志口径")
        check(c["parent_batch_id"] == b and c["status"] == "open",
              "修正批次关联父批次且为 open")
        submit_all(store, c["batch_id"], "C")
        cmeta = store.read_meta(c["batch_id"])
        check(cmeta["status"] == "frozen", "修正批 C 成功冻结")
        check(store.read_meta(b)["status"] == "failed", "父批次 B 结果保持不变")
        detail_c = store.get_detail(c["batch_id"])
        check(detail_c["analysis"]["feasible"], "C 分析可行")
        # 不能基于 open 批次开修正
        o = store.create_batch("open 批")
        try:
            store.create_correction(o["batch_id"])
            check(False, "open 批次不允许开修正")
        except BatchError:
            check(True, "仅冻结/冲突批次可发起修正")
    finally:
        shutil.rmtree(tmp)


# ---------- 6. 输入校验 ----------
def test_input_validation():
    print("[6] 非法输入拒绝且不冻结")
    tmp, store = fresh_store()
    try:
        bid = store.create_batch("非法输入")["batch_id"]
        bad = {"rows": [{"group": "candidate", "channel": "x", "date": "d",
                         "reach": -1, "log_clicks": 3}]}
        try:
            store.submit(bid, "cells", bad)
            check(False, "负数应拒绝")
        except BatchError:
            check(True, "负数触达拒绝")
        check(store.read_meta(bid)["status"] == "open", "拒绝后批次仍 open")
        # 渠道集合不一致：三份到齐做整包校验时拒绝，不产生冻结结果
        store.submit(bid, "cells", load("A_cells.json"))
        store.submit(bid, "date_audit", load("A_date.json"))
        try:
            store.submit(bid, "channel_audit",
                         {"entries": [{"channel": "不存在", "valid_clicks": 1}]})
            check(False, "渠道集合不一致应在冻结时报错")
        except BatchError:
            check(True, "渠道集合不一致在第三份齐全时拒绝，批次保持 open")
        check(store.read_meta(bid)["status"] == "open", "口径不合法不产生冻结结果")
    finally:
        shutil.rmtree(tmp)


# ---------- 7. HTTP API 全链路 ----------
def test_http_api():
    print("[7] HTTP API：创建/补交/查询/重跑/修正")
    tmp = Path(tempfile.mkdtemp(prefix="clickaudit-http-"))
    import os
    os.environ["CLICKAUDIT_DATA"] = str(tmp)
    import clickaudit.server as srv
    srv._STORE = None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"

    def req(method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(base + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    try:
        status, meta = req("POST", "/api/batches", {"note": "API 批"})
        check(status == 201, "创建批次 201")
        bid = meta["batch_id"]
        file_for = {"cells": "A_cells.json",
                    "channel_audit": "A_channel.json",
                    "date_audit": "A_date.json"}
        for kind in ("cells", "channel_audit", "date_audit"):
            code, m = req("POST", f"/api/batches/{bid}/submit/{kind}",
                          load(file_for[kind]))
            check(code == 200, f"提交 {kind} 200")
        check(m["status"] == "frozen", "第三份提交后自动冻结")
        code, detail = req("GET", f"/api/batches/{bid}")
        check(code == 200 and detail["analysis"]["feasible"], "查询分析结果")
        code, r = req("POST", f"/api/batches/{bid}/rerun")
        check(code == 200 and r["consistent"], "API 重跑一致")
        code, corr = req("POST", f"/api/batches/{bid}/correction", {"note": "api修正"})
        check(code == 201 and corr["parent_batch_id"] == bid, "API 开修正批次")
        code, listing = req("GET", "/api/batches")
        check(code == 200 and len(listing["batches"]) >= 2, "批次列表")
        code, err = req("POST", f"/api/batches/{bid}/submit/cells", load("A_cells.json"))
        check(code == 400 and "error" in err, "冻结后补交返回 400")
        with urllib.request.urlopen(base + "/") as resp:
            check(b"<html" in resp.read(), "静态首页可访问")
    finally:
        httpd.shutdown()
        shutil.rmtree(tmp)


if __name__ == "__main__":
    tests = [
        test_feasible_bounds,
        test_equal_total_infeasible,
        test_freeze_and_concurrency,
        test_rerun_determinism,
        test_correction_chain,
        test_input_validation,
        test_http_api,
    ]
    for fn in tests:
        fn()
    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：")
        for m in FAILS:
            print(" -", m)
        raise SystemExit(1)
    print("全部测试通过 ✔")
