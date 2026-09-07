"""A股基本面数据层（vendor 自 ai-hedge-fund hedge_fund.data，见 closed_loop_handover）。

仅导出本仓需要的符号：AkshareDataClient（免费 A 股数据源，无需 key）。
原仓的 CachedDataClient / FDClient / make_data_client 等未 vendor，需要时再补。
"""
from tradingagents.ashare.data.akshare_client import AkshareDataClient
from tradingagents.ashare.data.models import FinancialMetrics
from tradingagents.ashare.data.protocol import DataClient

__all__ = ["AkshareDataClient", "DataClient", "FinancialMetrics"]
