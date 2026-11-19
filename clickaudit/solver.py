"""触达/日志点击审计求解器（纯标准库）。

问题定义
========
按 (组 g, 渠道 c, 日期 d) 录入触达数 reach 与日志点击数 log_clicks，
审计只给出渠道 c 的有效点击总数 channel_audit[c] 与
日期 d 的有效点击总数 date_audit[d]。

决策变量 x[g][c][d] = 真实点击数，约束：
  0 <= x[g][c][d] <= cap[g][c][d] = min(reach, log_clicks)
  sum_{g,d} x[g][c][d] = channel_audit[c]   对每个渠道
  sum_{g,c} x[g][c][d] = date_audit[d]      对每个日期

目标：候选组与对照组的点击率差
  D(x) = sum_cd x[C][c][d]/R_C - sum_cd x[T][c][d]/R_T
其中 R_g = sum_cd reach[g][c][d]。求 D 的最紧上下界及达到端点的整数分配。

网络流建模（每个组、每个优化方向一张独立网络）
  source -> (g,c) 容量 sum_d cap[g][c][d]（组内渠道上限，等价于逐格上限
  的聚合）；(g,c) -> (g,d) 容量 cap[g][c][d]，单位费用按组方向取
  +/- scale/R_g；(g,d) -> sink 容量 sum_c cap[g][c][d]。
  再叠加 super-source/super-sink 承载渠道/日期审计的精确边际。
约束矩阵是二分图节点-关联矩阵（全幺模），整数容量 => LP 解天然整数，
SSP 最小费用流给出的就是整数最优解。
"""
from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Dict, List, Optional, Sequence, Tuple

CANDIDATE = "candidate"
CONTROL = "control"
GROUPS = (CANDIDATE, CONTROL)


class SolverError(ValueError):
    """输入数据本身不合法（区别于“合法但无解”的冲突）。"""


@dataclass
class CellInput:
    group: str
    channel: str
    date: str
    reach: int
    log_clicks: int


@dataclass
class _Edge:
    to: int
    cap: int
    cost: int
    rev: int


class _FlowNetwork:
    """带势能的最短路最小费用流 + 同一图上的最大流/最小割。"""

    def __init__(self, n: int):
        self.n = n
        self.g: List[List[_Edge]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, cap: int, cost: int = 0) -> None:
        fwd = _Edge(v, cap, cost, len(self.g[v]))
        rev = _Edge(u, 0, -cost, len(self.g[u]))
        self.g[u].append(fwd)
        self.g[v].append(rev)

    def max_flow(self, s: int, t: int, limit: Optional[int] = None) -> int:
        flow = 0
        while limit is None or flow < limit:
            prev_v, prev_e = self._bfs(s, t)
            if prev_v is None:
                break
            add = limit - flow if limit is not None else 10**18
            v = t
            while v != s:
                add = min(add, self.g[prev_v[v]][prev_e[v]].cap)
                v = prev_v[v]
            v = t
            while v != s:
                e = self.g[prev_v[v]][prev_e[v]]
                e.cap -= add
                self.g[v][e.rev].cap += add
                v = prev_v[v]
            flow += add
        return flow

    def _bfs(self, s: int, t: int):
        prev_v = [-1] * self.n
        prev_e = [-1] * self.n
        prev_v[s] = s
        q = deque([s])
        while q:
            u = q.popleft()
            for i, e in enumerate(self.g[u]):
                if e.cap > 0 and prev_v[e.to] < 0:
                    prev_v[e.to] = u
                    prev_e[e.to] = i
                    if e.to == t:
                        return prev_v, prev_e
                    q.append(e.to)
        return None, None

    def reachable_from_source(self, s: int) -> List[bool]:
        seen = [False] * self.n
        seen[s] = True
        q = deque([s])
        while q:
            u = q.popleft()
            for e in self.g[u]:
                if e.cap > 0 and not seen[e.to]:
                    seen[e.to] = True
                    q.append(e.to)
        return seen

    def min_cost_flow(self, s: int, t: int, required: int) -> Tuple[int, int]:
        """Dijkstra + 势能。返回 (流量, 费用)。流量不足时抛 SolverError。"""
        n = self.n
        potential = self._initial_potentials(s)
        flow = 0
        cost = 0
        INF = 10**30
        while flow < required:
            dist = [INF] * n
            dist[s] = 0
            prev_v = [-1] * n
            prev_e = [-1] * n
            # 简易二叉堆（heapq 为标准库）
            heap = [(0, s)]
            while heap:
                d, u = heapq.heappop(heap)
                if d != dist[u]:
                    continue
                for i, e in enumerate(self.g[u]):
                    if e.cap <= 0:
                        continue
                    nd = d + e.cost + potential[u] - potential[e.to]
                    if nd < dist[e.to]:
                        dist[e.to] = nd
                        prev_v[e.to] = u
                        prev_e[e.to] = i
                        heapq.heappush(heap, (nd, e.to))
            if prev_v[t] < 0:
                break
            for v in range(n):
                if dist[v] < INF:
                    potential[v] += dist[v]
            add = required - flow
            v = t
            while v != s:
                add = min(add, self.g[prev_v[v]][prev_e[v]].cap)
                v = prev_v[v]
            v = t
            while v != s:
                e = self.g[prev_v[v]][prev_e[v]]
                e.cap -= add
                self.g[v][e.rev].cap += add
                cost += add * e.cost
                v = prev_v[v]
            flow += add
        if flow != required:
            raise SolverError("最小费用流未能满足审计边际（网络内部无解）")
        return flow, cost

    def _initial_potentials(self, s: int) -> List[int]:
        """Bellman-Ford 求最短距离作为初始势能（图小，节点数 O(组x渠道x日期)）。

        图中允许负费用边但无负圈（初始正向网络是 DAG，残余负边均为其反向边）。
        """
        n = self.n
        INF = 10**30
        dist = [INF] * n
        dist[s] = 0
        for _ in range(n - 1):
            updated = False
            for u in range(n):
                if dist[u] == INF:
                    continue
                for e in self.g[u]:
                    if e.cap > 0 and dist[u] + e.cost < dist[e.to]:
                        dist[e.to] = dist[u] + e.cost
                        updated = True
            if not updated:
                break
        return [0 if d == INF else d for d in dist]


@dataclass
class Problem:
    groups: List[str]
    channels: List[str]
    dates: List[str]
    reach: Dict[Tuple[str, str, str], int]
    log_clicks: Dict[Tuple[str, str, str], int]
    channel_audit: Dict[str, int]
    date_audit: Dict[str, int]

    @property
    def caps(self) -> Dict[Tuple[str, str, str], int]:
        return {k: min(self.reach[k], self.log_clicks[k]) for k in self.reach}

    def group_reach(self, g: str) -> int:
        return sum(self.reach[(g, c, d)] for c in self.channels for d in self.dates)


@dataclass
class Conflict:
    kind: str
    message: str
    details: dict = field(default_factory=dict)


@dataclass
class BoundResult:
    fraction: Fraction
    decimal: float
    allocation: Dict[str, List[List[int]]]  # group -> 行=渠道 列=日期
    group_clicks: Dict[str, int]
    group_ctr: Dict[str, Fraction]


@dataclass
class SolveResult:
    feasible: bool
    lower: Optional[BoundResult] = None
    upper: Optional[BoundResult] = None
    conflict: Optional[Conflict] = None
    totals: dict = field(default_factory=dict)


def build_problem(
    cells: Sequence[CellInput],
    channel_audit: Dict[str, int],
    date_audit: Dict[str, int],
) -> Problem:
    """严格校验并构造 Problem。三类数据的 (组,渠道,日期) 空间必须对齐。"""
    if not cells:
        raise SolverError("触达/日志点击明细为空")
    groups_seen: List[str] = []
    channels_seen: List[str] = []
    dates_seen: List[str] = []
    reach: Dict[Tuple[str, str, str], int] = {}
    logs: Dict[Tuple[str, str, str], int] = {}
    for cell in cells:
        if cell.group not in GROUPS:
            raise SolverError(f"组名必须是 {GROUPS} 之一，收到 {cell.group!r}")
        if not isinstance(cell.reach, int) or not isinstance(cell.log_clicks, int):
            raise SolverError("触达数与日志点击数必须为整数")
        if cell.reach < 0 or cell.log_clicks < 0:
            raise SolverError("触达数与日志点击数不能为负")
        key = (cell.group, cell.channel, cell.date)
        if key in reach:
            raise SolverError(f"明细重复：{key}")
        reach[key] = cell.reach
        logs[key] = cell.log_clicks
        for seq, val in ((groups_seen, cell.group), (channels_seen, cell.channel),
                         (dates_seen, cell.date)):
            if val not in seq:
                seq.append(val)
    for g in GROUPS:
        if g not in groups_seen:
            raise SolverError(f"缺少组 {g} 的明细")
    channels, dates = channels_seen, dates_seen
    # 笛卡尔积必须齐全（缺格无法判断该格的两套计数）
    for g in GROUPS:
        for c in channels:
            for d in dates:
                if (g, c, d) not in reach:
                    raise SolverError(f"缺少明细格：组={g} 渠道={c} 日期={d}")
    ca, da = dict(channel_audit), dict(date_audit)
    if set(ca) != set(channels):
        raise SolverError("渠道审计的渠道集合与明细不一致")
    if set(da) != set(dates):
        raise SolverError("日期审计的日期集合与明细不一致")
    for name, vals in (("渠道审计", ca), ("日期审计", da)):
        for k, v in vals.items():
            if not isinstance(v, int) or v < 0:
                raise SolverError(f"{name}必须为非负整数，{k} 收到 {v!r}")
    return Problem(list(GROUPS), channels, dates, reach, logs, ca, da)


def _check_totals(problem: Problem) -> Optional[Conflict]:
    sum_c = sum(problem.channel_audit.values())
    sum_d = sum(problem.date_audit.values())
    if sum_c != sum_d:
        return Conflict(
            "audit_total_mismatch",
            f"审计总数冲突：渠道有效点击合计 {sum_c} ≠ 日期有效点击合计 {sum_d}",
            {"channel_total": sum_c, "date_total": sum_d},
        )
    total_cap = sum(problem.caps.values())
    if sum_c > total_cap:
        return Conflict(
            "capacity_overflow",
            f"审计有效点击 {sum_c} 超过全部格子容量 min(触达,日志) 合计 {total_cap}",
            {"audit_total": sum_c, "capacity_total": total_cap},
        )
    return None


def _feasibility_network(problem: Problem) -> Tuple[_FlowNetwork, int, int, dict, dict]:
    """source -> 渠道 -> 格子(按组) -> 日期 -> sink 的可行性网络。"""
    channels, dates, groups = problem.channels, problem.dates, problem.groups
    # 节点编号
    sid = 0
    cid = {c: 1 + i for i, c in enumerate(channels)}
    cell_id = {}
    nxt = 1 + len(channels)
    for g in groups:
        for c in channels:
            for d in dates:
                cell_id[(g, c, d)] = nxt
                nxt += 1
    did = {d: nxt + i for i, d in enumerate(dates)}
    nxt += len(dates)
    tid = nxt
    net = _FlowNetwork(tid + 1)
    caps = problem.caps
    for c in channels:
        net.add_edge(sid, cid[c], problem.channel_audit[c])
    for d in dates:
        net.add_edge(did[d], tid, problem.date_audit[d])
    for (g, c, d), cap in caps.items():
        u = cell_id[(g, c, d)]
        net.add_edge(cid[c], u, cap)
        net.add_edge(u, did[d], cap)
    return net, sid, tid, cid, did


def check_feasible(problem: Problem) -> Optional[Conflict]:
    """返回 None 表示可行；否则返回带最小割证书的冲突。"""
    conflict = _check_totals(problem)
    if conflict:
        return conflict
    net, s, t, cid, did = _feasibility_network(problem)
    required = sum(problem.channel_audit.values())
    flow = net.max_flow(s, t, limit=required)
    if flow == required:
        return None

    seen = net.reachable_from_source(s)
    cin = [c for c in problem.channels if seen[cid[c]]]
    cout = [c for c in problem.channels if not seen[cid[c]]]
    din = [d for d in problem.dates if seen[did[d]]]
    dout = [d for d in problem.dates if not seen[did[d]]]
    caps = problem.caps
    cap_cross = sum(
        caps[(g, c, d)]
        for g in problem.groups for c in cin for d in dout
    )
    audit_cin = sum(problem.channel_audit[c] for c in cin)
    audit_din = sum(problem.date_audit[d] for d in din)
    # 简单单点诊断（便于页面直接提示）
    simple = _simple_diagnosis(problem)
    details = {
        "max_flow": flow,
        "required": required,
        "cut_channels": cin,
        "cut_dates_inside": din,
        "cut_dates_outside": dout,
        "excluded_channels": cout,
        "cut_capacity_cross": cap_cross,
        "audit_inside_channels": audit_cin,
        "audit_inside_dates": audit_din,
        "simple": simple,
    }
    message = (
        f"无解：渠道集合 {cin or '∅'} 的审计有效点击共 {audit_cin}，"
        f"但这些渠道只能流向日期集合 {dout or '∅'} 的格子容量合计仅 {cap_cross}"
        + (f"（日期侧 {din} 已被截在割内，审计容量 {audit_din}）。" if din else "。")
    )
    if simple:
        message += " 直接线索：" + simple["message"]
    return Conflict("cut_certificate", message, details)


def _simple_diagnosis(problem: Problem) -> Optional[dict]:
    caps = problem.caps
    # 单个渠道审计 > 该渠道跨组跨日期容量
    for c in problem.channels:
        cap = sum(caps[(g, c, d)] for g in problem.groups for d in problem.dates)
        if problem.channel_audit[c] > cap:
            return {"message": f"渠道 {c} 审计 {problem.channel_audit[c]} > 其格子容量合计 {cap}",
                    "type": "channel", "key": c, "audit": problem.channel_audit[c], "capacity": cap}
    # 单个日期审计 > 该日期跨组跨渠道容量
    for d in problem.dates:
        cap = sum(caps[(g, c, d)] for g in problem.groups for c in problem.channels)
        if problem.date_audit[d] > cap:
            return {"message": f"日期 {d} 审计 {problem.date_audit[d]} > 其格子容量合计 {cap}",
                    "type": "date", "key": d, "audit": problem.date_audit[d], "capacity": cap}
    # 渠道子集（最小割会给更精确的），这里给逐格超限提示
    for k, cap in caps.items():
        g, c, d = k
        if problem.log_clicks[k] < 0:
            return None
    return None


def _optimize(problem: Problem, maximize: bool) -> BoundResult:
    """固定两套边际下，求点击率差的一个端点及达到它的整数分配。

    四层网络：S -> 渠道审计层 -> (组,渠道)行 -> (组,日期)列 -> 日期审计层 -> T。
    渠道/日期审计边保证两套边际精确成立；行/列容量边与逐格费用边保证
    0 <= x_gcd <= cap_gcd。费用 scale*sign/R_g 为整数，最小费用流给出整数最优。
    """
    groups = problem.groups
    reach_totals = {g: problem.group_reach(g) for g in groups}
    for g, r in reach_totals.items():
        if r == 0:
            raise SolverError(f"组 {g} 触达总数为 0，点击率无定义")

    # SSP 求最小费用。单位费用取 D=CTR_cand-CTR_ctrl 的系数时，
    # 最小费用 = scale*D（即最小化 D）；系数整体取反则最小费用 = -scale*D
    # （即最大化 D）。
    scale = 1
    for r in reach_totals.values():
        scale = scale * r // _gcd(scale, r)
    signs = {CANDIDATE: 1 if not maximize else -1,
             CONTROL: -1 if not maximize else 1}
    unit_cost = {g: scale * signs[g] // reach_totals[g] for g in groups}
    required = sum(problem.channel_audit.values())
    return _optimize_layered(problem, maximize, scale, unit_cost, required)


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


def _optimize_layered(problem: Problem, maximize: bool, scale: int,
                      unit_cost: Dict[str, int], required: int) -> BoundResult:
    """四层网络：S -> 渠道审计层 -> (组,渠道) -> (组,日期) -> 日期审计层 -> T。"""
    groups, channels, dates = problem.groups, problem.channels, problem.dates
    caps = problem.caps

    sid = 0
    ch_id = {c: 1 + i for i, c in enumerate(channels)}
    nxt = 1 + len(channels)
    gc_id = {(g, c): nxt + i for i, (g, c) in
             enumerate((g, c) for g in groups for c in channels)}
    nxt += len(gc_id)
    gd_id = {(g, d): nxt + i for i, (g, d) in
             enumerate((g, d) for g in groups for d in dates)}
    nxt += len(gd_id)
    da_id = {d: nxt + i for i, d in enumerate(dates)}
    nxt += len(dates)
    tid = nxt
    net = _FlowNetwork(tid + 1)

    cell_edges = {}
    for c in channels:
        net.add_edge(sid, ch_id[c], problem.channel_audit[c])
    for d in dates:
        net.add_edge(da_id[d], tid, problem.date_audit[d])
    for g in groups:
        for c in channels:
            # 组内渠道行容量 = 该组该渠道各格容量之和
            row_cap = sum(caps[(g, c, d)] for d in dates)
            net.add_edge(ch_id[c], gc_id[(g, c)], row_cap)
        for d in dates:
            col_cap = sum(caps[(g, c, d)] for c in channels)
            net.add_edge(gd_id[(g, d)], da_id[d], col_cap)
        for c in channels:
            for d in dates:
                u, v = gc_id[(g, c)], gd_id[(g, d)]
                idx = len(net.g[u])
                net.add_edge(u, v, caps[(g, c, d)], unit_cost[g])
                cell_edges[(g, c, d)] = (u, idx)

    _, total_cost = net.min_cost_flow(sid, tid, required)

    allocation = {}
    group_clicks = {g: 0 for g in groups}
    for g in groups:
        mat = [[0] * len(dates) for _ in channels]
        for ci, c in enumerate(channels):
            for di, d in enumerate(dates):
                u, idx = cell_edges[(g, c, d)]
                e = net.g[u][idx]
                flow = caps[(g, c, d)] - e.cap
                mat[ci][di] = flow
                group_clicks[g] += flow
        allocation[g] = mat

    reach_totals = {g: problem.group_reach(g) for g in groups}
    group_ctr = {g: Fraction(group_clicks[g], reach_totals[g]) for g in groups}
    diff = group_ctr[CANDIDATE] - group_ctr[CONTROL]
    # 精确校验：最小化时 total_cost/scale = D；最大化时 = -D
    if maximize:
        expected = -diff
    else:
        expected = diff
    if Fraction(total_cost, scale) != expected:
        raise SolverError(
            f"内部错误：费用口径不一致 {Fraction(total_cost, scale)} != {expected}"
        )
    return BoundResult(
        fraction=diff,
        decimal=float(diff),
        allocation=allocation,
        group_clicks=group_clicks,
        group_ctr=group_ctr,
    )


def solve(problem: Problem) -> SolveResult:
    conflict = check_feasible(problem)
    if conflict is not None:
        return SolveResult(
            feasible=False,
            conflict=conflict,
            totals={
                "channel_total": sum(problem.channel_audit.values()),
                "date_total": sum(problem.date_audit.values()),
                "capacity_total": sum(problem.caps.values()),
            },
        )
    lower = _optimize(problem, maximize=False)
    upper = _optimize(problem, maximize=True)
    if lower.fraction > upper.fraction:
        raise SolverError("内部错误：下界超过上界")
    return SolveResult(
        feasible=True,
        lower=lower,
        upper=upper,
        totals={
            "audit_total": sum(problem.channel_audit.values()),
            "capacity_total": sum(problem.caps.values()),
            "group_reach": {g: problem.group_reach(g) for g in problem.groups},
        },
    )
