#!/usr/bin/env python3
"""bt_macro.py — 宏观信号预测力检验（证伪导向，不是参数优化）

为什么写它：2026-09-17 建了"宏观大势"三层评分（全球流动性 / 国内基本面 /
市场结构），但评分权重是我拍的。用户追问"这件事本身对不对"。在让它碰仓位
之前，必须先回答一个更基础的问题：

    这些宏观信号到底能不能预测 A 股？

方法（刻意保持粗糙，避免过拟合）：
  · 数据：沪深300 月线（东财，2006 起）+ PMI/PPI/CPI 月度序列（东财）
  · 对齐：PMI 当月月末公布 → 信号月=数据月；CPI/PPI 次月公布 → 信号月=数据月+1
    （保守处理，杜绝前视偏差）
  · 检验：每个单因子分组后看"未来 1 个月 / 3 个月"沪深300 收益的均值差、
    胜率、样本数、t 值
  · 不做：不挑阈值、不选最优组合、不做样本内优化

如果某个维度连"单因子预测力"都没有，那它就不该进评分体系（更不该碰仓位）。

用法: .venv/bin/python scripts/bt_macro.py
"""
from __future__ import annotations

import json
import statistics
import urllib.request
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
QUOTE_UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
OUT = Path("ashare_out") / "_bt"


def _get(url: str, headers: dict, tries: int = 3) -> dict:
    last = None
    for _ in range(tries):
        try:
            return json.loads(urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=20).read().decode())
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise last  # type: ignore[misc]


def index_monthly() -> dict[str, float]:
    """沪深300 月线收盘 → {YYYY-MM: close}。"""
    u = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.000300"
         "&fields1=f1,f2&fields2=f51,f52,f53,f54,f55,f56,f57&klt=103&fqt=0"
         "&beg=20050101&end=20500101&lmt=300")
    d = _get(u, QUOTE_UA)
    out = {}
    for line in (d.get("data") or {}).get("klines") or []:
        f = line.split(",")
        out[f[0][:7]] = float(f[2])     # 收盘
    return out


def macro_series(report: str, field: str) -> dict[str, float]:
    """宏观月度序列 → {YYYY-MM: value}。"""
    d = _get(f"https://datacenter-web.eastmoney.com/api/data/v1/get?reportName={report}"
             f"&columns=ALL&pageSize=300&sortColumns=REPORT_DATE&sortTypes=-1", UA)
    out = {}
    for r in (d.get("result") or {}).get("data") or []:
        v = r.get(field)
        if isinstance(v, (int, float)):
            out[r["REPORT_DATE"][:7]] = float(v)
    return out


def add_months(ym: str, k: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    m += k
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def t_stat(a: list[float], b: list[float]) -> float | None:
    """两组均值差的 t 值（Welch）。样本太少返回 None。"""
    if len(a) < 5 or len(b) < 5:
        return None
    va, vb = statistics.variance(a), statistics.variance(b)
    se = (va / len(a) + vb / len(b)) ** 0.5
    return (statistics.mean(a) - statistics.mean(b)) / se if se else None


def evaluate(name: str, signal: dict[str, float], rets: dict[str, float],
             split) -> None:
    """按 split 把信号分两组，比较后续收益。"""
    groups: dict[str, list[float]] = {}
    for ym, v in signal.items():
        fwd = rets.get(ym)
        if fwd is None:
            continue
        groups.setdefault(split(v), []).append(fwd)
    print(f"\n### {name}")
    stat = {}
    for g, xs in sorted(groups.items()):
        if not xs:
            continue
        win = sum(1 for x in xs if x > 0) / len(xs) * 100
        stat[g] = (statistics.mean(xs), len(xs), win)
        print(f"  {g:<16} 均值 {statistics.mean(xs):+6.2f}%  胜率 {win:5.1f}%  样本 {len(xs)}")
    keys = list(stat)
    if len(keys) == 2:
        a, b = groups[keys[0]], groups[keys[1]]
        t = t_stat(a, b)
        diff = statistics.mean(a) - statistics.mean(b)
        verdict = "无显著差异" if (t is None or abs(t) < 2) else "有差异"
        print(f"  均值差 {diff:+.2f}%  t={t if t is None else round(t, 2)} → {verdict}")


def main() -> None:
    px = index_monthly()
    months = sorted(px)
    rets1, rets3 = {}, {}
    for i in range(len(months) - 1):
        rets1[months[i]] = (px[months[i + 1]] / px[months[i]] - 1) * 100
    for i in range(len(months) - 3):
        rets3[months[i]] = (px[months[i + 3]] / px[months[i]] - 1) * 100

    pmi = macro_series("RPT_ECONOMY_PMI", "MAKE_INDEX")
    ppi = macro_series("RPT_ECONOMY_PPI", "BASE_SAME")
    cpi = macro_series("RPT_ECONOMY_CPI", "NATIONAL_SAME")
    # CPI/PPI 次月公布 → 信号月右移一格（防前视）
    ppi_lag = {add_months(k, 1): v for k, v in ppi.items()}
    cpi_lag = {add_months(k, 1): v for k, v in cpi.items()}
    # 市场结构：沪深300 相对 10 月均线（月线口径）
    ma10 = {}
    for i in range(9, len(months)):
        ma10[months[i]] = statistics.mean(px[months[j]] for j in range(i - 9, i + 1))  # 含当月

    print(f"# 宏观信号预测力检验（沪深300，{months[0]} ~ {months[-1]}，{len(months)} 个月）")
    print(f"> 未来 1 个月收益；信号对齐已按公布滞后（CPI/PPI 右移 1 个月）")

    evaluate("① 制造业 PMI（荣枯线 50）", pmi, rets1,
             lambda v: "扩张(≥50)" if v >= 50 else "收缩(<50)")
    evaluate("② PPI 同比（工业品价格）", ppi_lag, rets1,
             lambda v: "涨价(>0)" if v > 0 else "通缩(≤0)")
    evaluate("③ CPI 同比（通胀）", cpi_lag, rets1,
             lambda v: "低(<1%)" if v < 1 else ("温和(1-3%)" if v <= 3 else "高(>3%)"))
    trend = {ym: px[ym] / ma10[ym] for ym in months if ym in ma10}
    evaluate("④ 指数 vs 10月均线（趋势）", trend, rets1,
             lambda v: "均线上方" if v >= 1 else "均线下方")

    print("\n### 同时看未来 3 个月（PMI / PPI）")
    evaluate("PMI → 未来3月", pmi, rets3, lambda v: "扩张(≥50)" if v >= 50 else "收缩(<50)")
    evaluate("PPI → 未来3月", ppi_lag, rets3, lambda v: "涨价(>0)" if v > 0 else "通缩(≤0)")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "bt_macro.txt").write_text("see stdout", encoding="utf-8")
    print("\n> 判读原则：|t| < 2 视为无显著差异；样本 < 30 的结论只能当线索。")


if __name__ == "__main__":
    main()
