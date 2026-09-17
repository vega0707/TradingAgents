#!/usr/bin/env python3
"""bt_regime.py — 宏观状态（regime）与长周期收益检验

用户质疑（2026-09-17）：bt_policy 只检验了"宽松事件"这种短脉冲，但宏观的
影响是整体的、长周期的——真正驱动市场的是**宏观状态组合**（增长 × 流动性 ×
价格），不是单点事件。

本脚本把 2008-2026 的每个月标记进四象限（regime），检验：
    不同 regime 下，未来 6 / 12 个月的沪深300 收益分布是否系统性不同？
如果有，说明"识别 regime → 匹配策略"成立（用户的观点对）；
如果没有，说明宏观状态在 A 股没有长期定价力（那也是一种结论）。

Regime 定义（全部用事前可计算的规则，不用事后叙事）：
  增长轴   = PMI 的近 6 个月均值 − 前 6 个月均值（上行/下行）
  流动性轴 = M2 同比的近 6 个月均值 − 前 6 个月均值（宽松中/收紧中）
  （用趋势而非单点，避免月度噪音；12 个月窗口滚动，保证标记时点已知过去）

样本：PMI 2008-01 起；M2 2008-02 起 → 有效样本约 2005-2026 中重叠部分
收益：沪深300 月线（腾讯接口）

用法: PYTHONPATH=. .venv/bin/python scripts/bt_regime.py
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradingagents.ashare.proxy import ensure_tx_tunnel, tx_env  # noqa: E402

if not ensure_tx_tunnel():
    print("[warn] 腾讯云隧道不可用——M2 序列将缺失")
os.environ.update(tx_env())

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
CACHE = Path("ashare_out") / "_cache" / "bt_regime.json"


def _get_json(url: str, headers: dict, tries: int = 3) -> dict:
    """直连抓取：临时清代理（urllib 不认 socks5h），退避重试。"""
    keys = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")
    saved = {k: os.environ.get(k) for k in keys}
    last = None
    for i in range(tries):
        try:
            for k in keys:
                os.environ.pop(k, None)
            return json.loads(urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=20).read().decode())
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i < tries - 1:
                time.sleep(3 * (i + 1))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    raise last  # type: ignore[misc]


def index_monthly() -> dict[str, float]:
    d = _get_json("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                  "?param=sh000300,month,,,320,qfq", {"User-Agent": "Mozilla/5.0"})
    out = {}
    for r in ((d.get("data") or {}).get("sh000300") or {}).get("month") or []:
        if len(r) >= 3:
            out[r[0][:7]] = float(r[2])
    return out


def pmi_series() -> dict[str, float]:
    d = _get_json("https://datacenter-web.eastmoney.com/api/data/v1/get"
                  "?reportName=RPT_ECONOMY_PMI&columns=ALL&pageSize=300"
                  "&sortColumns=REPORT_DATE&sortTypes=-1", UA)
    out = {}
    for r in (d.get("result") or {}).get("data") or []:
        v = r.get("MAKE_INDEX")
        if isinstance(v, (int, float)):
            out[r["REPORT_DATE"][:7]] = float(v)
    return out


def m2_series() -> dict[str, float]:
    """M2 同比（akshare/金十，月度）。走隧道。"""
    import akshare as ak
    df = ak.macro_china_money_supply()
    out = {}
    import re
    for _, r in df.iterrows():
        m = re.match(r"(\d{4})年(\d{2})月份", str(r.get("月份")))
        v = r.get("货币和准货币(M2)-同比增长")
        if m and isinstance(v, (int, float)):
            out[f"{m.group(1)}-{m.group(2)}"] = float(v)
    return out


def rolling_dir(series: dict[str, float], months: list[str], window: int = 6) -> dict[str, int]:
    """方向 = 近 window 月均值 − 前 window 月均值。1=上行，-1=下行，0=未知。"""
    out = {}
    vals = [series.get(m) for m in months]
    for i, ym in enumerate(months):
        if i < window:
            out[ym] = 0
            continue
        cur = [v for v in vals[i - window + 1: i + 1] if v is not None]
        prev = [v for v in vals[i - 2 * window + 1: i - window + 1] if v is not None]
        if len(cur) < window - 1 or len(prev) < window - 1:
            out[ym] = 0
            continue
        out[ym] = 1 if statistics.mean(cur) > statistics.mean(prev) else -1
    return out


def fwd(px: dict[str, float], months: list[str], k: int) -> dict[str, float]:
    out = {}
    for i, ym in enumerate(months):
        if i + k < len(months):
            out[ym] = (px[months[i + k]] / px[ym] - 1) * 100
    return out


def stat_line(name: str, xs: list[float], horizon: str) -> None:
    if len(xs) < 3:
        print(f"  {name:<24} 样本不足({len(xs)})")
        return
    win = sum(1 for x in xs if x > 0) / len(xs) * 100
    print(f"  {name:<24} 均值 {statistics.mean(xs):+7.2f}%  中位 {statistics.median(xs):+7.2f}%  "
          f"胜率 {win:5.1f}%  样本 {len(xs)}  [{horizon}]")


def main() -> None:
    if CACHE.is_file():
        try:
            blob = json.loads(CACHE.read_text())
            px, pmi, m2 = blob["px"], blob["pmi"], blob["m2"]
            print(f"(缓存 {blob.get('saved')})")
        except Exception:
            px = None
    else:
        px = None
    if px is None:
        px = index_monthly()
        pmi = pmi_series()
        m2 = m2_series()
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({"saved": time.strftime("%F %T"),
                                     "px": px, "pmi": pmi, "m2": m2}, ensure_ascii=False))

    months = sorted(set(px) & set(pmi) & set(m2))
    g_dir = rolling_dir(pmi, months)
    l_dir = rolling_dir(m2, months)
    r6 = fwd(px, months, 6)
    r12 = fwd(px, months, 12)

    names = {
        (1, 1): "增长↑+流动性↑（双击）",
        (-1, 1): "增长↓+流动性↑（政策对冲）",
        (1, -1): "增长↑+流动性↓（过热+收紧）",
        (-1, -1): "增长↓+流动性↓（双杀）",
        (0, 1): "流动性↑(增长未明)", (0, -1): "流动性↓(增长未明)",
        (1, 0): "增长↑(流动性未明)", (-1, 0): "增长↓(流动性未明)",
    }
    buckets = {}
    for ym in months:
        key = (g_dir.get(ym, 0), l_dir.get(ym, 0))
        if ym in r6:
            buckets.setdefault((key, 6), []).append(r6[ym])
        if ym in r12:
            buckets.setdefault((key, 12), []).append(r12[ym])

    print(f"# 宏观 regime 与长周期收益（{months[0]} ~ {months[-1]}，{len(months)} 个月）")
    print(f"# regime 用 6 个月均值趋势标记（事前可计算），非事后叙事\n")
    for horizon in (6, 12):
        print(f"### 未来 {horizon} 个月")
        order = [(1, 1), (-1, 1), (0, 1), (1, 0), (-1, 0), (0, -1), (1, -1), (-1, -1)]
        for key in order:
            xs = buckets.get((key, horizon)) or []
            stat_line(names.get(key, str(key)), xs, f"{horizon}月")
        print()


if __name__ == "__main__":
    main()
