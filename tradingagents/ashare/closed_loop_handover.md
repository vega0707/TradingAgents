# 交接文档：A 股研究流水线「自验证 → 自迭代」闭环（v1 范围）

> 读者：下一轮迭代的开发方（agent / 工程师）。按本文实现 v1 增量即可，方向与防坑纪律见文末。
> 落点：本仓库 `tradingagents/ashare/` 目录内（该目录为本地改造命名空间，尚未跟踪，改完自行提交）。

---

## 0. 方向与阶段模型（先读）

现状问题一句话：**10 位大师 → 多空辩论 → 研究经理 → 交易员**每天都在产出单票决策，但所有观点只存档、从不验证，没有人知道"谁准谁不准"，更谈不上改进。

目标是一个可自我演进的闭环，分四个阶段：

| 阶段 | 内容 | 说明 |
|---|---|---|
| 1 记录 | 每条决策落盘时带上「可对账锚点」（决策日、当时价格、方向、强度、基准） | **无前视**是铁律 |
| 2 对账 | 到期自动拉真实行情，算方向命中率 / IC / 分层收益 / 相对基准超额 | 只读，不反写决策 |
| 3 记分牌注入 | 把"每位大师近期战绩"注入后续决策上下文 | 模型参考，样本不足不采信 |
| 4 自动迭代 | 按战绩调大师聚合权重 / 人审批的自适应（改 persona、diff 可回滚） | 高风险，**本次不做** |

**本次 v1 范围 = 阶段 1 + 2 + 3（轻量注入）。阶段 4 一律不做，见 §5。**

---

## 1. 现状盘点（已核实的代码事实，别改这些语义）

决策链路与产出现状（均已实读确认）：

- 单票入口：`tradingagents/ashare/run.py` — `python -m tradingagents.ashare.run --ticker 601318 --name 中国平安 --shares 200 --cost 35.98 --date 2026-09-04`；`--no-save` 不落盘。
- 落盘目录：`ashare_out/<ticker>-<as_of>/`（相对 cwd，as_of 为 YYYY-MM-DD），产出 5 个文件：
  `material.md`、`views.json`、`debate.md`、`decision.json`（=`{"manager": …, "trader": …}`）、`card.md`。
- 结构化字段（复用，别改名）：
  - views（每大师一条）：`{master, signal(bullish|neutral|bearish|abstain), conviction 0-100, thesis, risks, price_view}`（`team.py` run_masters）
  - manager：`{recommendation(买入|增持|持有|减持|卖出|不评级), confidence 0-100, rationale, plan, watchlist}`（`team.py` _MANAGER_SYS）
  - trader：`{action(加仓|持有|减仓|清仓|建仓|观望|止损), reasoning, levels, stop_loss, position_note}`（`team.py` _TRADER_SYS）
- 材料包 `material.py`：行情用新浪日 K（不复权，只取 ≤ as_of，防未来泄漏）；基本面跨仓复用 ai-hedge-fund 的 PIT 快照（`AkshareDataClient` + `build_snapshot`，披露日过滤）。`Material` 有 `ticker/name/as_of/is_etf/mark/fundamentals_ok`。ETF/基本面缺失 → 大师可 abstain，交易员仍出动作。
- LLM 出入口 `llmio.py`：quick/deep 两档模型；JSON 解析失败一次重试后抛 `LLMCallError`。

当前缺口（本次要补的）：
1. 决策 json 里**没有**决策时刻的行情锚点（现价、成本、份额、基准），只散落在 material.md / card.md 里 —— 不可结构化对账。
2. 没有任何"到期拉真实行情对比"的 job。
3. 无记分牌、无战绩注入。
4. 同一 ticker + 同一 as_of 重跑会**覆盖**旧产出 —— 样本会被污染（LLM 输出非确定性）。

---

## 2. v1 增量 A —— 记录层：`record.json`（本次必做，最优先）

目标：每条决策生成一份与输出解耦的、机器可对账的记录，只写决策时刻可得的信息。

### 2.1 新增文件

`ashare_out/<ticker>-<as_of>/record.json`，由 `run.py` 在 save 分支统一写出（与 decision.json 同批），**原子写完**（先写临时文件再 rename）。

最小 schema（`version: 1`）：

```json
{
  "version": 1,
  "ticker": "601318",
  "name": "中国平安",
  "as_of": "2026-09-04",
  "decided_on": "2026-09-04",
  "is_etf": false,
  "fundamentals_ok": true,
  "mark": 55.20,
  "cost": 35.98,
  "shares": 200,
  "benchmark": "000300",
  "benchmark_mark": 3421.3,
  "horizons": [5, 20],
  "masters": [
    {"master": "graham", "signal": "bullish", "conviction": 70}
  ],
  "manager": {"recommendation": "增持", "confidence": 65},
  "trader": {"action": "加仓"},
  "failures": ["abstain 或调用失败的大师名单"],
  "llm_tier": {"quick": "…", "deep": "…"}
}
```

要点与纪律：
- **只允许决策时刻写**：`mark` 必须 = as_of 当日收盘（即 `material.mark`）；不得含任何 as_of 之后的信息。对账时由 reconcile 拉未来价格，record 里永不回填。
- `benchmark_mark`：沪深 300 在 as_of 的收盘。取不到时允许为 `null`（reconcile 侧降级为只出绝对指标），但**实现上要尝试取**（新浪日 K 端点取 `sh000300`，与 `material.py` 同一 URL 模板；index 代码可能返回结构不同，需实跑验证，失败就 null + 日志）。
- `horizons` 常量 [5, 20]（交易日），写死，不提供配置化——防止日后"挑 horizon"自欺。
- **防覆盖**：record.json（连同整个目录）已存在且 `as_of` 相同 → 默认拒绝覆盖并提示 `--force`；`run.py` 需新增 `--force` 显式确认。决策产物非确定性，重复跑同一天会污染样本。
- 方向口径：masters 的 signal 直接可判；manager/trader 的中文 enum 在 §3.2 给映射表。

### 2.2 单元测试

`tradingagents/ashare/test_record.py`：用 fixture 断言 schema 完整性（必填字段齐全、类型正确、`mark/as_of` 一致、字段内不含未来日期）。**测试不得调用 LLM、不得联网。**

---

## 3. v1 增量 B —— 对账 job：`reconcile.py`（只读）

新增 `tradingagents/ashare/reconcile.py`，CLI：

```bash
python -m tradingagents.ashare.reconcile                 # 对账所有已到期样本（默认）
python -m tradingagents.ashare.reconcile --horizon 20    # 只出该 horizon
python -m tradingagents.ashare.reconcile --as-of 2026-09-04  # 只对账该决策日之后到期的
```

### 3.1 核心逻辑（写成纯函数，便于单测）

- 扫描 `ashare_out/*/record.json`，对每条样本：
  - `mature = 数据最新交易日 >= as_of 后第 h 个交易日`，否则跳过（h ∈ horizons）。
  - 取该股日 K（复用 `material.fetch_daily_kline`，**只返回 as_of 当日及之前与之后的价格都取**——对账发生在未来，允许用整段历史；实现时注意把"<= as_of 取锚、> as_of 取 horizon"的分界写清楚并单测）。
  - `ret_h = close(as_of 后第 h 个交易日) / mark - 1`；取不到足够 bar → 标记 `insufficient_data`，不计入统计但计入计数。
  - 基准同窗口收益 → `excess_h = ret_h - bench_ret_h`（benchmark_mark 缺失则 excess 为 null）。

### 3.2 统计口径（写进 scoreboard 说明，避免误读）

方向映射（中文 enum → 多/空/中性）：

| 来源 | 多 | 空 | 中性/不计方向 |
|---|---|---|---|
| master.signal | bullish | bearish | neutral / abstain |
| manager.recommendation | 买入/增持 | 卖出/减持 | 持有 / 不评级 |
| trader.action | 加仓/建仓 | 减仓/清仓/止损 | 持有/观望 |

输出指标（每个维度 × 每个 horizon）：
- **方向命中率**：多方向且 `ret_h > 0` 或空方向且 `ret_h < 0` 计命中；命中数/有方向样本数。abstain、ETF、LLM 输出解析失败（标 `invalid`，不猜测）单独计数、不进命中率。
- **强度 IC**：`conviction`（master）或 `confidence`（manager）与 `ret_h` 的秩相关（Spearman）。**注意：本仓库 pyproject 未声明 scipy**，用 pandas `rank()` + 相关系数手算实现，零新增依赖。样本 < 10 不输出 IC。
- **分层**：按 signal（bullish/neutral/bearish）分组的 `ret_h` 与 `excess_h` 均值——验证"看多组真的跑赢看空组"。
- trader 动作的口径提醒：已持仓者的"减仓/清仓"正确 ≠ 股价下跌（可能是止盈/换仓），**v1 如实按上表统计并注明口径**，持仓语境精算留到后续迭代，不阻塞。

### 3.3 输出

写入 `ashare_out/_scoreboard/scoreboard.json` 与 `scoreboard.md`（覆盖式，另存历史一份带时间戳 `scoreboard-YYYYMMDD.json`）：
- 每位 master / manager 分 horizon 的：样本数、方向命中率、IC、均值超额；样本不足 10 的只列样本数。
- 大盘与全队汇总行。
- `scoreboard.md` 头部注明：数据截至日、各指标口径一句话、样本门槛说明。

只读铁律：**reconcile 不修改任何决策文件、不覆盖 record.json**。

### 3.4 单元测试

`tradingagents/ashare/test_reconcile.py`：用**合成日 K 与固定 fixture record** 验证——方向命中、Spearman IC、分层均值的数值正确性（不联网）；覆盖 abstain / invalid / 样本不足 / 基准缺失降级四条路径。

---

## 4. v1 增量 C —— 战绩注入（阶段 3 轻量版）

`team.py` / `run.py` 接入：跑新决策时，若 `ashare_out/_scoreboard/scoreboard.json` 存在，把每位大师（样本 ≥ 10 的）近 N=20 次战绩压缩成一段中文附在 material 之后，仅给 manager 与 trader 看（大师自身 prompt 不动——那属于阶段 4）：

```
[近期战绩参考（样本≥10 才列出；不足者标注"样本积累中，不据此采信"）]
graham   方向命中率 18/25（72%） ｜ 5日超额均值 +1.2% ｜ 样本 25
```

纪律：注入段必须是**客观数字转述**，禁止模型自行解读出"该信谁"的指令性文字；样本 < 10 一律不出现数字。`--no-scoreboard` 可关。

---

## 5. 验收标准（本轮迭代 agent 的自检清单）

1. `pytest`（或仓库现有测试入口）全绿：新增 `test_record.py`、`test_reconcile.py` 不联网不调 LLM。
2. 真实跑通一次决策（需 .env / 网关可达，建议在腾讯云主机或本机具备 key 处执行）：
   `python -m tradingagents.ashare.run --ticker 601318 --name 中国平安 --shares 200 --cost 35.98 --date <最近交易日>`
   → 断言 `ashare_out/601318-<date>/record.json` 存在、字段齐、`mark` == 当日收盘、无未来字段。
3. 重跑同一天不带 `--force` → 被拒绝覆盖（提示信息明确）。
4. 用 2~3 条历史 record + 合成 price fixture 跑 reconcile → scoreboard.json/.md 生成，数值与手算一致，且任何决策文件 mtime 未变。
5. 确认只新增/修改了 `tradingagents/ashare/` 内文件（及新测试），未动上游 `tradingagents/` 其它模块；`run.py`/`team.py` 改动为增量式。

## 6. 防坑纪律（一切实现必须遵守）

1. **无前视**：record 只含 as_of 及之前可得信息；reconcile 拉未来价格只用于对账，永不回写。
2. **horizon 固定**：5/20 交易日常量；不准按结果事后换 horizon 呈现。
3. **独立基准**：excess 相对沪深 300；不可只用绝对涨跌（单边行情会自欺）。
4. **样本门槛**：< 10 个到期样本不出 IC、不进战绩注入。
5. **方向不计 abstain/invalid**：单独计数，不猜测。
6. **防覆盖**：同 ticker 同 as_of 重复跑默认拒绝（见 §2.1）。
7. **阶段 4 不做**：自动改大师 prompt / persona、按 IC 自动改聚合权重，一律留给后续迭代，且届时必须"人审批 + git diff 可回滚"。
8. 本机为 Intel Mac + Python 3.12，新增依赖须满足 x86_64 wheel 可用；**优先零新增依赖**——本仓库 pyproject 只声明 pandas 而无 numpy/scipy（numpy 系传递依赖），统计量一律用 pandas 手写（rank/Spearman/分组均值），不要为对账 job 引 scipy。

## 7. 后续迭代（不在本次，留档）

- 批量入口：把 18 只持仓清单循环收进仓库（`run_holdings.py`），并对齐腾讯云每日调度；单票对账先跑通。
- trader 动作的"持仓语境"精算（减仓正确性 ≠ 股价方向）。
- 阶段 4-1：按 IC/命中率调大师聚合权重（现 team.py 里大师观点是简单计数汇总，见 `run.py` build_card）。
- 阶段 4-2：人审批的自适应（LLM 提案修订 persona → git diff 供人审 → 回滚位）。
- regime 标签（牛/熊/震荡快照随 record 落盘，供分形态统计）。
- 与 ai-hedge-fund ROADMAP 同构目标对齐：其 validation gate（CPCV/PBO）与 auto-promotion（human-approved）即为本闭环的工程化远期形态；本闭环产生的对账数据可直接作为其 gate 的输入。

## 8. 一句话回扣

判断来自外部基准与固定 horizon 的客观对账，不来自系统自我评价；迭代节奏由样本量约束，不由"想改进"的冲动驱动。
