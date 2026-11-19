"""独立交叉验证：小规模算例完全枚举所有可行整数分配，核对最紧上下界。"""
import itertools
import sys
from fractions import Fraction

sys.path.insert(0, "/workspace")
from clickaudit.solver import CellInput, build_problem, solve  # noqa: E402


def brute_force(problem):
    caps = problem.caps
    keys = [(g, c, d) for g in problem.groups
            for c in problem.channels for d in problem.dates]
    ranges = [range(caps[k] + 1) for k in keys]
    diffs = []
    for vals in itertools.product(*ranges):
        x = dict(zip(keys, vals))
        if any(sum(x[(g, c, d)] for g in problem.groups for d in problem.dates)
               != problem.channel_audit[c] for c in problem.channels):
            continue
        if any(sum(x[(g, c, d)] for g in problem.groups for c in problem.channels)
               != problem.date_audit[d] for d in problem.dates):
            continue
        rc = {g: problem.group_reach(g) for g in problem.groups}
        gc = {g: sum(x[(g, c, d)] for c in problem.channels for d in problem.dates)
              for g in problem.groups}
        diffs.append(Fraction(gc["candidate"], rc["candidate"])
                     - Fraction(gc["control"], rc["control"]))
    return (min(diffs), max(diffs), len(diffs)) if diffs else None


def case(reach_log, ca, da):
    cells = []
    for g, table in reach_log.items():
        for ci, c in enumerate(("C1", "C2")):
            for di, d in enumerate(("D1", "D2")):
                reach, log = table[ci][di]
                cells.append(CellInput(g, c, d, reach, log))
    return build_problem(cells, ca, da)


# 小容量算例（枚举空间 <= 5^8 ~ 39 万，可接受）
tables = {
    "candidate": [[(4, 3), (4, 4)], [(3, 2), (5, 3)]],
    "control":   [[(4, 2), (4, 3)], [(3, 3), (5, 4)]],
}
ca = {"C1": 5, "C2": 4}
da = {"D1": 6, "D2": 3}
p = case(tables, ca, da)
res = solve(p)
assert res.feasible
bf = brute_force(p)
assert bf is not None, "枚举应至少有一个可行解"
lo, hi, n = bf
print(f"枚举可行解 {n} 个；暴力区间 [{lo}, {hi}]；"
      f"求解器区间 [{res.lower.fraction}, {res.upper.fraction}]")
assert lo == res.lower.fraction and hi == res.upper.fraction

# 端点分配本身必须是枚举空间中的可行解（已由 test_end_to_end 的边际校验覆盖，
# 这里再验一次端点目标值）
for bound, want in ((res.lower, lo), (res.upper, hi)):
    rc = {g: p.group_reach(g) for g in p.groups}
    got = Fraction(bound.group_clicks["candidate"], rc["candidate"]) \
        - Fraction(bound.group_clicks["control"], rc["control"])
    assert got == want

# 第二个算例：容量更紧、存在多个紧约束
tables2 = {
    "candidate": [[(2, 1), (2, 2)], [(2, 2), (2, 1)]],
    "control":   [[(2, 2), (2, 1)], [(2, 1), (2, 2)]],
}
p2 = case(tables2, {"C1": 3, "C2": 3}, {"D1": 3, "D2": 3})
r2 = solve(p2)
bf2 = brute_force(p2)
print(f"算例2 枚举 {bf2[2]} 个；暴力 [{bf2[0]}, {bf2[1]}]；"
      f"求解器 [{r2.lower.fraction}, {r2.upper.fraction}]")
assert bf2[:2] == (r2.lower.fraction, r2.upper.fraction)

# 无解小算例：枚举同样为空
p3 = case(tables2, {"C1": 6, "C2": 0}, {"D1": 6, "D2": 0})
r3 = solve(p3)
assert not r3.feasible and brute_force(p3) is None
print("算例3：求解器与枚举均判定无解 ✔")
print("交叉验证全部通过 ✔")
