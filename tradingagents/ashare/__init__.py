"""TradingAgents A股每日研究切片（方案 A：大师=分析师，辩手/研究员/交易员拍板）。"""

from tradingagents.ashare.llmio import LLMCallError, complete, complete_json
from tradingagents.ashare.material import (
    Material,
    build_material,
    is_etf,
    latest_trade_date,
)
from tradingagents.ashare.team import TeamRecord, run_team

__all__ = [
    "LLMCallError", "Material", "TeamRecord",
    "build_material", "complete", "complete_json", "is_etf",
    "latest_trade_date", "run_team",
]
