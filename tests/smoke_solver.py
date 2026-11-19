"""手工可核对的小算例，验证求解器端点与约束。"""
import sys
from fractions import Fraction

sys.path.insert(0, "/workspace")
from clickaudit.solver import CellInput, build_problem, solve  # noqa: E402

# 2 组 x 2 渠道 x 2 日期；每格 reach=100, log=100 => cap=100
cells = []
for g in ("candidate", "control"):
    for c in ("C1", "C2"):
        for d in ("D1", "D2"):
            cells.append(CellInput(g, c, d, 100, 100))

# 渠道各 100，日期各 100，总 200
ca = {"C1": 100, "C2": 100}
da = {"D1": 100, "D2": 100}
p = build_problem(cells, ca, da)
res = solve(p)
assert res.feasible, res.conflict
# 候选触达 400、对照触达 400；点击和固定 200
# 极端：候选 0 / 对照 200 => 差 -0.5；候选 200 / 对照 0 => +0.5
assert res.lower.fraction == Fraction(-1, 2), res.lower.fraction
assert res.upper.fraction == Fraction(1, 2), res.upper.fraction
print("smoke1 bounds:", res.lower.fraction, res.upper.fraction)

# 端点分配必须满足全部约束
def verify(bound, p):
    caps = p.caps
    ch_sum = {c: 0 for c in p.channels}
    da_sum = {d: 0 for d in p.dates}
    g_sum = {g: 0 for g in p.groups}
    for g in p.groups:
        for ci, c in enumerate(p.channels):
            for di, d in enumerate(p.dates):
                x = bound.allocation[g][ci][di]
                assert 0 <= x <= caps[(g, c, d)], (g, c, d, x)
                ch_sum[c] += x
                da_sum[d] += x
                g_sum[g] += x
    assert ch_sum == p.channel_audit
    assert da_sum == p.date_audit
    assert sum(g_sum.values()) == sum(ca.values())

verify(res.lower, p)
verify(res.upper, p)

# 日志小于触达：cap 取 min；某格 cap=0 时流量必须绕开
cells2 = []
for g in ("candidate", "control"):
    for c in ("C1", "C2"):
        for d in ("D1", "D2"):
            log = 0 if (c == "C1" and d == "D1") else 60
            cells2.append(CellInput(g, c, d, 100, log))
p2 = build_problem(cells2, ca, da)
res2 = solve(p2)
assert res2.feasible, res2.conflict
assert res2.lower.allocation["candidate"][0][0] == 0
assert res2.lower.allocation["control"][0][0] == 0
verify(res2.lower, p2)
verify(res2.upper, p2)
print("smoke2 bounds:", res2.lower.fraction, res2.upper.fraction)

# 总数相等但无解：渠道审计 100/100，日期审计 200/0；
# D2 审计为 0 => 全部流量走 D1；C2 在 D1 的两格 cap 各 60 共 120，够 100；
# 而 C1 在 D1 两格 cap=0，无法承载 C1 的 100 => 无解
da_bad = {"D1": 200, "D2": 0}
p3 = build_problem(cells2, ca, da_bad)
res3 = solve(p3)
assert not res3.feasible
assert res3.conflict.kind == "cut_certificate"
print("smoke3 conflict:", res3.conflict.message)
print("SMOKE OK")
