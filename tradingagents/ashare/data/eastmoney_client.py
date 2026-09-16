#!/usr/bin/env python3
"""eastmoney_client.py — A 股数据客户端（东财直连，不依赖 akshare）

2026-09-16 替换 AkshareDataClient。原因：akshare 走的那些东财节点长期不可用
（连续多日 RemoteDisconnected：9/11、9/14、9/15、9/16 都出现），而东财官方
接口在 datacenter-web / 82.push2 等域名下稳定可用——换域名就通，说明不是
我们被封，是 akshare 用的固定节点不通。直连东财即可去掉这层不稳定依赖。

实现 DataClient 协议中本仓真正使用的两个方法：
  get_financial_metrics  主要财务指标（20 期，含 point-in-time 披露日过滤）
  get_company_facts      公司基本信息（名称/行业）
其余协议方法本仓不用，显式抛 NotImplementedError——协议要求"基础设施失败
必须抛错"，静默返回空会把回测毒化（缺数据和没信号无法区分）。

节流：单票 1 次请求，串行；失败退避重试 3 次（3/10/25s）。
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

from tradingagents.ashare.data.models import CompanyFacts, FinancialMetrics

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://emweb.securities.eastmoney.com/"}
DC = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName="
      "RPT_F10_FINANCE_MAINFINADATA&columns=ALL&filter=(SECUCODE%3D%22{secu}%22)"
      "&pageSize=20&sortColumns=REPORT_DATE&sortTypes=-1")
QUOTE_HOSTS = ("82.push2.eastmoney.com", "push2delay.eastmoney.com", "push2.eastmoney.com")
QUOTE_PATH = "/api/qt/ulist.np/get?secids={secid}&fields=f12,f14,f100,f20"
BACKOFF = (3.0, 10.0, 25.0)


def _secu(ticker: str) -> str:
    """6 位代码 → 东财 SECUCODE（600xxx.SH / 000xxx.SZ / 8xx·4xx 北交所）。"""
    if ticker[0] == "6":
        return f"{ticker}.SH"
    if ticker[0] in ("8", "4"):
        return f"{ticker}.BJ"
    return f"{ticker}.SZ"


def _secid(ticker: str) -> str:
    """6 位代码 → 行情 secid（1=沪 / 0=深·北）。"""
    return f"{1 if ticker[0] == '6' else 0}.{ticker}"


def _get(url: str, tries: int = 3) -> dict:
    last: Exception | None = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
        except Exception as exc:  # noqa: BLE001 — 间歇断连，退避重试（跨窗口）
            last = exc
            if i < tries - 1:
                time.sleep(BACKOFF[min(i, len(BACKOFF) - 1)])
    raise last  # type: ignore[misc]


def _ratio(v) -> float | None:
    """东财百分比字段（8.68 表示 8.68%）→ 小数 0.0868。"""
    return round(v / 100, 6) if isinstance(v, (int, float)) else None


def _de_from_ratio(v) -> float | None:
    """资产负债率(%) → 产权比率 D/E = r/(1-r)。"""
    if not isinstance(v, (int, float)) or not (0 < v < 100):
        return None
    r = v / 100
    return round(r / (1 - r), 4)


class EastMoneyClient:
    """东财直连数据客户端（满足 DataClient 协议的两个必需方法）。"""

    def __init__(self) -> None:
        self._hist_cache: dict[str, list[dict]] = {}

    # ---- 数据源 ----

    def _history(self, ticker: str) -> list[dict]:
        """20 期主要财务指标（1 次请求，进程内缓存）。"""
        if ticker not in self._hist_cache:
            d = _get(DC.format(secu=urllib.parse.quote(_secu(ticker))))
            rows = (d.get("result") or {}).get("data") or []
            rows.sort(key=lambda r: r["REPORT_DATE"], reverse=True)
            self._hist_cache[ticker] = rows
        return self._hist_cache[ticker]

    # ---- DataClient 协议 ----

    def get_financial_metrics(
        self, ticker: str, end_date: str, period: str = "ttm", limit: int = 10,
    ) -> list[FinancialMetrics]:
        """截至 end_date **已披露**的主要财务指标（point-in-time）。

        PIT 过滤用 NOTICE_DATE（公告日）而非 REPORT_DATE：报告期结束不等于
        数据公开，用报告期过滤会引入前视偏差。
        """
        rows = [r for r in self._history(ticker)
                if (r.get("NOTICE_DATE") or "")[:10] and (r["NOTICE_DATE"] or "")[:10] <= end_date]
        out: list[FinancialMetrics] = []
        for r in rows[:limit]:
            out.append(FinancialMetrics(
                ticker=ticker,
                report_period=r["REPORT_DATE"][:10],
                period=period,
                filing_date=(r.get("NOTICE_DATE") or "")[:10] or None,
                return_on_equity=_ratio(r.get("ROEJQ")),
                earnings_per_share=r.get("EPSJB"),
                book_value_per_share=r.get("BPS"),
                gross_margin=_ratio(r.get("XSMLL")),
                net_margin=_ratio(r.get("XSJLL")),
                debt_to_equity=_de_from_ratio(r.get("ZCFZL")),
                revenue_growth=_ratio(r.get("TOTALOPERATEREVETZ")),
                free_cash_flow_per_share=r.get("MGJYXJJE"),
            ))
        return out

    def get_company_facts(self, ticker: str) -> CompanyFacts | None:
        """公司基本信息（名称/行业）。取不到返回 None——build_snapshot 已容错。"""
        for host in QUOTE_HOSTS:
            try:
                d = _get(f"https://{host}{QUOTE_PATH.format(secid=_secid(ticker))}", tries=1)
                rows = (d.get("data") or {}).get("diff") or []
                if rows:
                    r = rows[0]
                    return CompanyFacts(ticker=ticker, name=r.get("f14"),
                                        industry=r.get("f100"), is_active=True)
            except Exception:  # noqa: BLE001 — 换下一个行情节点
                continue
        return None

    # ---- 本仓未使用的协议方法：显式失败，不静默返回空 ----

    def _unused(self, name: str):
        raise NotImplementedError(
            f"{name} 未实现：本仓只使用 get_financial_metrics / get_company_facts。"
            "价格走 material.fetch_daily_kline（新浪），新闻走新浪 7x24。")

    def get_prices(self, *a, **k):
        return self._unused("get_prices")

    def get_news(self, *a, **k):
        return self._unused("get_news")

    def get_insider_trades(self, *a, **k):
        return self._unused("get_insider_trades")

    def get_earnings(self, *a, **k):
        return self._unused("get_earnings")

    def get_earnings_history(self, *a, **k):
        return self._unused("get_earnings_history")

    def get_market_cap(self, *a, **k):
        return self._unused("get_market_cap")


if __name__ == "__main__":   # 冒烟测试：python -m tradingagents.ashare.data.eastmoney_client 000651
    import sys as _sys
    c = EastMoneyClient()
    code = (_sys.argv[1] if len(_sys.argv) > 1 else "000651")
    ms = c.get_financial_metrics(code, "2026-09-16", limit=5)
    print(f"{code}: {len(ms)} 期")
    for m in ms:
        print(f"  {m.report_period} (披露 {m.filing_date}) ROE={m.return_on_equity} "
              f"EPS={m.earnings_per_share} BPS={m.book_value_per_share} "
              f"毛利率={m.gross_margin} D/E={m.debt_to_equity}")
    print("facts:", c.get_company_facts(code))
