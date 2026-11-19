# 点击审计批次台（clickaudit）

日志点击存在误触，审计侧只给**渠道**与**日期**各自的有效点击总数。本系统在
“按 (组, 渠道, 日期) 录入触达与日志点击”的明细上，求真实点击满足两套计数时，
**候选组 − 对照组点击率差的最紧上下界**，并给出能达到两端的整数分配；
无可行解时给出最小割冲突证书。纯 Python 标准库实现，零第三方依赖、零构建步骤。

## 口径与约束

对每个格子 `(g, c, d)`（组 g∈{candidate, control}、渠道 c、日期 d）录入
触达 `reach` 与日志点击 `log_clicks`，真实点击 `x_gcd` 满足：

1. `0 ≤ x_gcd ≤ min(reach_gcd, log_clicks_gcd)` —— 各格不能超过日志，也不能超过触达；
2. `Σ_{g,d} x_gcd = channel_audit[c]` —— 同时符合渠道审计计数；
3. `Σ_{g,c} x_gcd = date_audit[d]` —— 同时符合日期审计计数。

目标量：`D = Σ_cd x_cand / R_cand − Σ_cd x_ctrl / R_ctrl`，
其中 `R_g = Σ_cd reach_gcd`。约束矩阵是二分图节点-关联矩阵（全幺模），
整数容量 ⇒ 线性规划的最优解天然是整数。

## 方法（最紧上下界 = 两次最小费用流）

- 可行性：`S → 渠道层 → 格子层 → 日期层 → T` 的整数最大流；
  流量达不到审计总数即无解，用 **最小割** 输出冲突证书
  （哪些渠道的审计量、被哪些日期的格子容量卡住，跨割容量是多少）。
- 端点：四层网络 `S → 渠道审计 → (组,渠道)行 → (组,日期)列 → 日期审计 → T`，
  逐格边容量 `min(reach, log)`，单位费用取点击率差系数
  `±LCM(R_cand,R_ctrl)/R_g`（整数费用、精确排序，无浮点误差）。
  Successive Shortest Path（Bellman-Ford 初始化势能 + Dijkstra）求最小费用流，
  分别最小化/最大化 D，得到下界、上界及达到端点的整数分配。
- 结果以 `Fraction` 精确分数给出，同时附小数；端点分配回算复核两套边际与费用。

## 批次规则

- **三份数据同批齐全才冻结**：`cells`（触达+日志）、`channel_audit`、`date_audit`
  在同一批次内到齐（顺序任意、支持并发补交）才触发分析；未齐全时批次保持 `open`，
  绝不产出结论。
- 同批次同类型重复提交、冻结后补交一律拒绝（fcntl 文件锁 + 进程内锁串行裁决）。
- **修正另开一批**：修正批次带 `parent_batch_id`，旧批次与冻结结果原样保留。
- **重跑旧批结果一致**：输入三份文件的 SHA256 与冻结记录比对，重算结果与
  `analysis.json` 逐字节校验和比对；输入被外部篡改则拒绝重跑、不覆盖历史。
- 无解批次状态为 `failed`（输入合法但约束冲突，同样冻结留痕）；输入口径本身
  非法（缺格、负值、渠道/日期集合不一致等）则拒绝并保持 `open`。
- **总数相等却无解**会被最小割识别：渠道合计 = 日期合计只是必要条件，
  结构容量不足仍无解，演示批次 B 即此场景（两边合计都是 3000）。

## 目录

```
clickaudit/
  solver.py    # 最小费用流/最大流最小割、问题校验、上下界与端点分配
  batches.py   # 批次生命周期、文件锁、校验和、冻结/重跑/修正、结果序列化
  server.py    # 标准库 http.server 的 REST/JSON + 静态页面
  cli.py       # 命令行入口
  web/         # 原生 ES module 页面（index.html/app.js/styles.css）
demo/          # 演示数据生成（A 可解 / B 总数相等但无解 / C 修正批）
tests/         # 冒烟、完全枚举交叉验证、端到端（含并发与 HTTP）
data/          # 默认批次存储（JSON + runs.log，可由 CLICKAUDIT_DATA 改）
```

## 启动与使用

```bash
# Web 页面 + API（默认 http://127.0.0.1:18080）
PYTHONPATH=. CLICKAUDIT_DATA=./data PORT=18080 python3 -m clickaudit.server

# 生成演示数据
python3 -I demo/make_demo.py

# CLI：建批 → 补交三份 → 查看 → 重跑 → 修正
python3 -I -m clickaudit.cli --data ./data create --note "首批"
python3 -I -m clickaudit.cli --data ./data submit <批次> cells        demo/data/A_cells.json
python3 -I -m clickaudit.cli --data ./data submit <批次> channel_audit demo/data/A_channel.json
python3 -I -m clickaudit.cli --data ./data submit <批次> date_audit    demo/data/A_date.json
python3 -I -m clickaudit.cli --data ./data show   <批次>
python3 -I -m clickaudit.cli --data ./data rerun  <批次>
python3 -I -m clickaudit.cli --data ./data correct <批次>
# 不建批次直接求解三份 JSON
python3 -I -m clickaudit.cli solve-once demo/data/A_cells.json demo/data/A_channel.json demo/data/A_date.json
```

### HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/batches` | 建批 `{"note": "..."}` |
| GET | `/api/batches` | 批次列表 |
| GET | `/api/batches/<id>` | 批次详情（含分析结果与 runs.log） |
| POST | `/api/batches/<id>/submit/<kind>` | kind ∈ cells/channel_audit/date_audit |
| POST | `/api/batches/<id>/rerun` | 重跑并校验一致性 |
| POST | `/api/batches/<id>/correction` | 另开修正批次 |

## 边界与假设

- 组固定为 `candidate`/`control`，组内 (渠道,日期) 笛卡尔积必须齐全；
  点击率分母用触达总数，某组触达为 0 时拒绝分析。
- 单机文件存储，适合小型本地审计协作；跨节点部署需把 `BatchStore` 换成
  带事务的数据库实现（求解器本身无状态、可直接复用）。
- 最小费用流规模为 O(组·渠道·日期) 节点、O(组·渠道·日期) 边，演示规模毫秒级。
