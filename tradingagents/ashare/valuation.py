#!/usr/bin/env python3
"""valuation.py — 合理价估值锚（PB-ROE-Gordon v2）

回答"这票到底值多少、现在贵不贵"：
  合理 PB = (ROE - g) / (r - g)     （ROE=平均净资产收益率, g=每股账面增长,
                                       r=要求回报率, 默认 10%）
  合理价   = 合理 PB × 最新每股净资产
  ROE ≤ r 时退化为 ROE/r（无增长零溢价）；合理 PB 下限 0.3（防极端）。
交叉：PB 历史分位（PIT 快照 20 期 price_to_book_ratio 序列，有值才显示）。

用法:
  python -m tradingagents.ashare.valuation 000651 600941 ...   # CLI 估值卡
  from tradingagents.ashare.valuation import fair_value         # 库调用
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import date
from pathlib import Path
from typing import Optional

from tradingagents.ashare.data import AkshareDataClient
from tradingagents.ashare.material import fetch_daily_kline
from tradingagents.ashare.snapshot import FundamentalsSnapshot, build_snapshot

REQ_RETURN = 0.10   # 默认要求回报率 r（被 asset_r 覆盖）
PB_FLOOR = 0.30     # 合理 PB 下限（防低 ROE 极端）
PB_CAP = 10.0
SNAP_CACHE = Path("ashare_out") / "_cache" / "val-snap.json"

# 类债资产（公用事业/运营商/收租型）：市场接受低要求回报（r≈6%），
# 用 ROE-PB 高 r 会误判"高估"（长电 2.9PB 案例）。
BOND_LIKE = ("电力", "燃气", "水务", "高速", "公路", "港口", "机场", "铁路",
             "移动", "电信", "联通", "核电", "水电")


def annual_vol(code: str, as_of: str, n: int = 120) -> float:
    """近 n 日年化波动（r 的 σ 代理）。"""
    try:
        rows = [k for k in fetch_daily_kline(code, n + 10) if k["date"] <= as_of]
        closes = [r["close"] for r in rows[-n:]]
        if len(closes) < 40:
            return 0.20
        rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
        mean = sum(rets) / len(rets)
        var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
        return (var ** 0.5) * (252 ** 0.5)
    except Exception:
        return 0.20


def asset_r(name: str, code: str, as_of: str) -> tuple[float, str]:
    """资产属性 → 要求回报率 r。

    类债(关键词) → 0.06；否则 σ 映射 r=0.025+0.45σ，限 [0.07, 0.14]。
    """
    if any(k in (name or "") for k in BOND_LIKE):
        return 0.06, "类债(低要求回报)"
    sig = annual_vol(code, as_of)
    r = 0.025 + 0.45 * sig
    return max(0.07, min(r, 0.14)), f"σ映射(σ={sig*100:.0f}%)"


def _get_snapshot(code: str, as_of: str) -> Optional[FundamentalsSnapshot]:
    """PIT 快照对象，带 21 天文件缓存（存结构化 dump，免重复拉 akshare）。"""
    blob = None
    if SNAP_CACHE.is_file():
        try:
            blob = json.loads(SNAP_CACHE.read_text(encoding="utf-8"))
        except Exception:
            blob = None
    cache = blob or {}
    key = f"{code}"
    if key in cache:
        entry = cache[key]
        saved = date.fromisoformat(entry["day"])
        if (date.today() - saved).days < 21 and entry["as_of"] <= as_of:
            return FundamentalsSnapshot.model_validate(entry["snap"])
    snap = build_snapshot(code, as_of, AkshareDataClient())
    cache[key] = {"day": date.today().isoformat(), "as_of": as_of,
                  "snap": snap.model_dump(mode="json")}
    try:
        SNAP_CACHE.parent.mkdir(parents=True, exist_ok=True)
        SNAP_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return snap


def fair_value(code: str, as_of: str, name: str = "", snap=None) -> Optional[dict]:
    """估值锚：合理价/PB/结论/分位。r 按资产属性分档（v3）。"""
    try:
        px = [k for k in fetch_daily_kline(code, 20) if k["date"] <= as_of][-1]["close"]
        if snap is None:
            snap = _get_snapshot(code, as_of)
        if snap is None:
            return None
        bvps = snap.periods[0].book_value_per_share
        if not bvps or bvps <= 0:
            return None
        roe = snap.roe_avg or 0.0
        g = snap.bvps_cagr or 0.0
        if g < 0 or g >= REQ_RETURN:
            g = 0.0
        r, how = asset_r(name or code, code, as_of)
        # 合理 PB：Gordon 变体；ROE<=r 退化 ROE/r
        if roe > r and roe > g:
            fair_pb = (roe - g) / (r - g)
        else:
            fair_pb = roe / r
        fair_pb = max(PB_FLOOR, min(fair_pb, PB_CAP))
        fair_price = fair_pb * bvps
        cur_pb = px / bvps
        # PB 历史分位（快照 period 序列里 price_to_book_ratio 有值的）
        pbs = [p.price_to_book_ratio for p in snap.periods if p.price_to_book_ratio]
        pct = None
        if len(pbs) >= 4:
            pct = sum(1 for x in pbs if x <= cur_pb) / len(pbs) * 100
        delta = (px / fair_price - 1) * 100
        zone = "🔥高估" if delta > 30 else ("偏高" if delta > 10 else
               ("⚖️合理" if delta > -15 else ("低估" if delta > -30 else "🧊深度低估")))
        return {
            "code": code, "price": px, "roe": roe, "g": g,
            "bvps": bvps, "cur_pb": cur_pb, "fair_pb": fair_pb,
            "fair_price": fair_price, "delta_pct": delta,
            "pb_pct": pct, "zone": zone, "r": r, "r_how": how,
        }
    except Exception:
        return None


def render(v: dict) -> str:
    pct_s = f"｜PB分位 {v['pb_pct']:.0f}%" if v.get("pb_pct") is not None else ""
    return (f"{v['code']}: 现价 {v['price']:.2f} vs 合理价 {v['fair_price']:.1f} "
            f"({v['delta_pct']:+.0f}%) {v['zone']} {pct_s} ｜ "
            f"r={v['r']*100:.0f}%({v['r_how']}) ROE {v['roe']*100:.0f}% "
            f"现PB {v['cur_pb']:.2f} 合理PB {v['fair_pb']:.2f}")


def _names() -> dict:
    import yaml
    try:
        d = yaml.safe_load(Path("/Users/vega/git/ai-hedge-fund/config/tickers.yaml").read_text(encoding="utf-8"))
        return {t["code"]: t.get("name", "") for t in d["tickers"]}
    except Exception:
        return {}


if __name__ == "__main__":
    as_of = sys.argv[1] if len(sys.argv) > 1 and "-" in sys.argv[1] else "2026-09-09"
    codes = [a for a in sys.argv[1:] if "-" not in a] or ["000651", "600941", "600900"]
    names = _names()
    for c in codes:
        v = fair_value(c, as_of, name=names.get(c, ""))
        print(render(v) if v else f"{c}: 数据不足")
