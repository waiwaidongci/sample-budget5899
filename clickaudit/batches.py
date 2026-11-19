"""批次生命周期管理：三份数据同批齐全才冻结分析。

存储（默认 ./data/batches，纯 JSON + 文件锁，零依赖）：
  data/batches/<batch_id>/
    meta.json              批次元数据（状态、父批次、校验和）
    submissions/
      cells.json           触达+日志点击明细（第三份数据）
      channel_audit.json   渠道审计（第一份）
      date_audit.json      日期审计（第二份）
    analysis.json          冻结后的分析结果（不可变；重跑只验证不覆盖）
    runs.log               每次分析/重跑的记录（时间、输入校验和、结果校验和）

规则：
- 三份数据在同一批次内到齐（顺序任意、可并发补交）才触发冻结分析；
  未齐全时批次保持 open，绝不产出分析结论。
- 同一批次同类型数据重复提交 => 拒绝（防止补交悄悄改写已登记口径）。
- 修正数据 = 另开一个 correction 批次，关联 parent_batch_id；
  旧批次与其冻结结果原样保留。
- 重跑旧批次：输入未变则结果必须逐字节一致（校验和比对）；
  输入被外部篡改则拒绝并标记，不覆盖历史结果。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .solver import (
    CellInput,
    Conflict,
    SolverError,
    build_problem,
    solve,
)

DATA_KINDS = ("cells", "channel_audit", "date_audit")
OPEN = "open"
FROZEN = "frozen"
FAILED = "failed"  # 数据合法但审计约束无解（冲突），同样冻结输入

_process_lock = threading.RLock()


class BatchError(Exception):
    pass


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).digest().hex()


def canonical_json(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _payload_checksum(kind: str, payload) -> str:
    return _sha256_bytes(kind.encode("utf-8") + b"|" + canonical_json(payload))


class BatchStore:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        (self.root / "batches").mkdir(parents=True, exist_ok=True)

    # ---------- 内部工具 ----------
    def _dir(self, batch_id: str) -> Path:
        self._validate_id(batch_id)
        d = self.root / "batches" / batch_id
        if not d.exists():
            raise BatchError(f"批次不存在：{batch_id}")
        return d

    @staticmethod
    def _validate_id(batch_id: str) -> None:
        if not batch_id or not all(ch.isalnum() or ch in "-_" for ch in batch_id):
            raise BatchError("非法批次 ID")

    def _meta_path(self, batch_id: str) -> Path:
        return self._dir(batch_id) / "meta.json"

    def read_meta(self, batch_id: str) -> dict:
        with self._meta_path(batch_id).open("r", encoding="utf-8") as f:
            return json.load(f)

    def _write_meta(self, batch_id: str, meta: dict) -> None:
        path = self._meta_path(batch_id)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _lock(self, batch_id: str):
        return _FileLock(self._dir(batch_id) / ".lock")

    # ---------- 批次创建 ----------
    def create_batch(self, note: str = "", parent_batch_id: Optional[str] = None) -> dict:
        with _process_lock:
            if parent_batch_id is not None:
                parent = self.read_meta(parent_batch_id)
                if parent["status"] not in (FROZEN, FAILED):
                    raise BatchError("只能基于已冻结（含冲突冻结）的批次发起修正")
            batch_id = time.strftime("B%Y%m%d-%H%M%S-") + _sha256_bytes(
                str(time.time_ns()).encode())[:10]
            d = self.root / "batches" / batch_id
            (d / "submissions").mkdir(parents=True)
            meta = {
                "batch_id": batch_id,
                "status": OPEN,
                "note": note,
                "parent_batch_id": parent_batch_id,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "received": {k: None for k in DATA_KINDS},
                "checksums": {},
                "frozen_at": None,
                "analysis_checksum": None,
            }
            self._write_meta(batch_id, meta)
            self._append_run_log(batch_id, "create", {"note": note})
            return meta

    # ---------- 数据提交（支持并发补交） ----------
    def submit(self, batch_id: str, kind: str, payload: dict) -> dict:
        if kind not in DATA_KINDS:
            raise BatchError(f"数据类型必须是 {DATA_KINDS} 之一")
        with _process_lock, self._lock(batch_id):
            meta = self.read_meta(batch_id)
            if meta["status"] != OPEN:
                raise BatchError(f"批次 {batch_id} 已{meta['status']}，不能再补交；修正请另开批次")
            if meta["received"][kind] is not None:
                raise BatchError(f"批次 {batch_id} 的 {kind} 已登记，重复提交被拒绝；修正请另开批次")
            clean = self._validate_payload(kind, payload)
            path = self._dir(batch_id) / "submissions" / f"{kind}.json"
            tmp = path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(clean, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            checksum = _payload_checksum(kind, clean)
            meta["received"][kind] = time.strftime("%Y-%m-%dT%H:%M:%S")
            meta["checksums"][kind] = checksum
            self._write_meta(batch_id, meta)
            self._append_run_log(batch_id, "submit",
                                 {"kind": kind, "checksum": checksum})
            all_in = all(meta["received"][k] is not None for k in DATA_KINDS)
            if all_in:
                self._freeze_and_analyze(batch_id)
                meta = self.read_meta(batch_id)
            return meta

    @staticmethod
    def _validate_payload(kind: str, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise BatchError("提交内容必须是 JSON 对象")
        if kind == "cells":
            rows = payload.get("rows")
            if not isinstance(rows, list) or not rows:
                raise BatchError("cells.rows 必须是非空数组")
            clean_rows = []
            for i, row in enumerate(rows):
                try:
                    reach = int(row["reach"])
                    log_clicks = int(row["log_clicks"])
                except (KeyError, TypeError, ValueError) as e:
                    raise BatchError(f"cells 第 {i} 行字段缺失或非整数：{e}") from None
                if reach < 0 or log_clicks < 0:
                    raise BatchError(f"cells 第 {i} 行触达/日志点击不能为负")
                clean_rows.append(asdict(CellInput(
                    group=str(row["group"]),
                    channel=str(row["channel"]),
                    date=str(row["date"]),
                    reach=reach,
                    log_clicks=log_clicks,
                )))
            return {"rows": clean_rows}
        if kind in ("channel_audit", "date_audit"):
            key = "channel" if kind == "channel_audit" else "date"
            entries = payload.get("entries")
            if not isinstance(entries, list) or not entries:
                raise BatchError(f"{kind}.entries 必须是非空数组")
            clean_entries = []
            seen = set()
            for i, row in enumerate(entries):
                try:
                    name = str(row[key])
                    value = int(row["valid_clicks"])
                except (KeyError, TypeError, ValueError) as e:
                    raise BatchError(f"{kind} 第 {i} 行非法：{e}") from None
                if value < 0:
                    raise BatchError(f"{kind} 第 {i} 行有效点击不能为负")
                if name in seen:
                    raise BatchError(f"{kind} 中 {key}={name} 重复")
                seen.add(name)
                clean_entries.append({key: name, "valid_clicks": value})
            return {"entries": clean_entries}
        raise BatchError("未知数据类型")

    # ---------- 冻结与分析 ----------
    def _freeze_and_analyze(self, batch_id: str) -> None:
        meta = self.read_meta(batch_id)
        try:
            data = self._load_submissions(batch_id)
            result = self._compute(data)
            status = FAILED if not result["feasible"] else FROZEN
            meta["status"] = status
            meta["frozen_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            analysis_path = self._dir(batch_id) / "analysis.json"
            body = canonical_json(result)
            tmp = analysis_path.with_suffix(".json.tmp")
            tmp.write_bytes(body)
            os.replace(tmp, analysis_path)
            meta["analysis_checksum"] = _sha256_bytes(body)
            self._write_meta(batch_id, meta)
            self._append_run_log(batch_id, "freeze", {
                "status": status,
                "analysis_checksum": meta["analysis_checksum"],
            })
        except SolverError as e:
            # 输入口径本身不合法：不冻结，批次保持 open 等待修正提交
            self._append_run_log(batch_id, "reject", {"reason": str(e)})
            raise BatchError(f"输入校验未通过，批次保持 open：{e}") from None

    def _load_submissions(self, batch_id: str) -> Tuple[List[CellInput], dict, dict]:
        base = self._dir(batch_id) / "submissions"
        cells_doc = json.loads((base / "cells.json").read_text(encoding="utf-8"))
        ch_doc = json.loads((base / "channel_audit.json").read_text(encoding="utf-8"))
        da_doc = json.loads((base / "date_audit.json").read_text(encoding="utf-8"))
        cells = [CellInput(**row) for row in cells_doc["rows"]]
        channel_audit = {r["channel"]: r["valid_clicks"] for r in ch_doc["entries"]}
        date_audit = {r["date"]: r["valid_clicks"] for r in da_doc["entries"]}
        return cells, channel_audit, date_audit

    @staticmethod
    def _compute(data) -> dict:
        cells, channel_audit, date_audit = data
        problem = build_problem(cells, channel_audit, date_audit)
        result = solve(problem)
        return serialize_result(problem, result)

    def rerun(self, batch_id: str) -> dict:
        """重跑已冻结批次：输入校验和必须不变，结果逐字节一致。"""
        with _process_lock, self._lock(batch_id):
            meta = self.read_meta(batch_id)
            if meta["status"] not in (FROZEN, FAILED):
                raise BatchError("只有已冻结批次可以重跑")
            # 1. 输入文件校验和与冻结时记录比对
            for kind in DATA_KINDS:
                path = self._dir(batch_id) / "submissions" / f"{kind}.json"
                actual = _sha256_bytes(
                    kind.encode() + b"|" + canonical_json(json.loads(
                        path.read_text(encoding="utf-8"))))
                if actual != meta["checksums"][kind]:
                    self._append_run_log(batch_id, "rerun_tamper",
                                         {"kind": kind})
                    raise BatchError(
                        f"批次 {batch_id} 的 {kind} 与冻结时不一致，拒绝重跑；"
                        "请基于当前数据另开修正批次")
            # 2. 重新计算
            data = self._load_submissions(batch_id)
            result = self._compute(data)
            body = canonical_json(result)
            new_checksum = _sha256_bytes(body)
            consistent = new_checksum == meta["analysis_checksum"]
            self._append_run_log(batch_id, "rerun", {
                "consistent": consistent,
                "analysis_checksum": new_checksum,
            })
            if not consistent:
                raise BatchError("重跑结果与冻结结果不一致（确定性被破坏）")
            return {"batch_id": batch_id, "consistent": True,
                    "analysis_checksum": new_checksum}

    # ---------- 修正批次 ----------
    def create_correction(self, parent_batch_id: str, note: str = "") -> dict:
        with _process_lock:
            meta = self.create_batch(
                note=note or f"修正自 {parent_batch_id}",
                parent_batch_id=parent_batch_id)
            self._append_run_log(meta["batch_id"], "correction_open",
                                 {"parent": parent_batch_id})
            return meta

    # ---------- 查询 ----------
    def list_batches(self) -> List[dict]:
        out = []
        base = self.root / "batches"
        for d in sorted(base.iterdir() if base.exists() else []):
            if d.is_dir() and (d / "meta.json").exists():
                out.append(self.read_meta(d.name))
        return sorted(out, key=lambda m: m["created_at"], reverse=True)

    def get_detail(self, batch_id: str) -> dict:
        with self._lock(batch_id):
            meta = self.read_meta(batch_id)
            detail = {"meta": meta, "submissions": {}}
            for kind in DATA_KINDS:
                path = self._dir(batch_id) / "submissions" / f"{kind}.json"
                if path.exists():
                    detail["submissions"][kind] = json.loads(
                        path.read_text(encoding="utf-8"))
            analysis_path = self._dir(batch_id) / "analysis.json"
            if analysis_path.exists():
                detail["analysis"] = json.loads(
                    analysis_path.read_text(encoding="utf-8"))
            log_path = self._dir(batch_id) / "runs.log"
            if log_path.exists():
                detail["run_log"] = log_path.read_text(encoding="utf-8")
            return detail

    def _append_run_log(self, batch_id: str, event: str, extra: dict) -> None:
        line = json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "event": event, **extra,
        }, ensure_ascii=False)
        with (self._dir(batch_id) / "runs.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")


class _FileLock:
    """fcntl 跨进程锁 + 线程可重入保护，适配并发补交。"""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def __enter__(self):
        import fcntl
        self._fh = self.path.open("a+")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        import fcntl
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()
        self._fh = None


# ---------- 结果序列化（分数精确值 + 端点分配） ----------
def serialize_result(problem, result) -> dict:
    payload = {
        "feasible": result.feasible,
        "totals": result.totals,
        "schema": {
            "groups": problem.groups,
            "channels": problem.channels,
            "dates": problem.dates,
        },
        "inputs": {
            "reach": {f"{g}|{c}|{d}": v for (g, c, d), v in problem.reach.items()},
            "log_clicks": {f"{g}|{c}|{d}": v
                           for (g, c, d), v in problem.log_clicks.items()},
            "caps": {f"{g}|{c}|{d}": v for (g, c, d), v in problem.caps.items()},
            "channel_audit": problem.channel_audit,
            "date_audit": problem.date_audit,
        },
    }
    if not result.feasible:
        conflict: Conflict = result.conflict
        payload["conflict"] = {
            "kind": conflict.kind,
            "message": conflict.message,
            "details": conflict.details,
        }
        return payload

    def bound_dict(b):
        return {
            "ctr_diff_fraction": f"{b.fraction.numerator}/{b.fraction.denominator}",
            "ctr_diff_decimal": b.decimal,
            "group_clicks": b.group_clicks,
            "group_ctr": {g: f"{v.numerator}/{v.denominator}"
                          for g, v in b.group_ctr.items()},
            "group_ctr_decimal": {g: float(v) for g, v in b.group_ctr.items()},
            "allocation": b.allocation,
        }

    payload["lower_bound"] = bound_dict(result.lower)
    payload["upper_bound"] = bound_dict(result.upper)
    return payload
