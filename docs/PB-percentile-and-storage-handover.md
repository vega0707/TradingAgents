# PB 分位 + 本地数据落库 — 交接文档（2026-09-09）

> 给云上 cursor 继续实施用。原则：**最小化外部接口依赖，拉到数据即本地落一份**
> （SQLite 长期主存储 + JSON 文件缓存过渡），不要再设计成"每次现拉"。

## 一、任务目标

给估值锚补第二个维度：**PB 历史分位**（当前市净率在自身历史区间的位置）。
与已有"合理价"（ROE-PB 模型，理论值）互补：
- 合理价低估 + PB 分位低 = 强机会信号
- 合理价低估 + PB 分位高 = 警惕价值陷阱（市场在高位必有原因）
- 参考案例：汉缆股份现价 PB 2.63，历史 5 年区间 0.8~2.6 → 分位 ~97%（题材价）

## 二、本地存储现状（已评估，容量无忧）

| 项 | 值 |
|---|---|
| 磁盘余量 | 149 GB（空间不是约束） |
| ashare_out/ | 6.0 MB（`_cache/` 4.4MB / 156 个 JSON 文件） |
| 单只 520 根日K JSON | ~57 KB |
| **关注池 ~40 只 × 2500 根** | ~6 MB（JSON）/ ~3 MB（SQLite）——完全可行 |

**真正的瓶颈 = 外部接口请求量**，不是存储：
- 全市场 5500 只 × 2500 根 ≈ 1375 万根 → 不现实，**别做全量**
- 范围限定：持仓(13) + 观察池(5) + 候选池(~20) ≈ **40 只** → 拉取与存储都轻松

## 三、落库设计（2026-09-09 选型：Turso edge SQLite）

**选型结论**：用 **Turso**（用户 steel 项目已注册，cfg.properties 有连接配置）。
理由：免费 9GB（Neon/Supabase 0.5GB 的 18 倍，唯一能撑全市场 1250 万行）、
SQLite 语义与现有代码/DDL 无缝、10 亿行读/月配额充足。备选 Supabase（Postgres，
有网页看板，500MB 关注池够）。**弃**：Mongo(文档型)、Upstash(Redis)、Aiven(额度小)、
本地 SQLite(云上 cursor 无法共享)、D2(公开行情数据不需公司库)。

```
Turso 库（libSQL / edge SQLite，多端共享）

表 kline(code TEXT, date TEXT, open REAL, high REAL, low REAL,
         close REAL, volume REAL, PRIMARY KEY(code, date))
   — 长历史日K，关注池按需灌入，增量更新（每日只补缺失日期）

表 bvps_history(code TEXT, report_period TEXT, bvps REAL, filing_date TEXT)
   — PIT 每股净资产序列（来源：akshare 快照已含，落地防重复拉）

表 valuation_snapshot(code TEXT, as_of TEXT, cur_pb REAL, pb_pct REAL, ...)
   — 每日估值结果快照（历史可回溯、可审计）
```

- 连接：Turso URL + token（cfg.properties `turso` 段），Python 用
  `libsql-experimental` / SQLAlchemy+`sqlalchemy-turso` 驱动；云上 cursor 同库
- 写入链路：**本机外部接口拉取（新浪/腾讯）→ 写 Turso**；不存本地 JSON 主副本
  （`_cache/` 现有当日缓存降级为进程内/当日层，Turso 为持久真相）
- 数据量：关注池 40 只 × 2500 根 ≈ 10 万行 ≈ 5MB——Turso 免费层毫无压力

### Turso 配置获取（2026-09-09 状态：仅 OAuth 记录，无实际连接串）

steel `cfg.properties` 只存了登录方式 `turso github:vega0707 (OAuth)`，
**库 URL/token 未保存**，需登录一次获取（本机 turso CLI 未装）：

```bash
# 1. 安装并登录（GitHub OAuth 网页授权，账号 vega0707）
brew install tursodatabase/tap/turso   # 或 curl -sSfL https://get.turso.tech/install.sh | bash
turso auth login

# 2. 建库 + 拿连接信息
turso db create ashare                     # 库名建议 ashare
turso db show ashare --url                  # → libsql://ashare-<org>.turso.io
turso db tokens create ashare              # → 生成 token（保存好，只显示一次）

# 3. 写入本地配置（gitignored）
#    /Users/vega/git/TradingAgents/.env 增加：
#    TURSO_URL=libsql://ashare-<org>.turso.io
#    TURSO_TOKEN=<token>
```

- **安全**：token 能读写该库——**勿提交 git**（TradingAgents 若 public）。
  Turso 控制台可随时 revoke 重建 token（行情库无个人数据，泄露可接受但应 revoke）
- 云上 cursor：用同一 `TURSO_URL/TURSO_TOKEN`（或从其 .env 读），多端同库
- 拿到连接串后建议回填 steel `cfg.properties`（`turso.dburl`/`turso.token`），
  保持资源登记完整

## 四、数据源与限流经验（重要）

| 源 | 用途 | 状态 |
|---|---|---|
| 新浪日K `quotes.sina.cn` | 现主力（material.fetch_daily_kline） | 稳定；**datalen 上限需验证**（现用 ≤520，PB 5 年需 ~1250 根） |
| 腾讯 `web.ifzq.gtimg.cn/appstock/app/fqkline/get` | 备选长历史（支持指定起止日） | 需**验证 start/end 参数**能否拿 5 年历史（格式 `param=sh600519,day,2021-01-01,2026-09-09,640,qfq`） |
| 东财 push2/datacenter | 行情/财务 | **本机被封/限流中**（2026-09-09），勿做主源 |
| akshare 财务（datacenter 底层） | 快照 ROE/BVPS | 间歇限流——21 天缓存已缓解；**避免并发冷拉**（>2 路即触发） |
| 新浪财务页（GBK） | valuation fallback | 稳定；`sina_metrics` 抓列需小心（曾抓到非年报口径 ROE 3.55% vs 真 6.2%） |

**限流铁律**（全部实测踩坑）：
1. 新浪请求间隔 ≥1.2s；东财彻底不用
2. akshare 财务串行（并发 >2 必挂）；失败指数退避（client 已实现 2/4/8/16s×5）
3. 每只票每天只拉一次（当日文件缓存 + SQLite 增量）

## 五、PB 分位算法（PIT 注意）

```
对目标票：
  1. 取长历史日K（优先腾讯 5 年，或新浪 datalen 上限）
  2. PB 序列 = 每日 close / 当日已披露 BVPS（PIT：BVPS 用 report_period 的
     filing_date ≤ 当日 的最新值——防未来函数）
     ※ 简化版可用"当前 BVPS ÷ 历史价"近似，标注误差，后续升级 PIT
  3. 当前 PB 在历史序列的分位 = 历史 PB < 当前 PB 的天数占比
  4. 输出：当前 PB、分位%、历史区间(PB 5%~95%)、结论（低位<20%/高位>80%）
```

## 六、实施步骤（给 cursor）

0. **准备 Turso**：
   - 本机装 turso CLI → `turso auth login`（GitHub OAuth，账号 vega0707）
   - `turso db create ashare` → `db show --url` + `db tokens create` 拿连接串
   - 写 `.env`：`TURSO_URL`/`TURSO_TOKEN`（gitignored；勿提交，见上节安全提示）
   - `pip install libsql-experimental`（或 sqlalchemy-turso）；测试多端连接（本机/云上）
1. **验证数据源长历史能力**（各 1 次请求，不批量）：
   - 腾讯 fqkline 指定起止日能否返回 2021 至今（~1250 根）
   - 新浪 datalen=1250 是否被截断
   - 选可用者作为长历史源，写入 `tradingagents/ashare/material.py` 新函数 `fetch_long_kline(code, years=5)`
2. **建 SQLite**：`data/market.db` + 上述表结构；迁移函数把现有 `_cache/kline-*.json` 灌入（40 只内，一次性）
3. **实现 PB 分位**：`valuation.py` 加 `pb_percentile(code, as_of)`（先简化版：当前 BVPS×历史价），fair_value 输出加 `pb_pct`（模型已有此字段占位，此前因无数据恒 None）
4. **增量更新**：每日 12:00 流程尾部补 `kline` 缺失日期（关注池 40 只 × 每次 1 根 ≈ 秒级 40 请求/日，可接受；或仅 3 只/日轮换降频）
5. **验证**：汉缆 002498 分位应 ≈95%+（历史高位）；平安/格力分位应低位；与合理价方向一致
6. **估值节接入**：portfolio render 估值锚行加"分位 xx%"

## 七、相关代码索引

| 文件 | 职责 | 关键函数 |
|---|---|---|
| `tradingagents/ashare/material.py` | 行情/快照缓存 | `fetch_daily_kline`, `_cache_load/_save`, `build_material`（21天财务缓存） |
| `tradingagents/ashare/valuation.py` | 合理价估值锚 | `fair_value`, `sina_metrics`, `asset_r`, `_get_snapshot`（val-snap 缓存） |
| `tradingagents/ashare/portfolio.py` | 决策单 | `render`（估值锚节）, `build_portfolio`, `returns_by_date` |
| `tradingagents/ashare/watchlist_check.py` | 观察池触发 | dip/ma20 触发 + 状态去重 |
| `tradingagents/ashare/moneyflow.py` | 资金流检测 | `fund_flow`, `summarize`（新浪，5/10日累计） |
| `tradingagents/ashare/daily_brief.py` | 盘前推荐 | 候选深析（预热+并行+去重+可执行卡） |
| `tradingagents/ashare/daily_flow.py` | 增量编排 | `decide`（触发深析） |
| `tradingagents/ashare/data/akshare_client.py` | 财务源 | `_fetch`（指数退避）|
| `run_local_ashare.sh` | 12:00 每日流程 | 决策单推送（有动作才推） |

## 八、已知待办/风险

- 腾讯长历史接口参数未验证（本交接前未拉数据）
- 简化 PB 分位用当前 BVPS 近似，PIT 精确版后续
- akshare 财务本机限流可能持续数小时——全部走新浪/缓存兜底
- 关注池扩容时先评估请求量（40→100 只每日增量仍可接受，全市场不可行）

## 九、环境

- 项目：`/Users/vega/git/TradingAgents`，分支 `feat/ashare-a-share-closed-loop`
- LLM：.env ADA coding-plan（key 见 keychain `程小帮/ada-codex-auto`）；或程小帮 18787 代理（需 Chat key）
- 服务器：ubuntu@124.221.95.205（tickers.yaml 权威源，每日 12:00 从服务器同步）
- 飞书推送：服务器 openclaw → oc_07d9d5b6be512982a85b450cc85f57dd
