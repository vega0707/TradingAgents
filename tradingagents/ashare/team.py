"""team.py — 仿 TradingAgents 的分层流程（方案 A 垂直切片）。

  大师分析师层（10 位，并行） → 多空研究员辩论 → 研究员(Research Manager)
  → 交易员(Trader) 拍板。

本切片先于 LangGraph 接线：每一层输出结构化 JSON（存盘可复盘），角色与轮数
与 TA 图一致（analysts -> bull/bear researchers -> research manager ->
trader）。跑通并认可质量后，再把这套节点迁入 graph 以获得 checkpoint 等能力。
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from tradingagents.ashare.llmio import LLMCallError, complete, complete_json
from tradingagents.ashare.material import Material

logger = logging.getLogger(__name__)

PERSONAS = Path(__file__).with_name("personas.json")
MASTER_ORDER = ["graham", "buffett", "munger", "burry", "templeton",
                "neff", "ackman", "marks", "dalio", "lynch"]
DEBATE_ROUNDS = int(os.environ.get("ASHARE_DEBATE_ROUNDS", "2"))  # 每方发言次数

_CONTRACT_MASTER = (
    "\n\n输出要求：只输出一个 JSON 对象，字段："
    '{"signal": "bullish|neutral|bearish", "conviction": 0-100,'
    ' "thesis": "中文 2-4 句核心观点", "risks": "中文 1-3 句风险或反面论点"}。'
    "signal 只表达你对基本面的方向判断（三选一，方向不明才选 neutral 并说明缺什么）。"
    "铁律：只能引用材料里出现的数据；材料没有的数字（市值、目标价、同行数据）一律不编。"
    "JSON 必须是单行：所有字段值禁止换行/引号/反斜杠，用顿号分隔短句。"
)

_BULL_SYS = (
    "你是研究团队的多方首席研究员（Bull Researcher）。大师们的观点材料已附上，"
    "你的任务不是重复他们，而是把支持做多的论据组织成最强的完整案例："
    "分清哪些论点是站得住的、哪些是情绪或幻想；补出大师没说但材料支持的多方逻辑。"
    "之后你会看到空方论点并反击。全程中文，只许引用材料内的数据。"
)
_BEAR_SYS = (
    "你是研究团队的空方首席研究员（Bear Researcher）。大师们的观点材料已附上，"
    "你的任务是把支持做空/回避的论据组织成最强的完整案例：找出多头叙事里的漏洞、"
    "被忽略的风险与估值陷阱。全程中文，只许引用材料内的数据。"
)
_MANAGER_SYS = (
    "你是研究经理（Research Manager）。你已看到大师观点与多空双方完整辩论记录。"
    "你的职责是裁决这场辩论并产出可执行的投资计划，而不是和稀泥："
    "明确指出多空各自最强的一点、双方共识、以及最终倾向（含置信度）。"
    "输出 JSON 字段：{\"recommendation\": \"买入|增持|持有|减持|卖出|不评级\","
    ' "confidence": 0-100, "rationale": "中文裁决理由 3-6 句",'
    ' "plan": "中文 2-4 句：仓位/加仓或退出节奏", "watchlist": "中文 1-3 句：'
    '需跟踪验证的关键变量（财报/价格位/政策…）"}。只引用材料内数据。'
    "JSON 必须是单行：字段值禁止换行/引号/反斜杠，用顿号分隔短句。"
)
_TRADER_SYS = (
    "你是交易员（Trader），研究已结束，由你拍板执行方案。输入包含研究经理的投资计划、"
    "多空辩论、大师观点、行情与你的持仓。你的职责是把方向翻译成具体操作："
    "结合现价与成本决定动作、给出分批/价位/止损与仓位的可执行话术。"
    "铁律：不得编造行情数字；价格一律用材料中的现价与持仓成本说话。"
    "输出 JSON 字段：{\"action\": \"加仓|持有|减仓|清仓|建仓|观望|止损\","
    ' "reasoning": "中文 2-5 句", "levels": "中文 1-3 句：关键价位/分批区间",'
    ' "stop_loss": "中文 1-2 句止损策略，无法设定就说无法设定",'
    ' "position_note": "中文 1-2 句仓位与节奏"}'
    "JSON 必须是单行：字段值禁止换行/引号/反斜杠，用顿号分隔短句。"
)


@dataclass
class TeamRecord:
    views: list[dict]                 # 每大师一条 {master, signal, conviction, thesis, risks, price_view}
    debate: list[dict]                # [{side, round, text}]
    manager: dict
    trader: dict
    failures: list[str]               # 弃权/失败的大师与原因


def _views_text(views: list[dict]) -> str:
    parts = []
    for v in views:
        parts.append(
            f"[{v['master']}] 信号={v['signal']} 置信={v['conviction']}\n"
            f"  论点：{v['thesis']}\n  风险：{v['risks']}"
        )
    return "\n".join(parts)


def run_masters(mat: Material) -> list[dict]:
    """10 位大师并行分析。

    大师只看基本面（与生产决策卡同口径，保证可比）；价格与技术面交给辩论、
    研究员、交易员逐层把关，避免"高位"把所有人的方向判断拉成中性。
    """
    personas = json.loads(PERSONAS.read_text(encoding="utf-8"))
    fundamentals = (f"标的：{mat.name} {mat.ticker} ｜ 分析截至 {mat.as_of}（收盘）\n\n"
                    + mat.fundamentals)
    views: list[dict] = []

    def one(name: str) -> dict:
        try:
            parsed = complete_json(
                "quick", personas[name] + _CONTRACT_MASTER, fundamentals, max_tokens=2200)
            parsed["master"] = name
            parsed.setdefault("signal", "neutral")
            parsed.setdefault("conviction", 0)
            parsed.setdefault("thesis", "")
            parsed.setdefault("risks", "")
            return parsed
        except LLMCallError as exc:
            return {"master": name, "signal": "neutral", "conviction": 0,
                    "thesis": f"[调用失败] {exc}", "risks": ""}

    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = {pool.submit(one, name): name for name in MASTER_ORDER}
        for fut in as_completed(futs):
            views.append(fut.result())
    # 保持固定出场顺序，复盘更稳
    views.sort(key=lambda v: MASTER_ORDER.index(v["master"]))
    return views


def run_debate(mat: Material, views: list[dict], rounds: int = DEBATE_ROUNDS) -> list[dict]:
    """多空研究员交替发言 rounds 轮（每轮双方各一条）。"""
    material = mat.render()
    views_block = ("===== 大师观点 =====" + "\n" + _views_text(views)) if views else (
        "（无大师观点：本标的为 ETF/无基本面，辩论仅基于行情与技术面。）")
    record: list[dict] = []
    bull = ""  # 上一轮多方全文
    bear = ""  # 上一轮空方全文

    for r in range(1, rounds + 1):
        bull_prompt = (
            f"{material}\n\n{views_block}\n\n"
            f"===== 第 {r} 轮 ====="
            + (f"\n空方上一轮论点：\n{bear}\n\n你的任务：逐条反驳它最有力的部分，"
               "并重申/加固你的多方案例。" if bear else "\n这是你的开场：构建最强多方案例。")
        )
        b = complete("quick", _BULL_SYS, bull_prompt, max_tokens=1800)
        record.append({"side": "bull", "round": r, "text": b})
        bull = b

        bear_prompt = (
            f"{material}\n\n{views_block}\n\n===== 第 {r} 轮 ====="
            f"\n多方本轮论点：\n{bull}\n\n你的任务：攻击多方论点中最脆弱处，"
            "并给出空方完整案例（含估值/基本面/技术面反面证据，只引用材料）。"
        )
        k = complete("quick", _BEAR_SYS, bear_prompt, max_tokens=1800)
        record.append({"side": "bear", "round": r, "text": k})
        bear = k

    return record


def _debate_text(record: list[dict]) -> str:
    return "\n".join(f"【{d['side']}·第{d['round']}轮】\n{d['text']}" for d in record)


def run_manager(mat: Material, views: list[dict], debate: list[dict],
                note: str = "") -> dict:
    material = mat.render()
    note_block = ("\n\n" + note) if note else ""
    user = (
        f"{material}\n\n{_views_text(views)}\n\n===== 多空辩论记录 =====\n"
        f"{_debate_text(debate)}{note_block}\n\n请裁决。"
    )
    parsed = complete_json("deep", _MANAGER_SYS, user, max_tokens=2400)
    parsed.setdefault("recommendation", "不评级")
    return parsed


def _holdings_text(shares: float | None, cost: float | None, mark: float | None) -> str:
    if not shares or not cost or not mark:
        return "未持仓（或未提供持仓），按新建仓视角决策。"
    pnl = (mark / cost - 1) * 100
    return (f"持仓 {int(shares)} 股，成本 {cost:.2f}，现价 {mark:.2f}"
            f"，浮动 {pnl:+.1f}%。")


def run_trader(mat: Material, views: list[dict], debate: list[dict], manager: dict,
               shares: float | None = None, cost: float | None = None,
               note: str = "") -> dict:
    material = mat.render()
    note_block = ("\n\n" + note) if note else ""
    user = (
        f"{material}\n\n{_views_text(views)}\n\n===== 多空辩论记录 =====\n"
        f"{_debate_text(debate)}\n\n===== 研究经理裁决 =====\n"
        f"{json.dumps(manager, ensure_ascii=False)}\n\n===== 你的持仓 =====\n"
        f"{_holdings_text(shares, cost, mat.mark)}{note_block}\n\n请拍板。"
    )
    parsed = complete_json("quick", _TRADER_SYS, user, max_tokens=2000)
    parsed.setdefault("action", "观望")
    return parsed


def run_team(mat: Material, shares: float | None = None, cost: float | None = None,
             rounds: int = DEBATE_ROUNDS, scoreboard_note: str = "") -> TeamRecord:
    """完整跑一支股票的分析团队。任一层整体失败 -> 抛错（不产出半张卡）。

    ETF/指数：无个股基本面，跳过 10 位大师（避免逼他们在没有基本面材料时
    表态编造），辩手与研究员、交易员仅基于行情/技术面。

    scoreboard_note：阶段3 战绩注入（只给 manager/trader，大师自身不看）。
    """
    if mat.is_etf:
        views: list[dict] = []
    else:
        views = run_masters(mat)
    debate = run_debate(mat, views, rounds=rounds)
    manager = run_manager(mat, views, debate, note=scoreboard_note)
    trader = run_trader(mat, views, debate, manager, shares=shares, cost=cost,
                        note=scoreboard_note)
    failures = [v["master"] for v in views
                if str(v["thesis"]).startswith("[调用失败]")]
    return TeamRecord(views=views, debate=debate, manager=manager,
                      trader=trader, failures=failures)
