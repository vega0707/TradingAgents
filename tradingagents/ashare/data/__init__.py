"""A股基本面数据层（vendor 自 ai-hedge-fund hedge_fund.data，见 closed_loop_handover）。

仅导出本仓需要的符号：EastMoneyClient（东财直连，2026-09-16 替换 akshare——
后者用的东财节点长期不可用，换域名即通）。
原仓的 CachedDataClient / FDClient / make_data_client 等未 vendor，需要时再补。
"""
from tradingagents.ashare.data.eastmoney_client import EastMoneyClient
from tradingagents.ashare.data.models import FinancialMetrics
from tradingagents.ashare.data.protocol import DataClient

__all__ = ["EastMoneyClient", "DataClient", "FinancialMetrics"]
