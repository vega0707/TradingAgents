#!/usr/bin/env python3
"""bt_policy.py — "政策市"共振检验：PMI 收缩 × 宽松政策 = 机会窗口？

由来：2026-09-17 的宏观预测力回测（bt_macro.py）发现，制造业 PMI<50（经济差）
之后 3 个月沪深300 反而平均 +4.79%（t=-2.72 显著）——与"经济差该防守"的直觉
相反，指向 A 股的政策市特征：数据差 → 宽松预期 → 股市提前反应。

本脚本把这条线索拆开验证：如果"政策市"成立，那么
    PMI 收缩 且 3 个月内有宽松动作（降准/降息）
的组合应该显著好于
    PMI 收缩 但没有宽松动作
——如果两者没有差别，说明"政策市"只是叙事。

数据：
  宽松事件 = 降准（准备金率调整幅度<0，akshare 金十源，2007 起 58 次）
           + 降息（LPR1Y/5Y 环比下降，akshare 东财源，2019-08 起）
             —— 2015-2019 的基准利率降息不在样本内（诚实标注，样本偏保守）
  PMI = 制造业 PMI（东财 datacenter，2008-01 起）
  收益 = 沪深300 月线（东财）

用法: PYTHONPATH=. .venv/bin/python scripts/bt_policy.py
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# akshare 部分走腾讯云隧道（公司网络直连不通，见 ashare/proxy.py）
from tradingagents.ashare.proxy import ensure_tx_tunnel, tx_env  # noqa: E402

if not ensure_tx_tunnel():
    print("[warn] 腾讯云隧道不可用——政策事件序列将缺失")
os.environ.update(tx_env())

import re  # noqa: E402
import time  # noqa: E402
import urllib.request  # noqa: E402
from contextlib import contextmanager  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
QUOTE_UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
CACHE = Path("ashare_out") / "_cache" / "bt_policy.json"


@contextmanager
def direct_env():
    """临时清除代理环境变量（urllib 不认 socks5h，模块级设的 tx 代理会把东财
    直连请求带崩 —— bt_zt_nextday 踩过同一个坑）。"""
    keys = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
            "https_proxy", "http_proxy", "all_proxy")
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _get(url: str, headers: dict, tries: int = 3) -> dict:
    last = None
    for i in range(tries):
        try:
            with direct_env():
                return json.loads(urllib.request.urlopen(
                    urllib.request.Request(url, headers=headers), timeout=20).read().decode())
        except Exception as exc:  # noqa: BLE001 — 东财间歇断连，退避重试
            last = exc
            if i < tries - 1:
                time.sleep(3 * (i + 1))
    raise last  # type: ignore[misc]


def index_monthly() -> dict[str, float]:
    """沪深300 月线收盘（腾讯接口——东财 push2his 断连频繁，腾讯一直最稳）。"""
    d = _get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000300,month,,,320,qfq",
             {"User-Agent": "Mozilla/5.0"})
    out = {}
    rows = ((d.get("data") or {}).get("sh000300") or {}).get("month") or []
    for r in rows:
        if len(r) >= 3:
            out[r[0][:7]] = float(r[2])
    return out


def pmi_series() -> dict[str, float]:
    d = _get("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_ECONOMY_PMI"
             "&columns=ALL&pageSize=300&sortColumns=REPORT_DATE&sortTypes=-1", UA)
    out = {}
    for r in (d.get("result") or {}).get("data") or []:
        v = r.get("MAKE_INDEX")
        if isinstance(v, (int, float)):
            out[r["REPORT_DATE"][:7]] = float(v)
    return out


def easing_months() -> set[str]:
    """宽松事件月集合（降准公布月 + LPR 下调月）。"""
    import akshare as ak
    out: set[str] = set()
    # 降准：调整幅度<0（大型金融机构）→ 公布月
    rr = ak.macro_china_reserve_requirement_ratio()
    for _, r in rr.iterrows():
        try:
            amp = float(r.get("大型金融机构-调整幅度"))
        except (TypeError, ValueError):
            continue
        if amp < 0:
            m = re.match(r"(\d{4})年(\d{2})月", str(r.get("公布时间")))
            if m:
                out.add(f"{m.group(1)}-{m.group(2)}")
    # 降息：LPR 环比下降（月度取值）
    lpr = ak.macro_china_lpr()
    rows = lpr.sort_values("TRADE_DATE")
    prev = None
    for _, r in rows.iterrows():
        ym = str(r.get("TRADE_DATE"))[:7]
        cur = r.get("LPR1Y")
        if prev is not None and isinstance(cur, (int, float)) and cur < prev:
            out.add(ym)
        prev = cur if isinstance(cur, (int, float)) else prev
    return out


def add_months(ym: str, k: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    m += k
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def had_easing(ym: str, events: set[str], window: int = 3) -> bool:
    return any(add_months(ym, -k) in events for k in range(window))


def fwd_returns(px: dict[str, float], months: list[str]) -> tuple[dict, dict]:
    r1, r3 = {}, {}
    for i, ym in enumerate(months):
        if i + 1 < len(months):
            r1[ym] = (px[months[i + 1]] / px[ym] - 1) * 100
        if i + 3 < len(months):
            r3[ym] = (px[months[i + 3]] / px[ym] - 1) * 100
    return r1, r3


def t_stat(a, b):
    if len(a) < 5 or len(b) < 5:
        return None
    se = (statistics.variance(a) / len(a) + statistics.variance(b) / len(b)) ** 0.5
    return (statistics.mean(a) - statistics.mean(b)) / se if se else None


def report(name: str, xs: list[float], horizon: str) -> None:
    if not xs:
        print(f"  {name:<22} 样本 0")
        return
    win = sum(1 for x in xs if x > 0) / len(xs) * 100
    print(f"  {name:<22} 均值 {statistics.mean(xs):+6.2f}%  胜率 {win:5.1f}%  样本 {len(xs)}  [{horizon}]")


def main() -> None:
    if CACHE.is_file():
        try:
            blob = json.loads(CACHE.read_text())
            px, pmi, events = blob["px"], blob["pmi"], set(blob["events"])
            print(f"(缓存 {blob.get('saved')})")
        except Exception:
            px = pmi = events = None
    else:
        px = pmi = events = None
    if px is None:
        px = index_monthly()
        pmi = pmi_series()
        events = easing_months()
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({"saved": __import__("time").strftime("%F %T"),
                                     "px": px, "pmi": pmi, "events": sorted(events)},
                                    ensure_ascii=False))
    months = sorted(set(px) & set(pmi))
    r1, r3 = fwd_returns(px, months)
    print(f"# 政策市共振检验（{months[0]} ~ {months[-1]}，{len(months)} 个月）")
    print(f"# 宽松事件月：{len(events)} 个（降准 2007 起 + LPR 下调 2019-08 起；2015-2019 基准利率降息不在样本）")

    groups = {"A 共振(PMI<50+宽松)": [], "B 经济差无宽松": [],
              "C 顺周期宽松": [], "D 景气无宽松": []}
    g3: dict[str, list[float]] = {k: [] for k in groups}
    for ym in months:
        weak = pmi[ym] < 50
        ease = had_easing(ym, events, window=3)
        if weak and ease:
            k = "A 共振(PMI<50+宽松)"
        elif weak:
            k = "B 经济差无宽松"
        elif ease:
            k = "C 顺周期宽松"
        else:
            k = "D 景气无宽松"
        if ym in r1:
            groups[k].append(r1[ym])
        if ym in r3:
            g3[k].append(r3[ym])

    for horizon, g in (("未来 1 月", groups), ("未来 3 月", g3)):
        print(f"\n### {horizon}")
        for k in ("A 共振(PMI<50+宽松)", "B 经济差无宽松", "C 顺周期宽松", "D 景气无宽松"):
            report(k, g[k], horizon)
        t = t_stat(g["A 共振(PMI<50+宽松)"], g["B 经济差无宽松"])
        if t is not None:
            print(f"  → A vs B 均值差 {statistics.mean(g['A 共振(PMI<50+宽松)']) - statistics.mean(g['B 经济差无宽松']):+.2f}%  t={t:.2f}"
                  + ("（显著）" if abs(t) >= 2 else "（不显著）"))


if __name__ == "__main__":
    main()
