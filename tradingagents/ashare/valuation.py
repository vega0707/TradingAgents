#!/usr/bin/env python3
"""valuation.py — 合理价估值锚（PB-ROE-Gordon v3）

回答"这票到底值多少、现在贵不贵"：
  合理 PB = (ROE - g) / (r - g)     r 按资产属性分档（类债 6%/σ 映射）
  合理价   = 合理 PB × 最新每股净资产
  双源：akshare PIT 快照(主,21天缓存) → 新浪财务页(fallback,限流时自动切)
交叉：PB 历史分位（快照有值才显示）。

用法:
  python -m tradingagents.ashare.valuation 000651 600941 ...   # CLI 估值卡
  from tradingagents.ashare.valuation import fair_value         # 库调用
"""
from __future__ import annotations

import json
import re
import statistics
import sys
import urllib.request
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
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/126.0"}


def sina_metrics(code: str, year: int = 2025) -> dict | None:
    """新浪财务指标页(GBK) → {roe, bvps}。akshare 限流时的独立 fallback。"""
    try:
        url = (f"https://money.finance.sina.com.cn/corp/go.php/vFD_FinancialGuideLine/"
               f"stockid/{code}/ctrl/{year}/displaytype/4.phtml")
        raw = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15).read()
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw.decode("gbk", "replace")))

        def grab(kw):
            i = text.find(kw)
            if i < 0:
                return None
            seg = text[i:i + 60].split(kw, 1)[1]
            m = re.search(r"[\d.]+", seg)
            return float(m.group()) if m else None

        roe, bvps = grab("净资产收益率(%)"), grab("每股净资产_调整前(元)")
        if roe and bvps:
            return {"roe": roe / 100, "bvps": bvps, "g": 0.0}
        return None
    except Exception:
        return None


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


def _get_snapshot(code: str, as_of: str, allow_fetch: bool = True) -> Optional[FundamentalsSnapshot]:
    """PIT 快照对象，21 天文件缓存优先；allow_fetch=True 才冷拉 akshare。"""
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
    if not allow_fetch:
        return None  # 缓存 miss 且不许冷拉 → 调用方走新浪快路径
    snap = build_snapshot(code, as_of, AkshareDataClient())
    cache[key] = {"day": date.today().isoformat(), "as_of": as_of,
                  "snap": snap.model_dump(mode="json")}
    try:
        SNAP_CACHE.parent.mkdir(parents=True, exist_ok=True)
        SNAP_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return snap


def fair_value(code: str, as_of: str, name: str = "", snap=None,
               allow_fetch: bool = False) -> Optional[dict]:
    """估值锚：合理价/PB/结论/分位。r 按资产属性分档（v3）。

    数据源：akshare PIT 快照缓存(21天) → miss 且 allow_fetch 时冷拉 akshare，
    否则快速切新浪财务页(单期 ROE/BVPS)。返回 src 标注数据源。
    """
    try:
        px = [k for k in fetch_daily_kline(code, 20) if k["date"] <= as_of][-1]["close"]
        r, how = asset_r(name or code, code, as_of)   # r 先定，分支内 g 归零依赖它
        src = "akshare"
        if snap is None:
            # 决策单每日调用走快路径(缓存→新浪)；CLI --fetch 才冷拉 akshare
            snap = _get_snapshot(code, as_of, allow_fetch=allow_fetch)
        if snap is None:
            sm = sina_metrics(code)          # fallback：akshare 限流/无缓存时
            if not sm:
                return None
            roe, bvps, g = sm["roe"], sm["bvps"], sm["g"]
            pct = None
            src = "sina"
        else:
            bvps = snap.periods[0].book_value_per_share
            if not bvps or bvps <= 0:
                return None
            # ROE 用最近年报(12-31)期——快照 roe_avg 混入未年化的 Q1 单季
            # ROE(4%)会摊薄真实盈利(格力年报 20%→均值 14%)，口径失真
            annual = next((p.return_on_equity for p in snap.periods
                           if p.return_on_equity and p.report_period.endswith("12-31")), None)
            roe = annual or (snap.roe_avg or 0.0)
            # g 固定 0（零增长保守假设）：bvps_cagr 账面增长含会计因素，
            # 类债资产(长电)上失真会让 Gordon 分母爆炸，保守不为增长付溢价
            g = 0.0
            pct = None               # PB 分位需历史 PB 序列（快照无此字段），暂缺
        # 合理 PB：Gordon 变体；ROE<=r 退化 ROE/r
        if roe > r and roe > g:
            fair_pb = (roe - g) / (r - g)
        else:
            fair_pb = roe / r
        fair_pb = max(PB_FLOOR, min(fair_pb, PB_CAP))
        fair_price = fair_pb * bvps
        cur_pb = px / bvps
        delta = (px / fair_price - 1) * 100
        zone = "🔥高估" if delta > 30 else ("偏高" if delta > 10 else
               ("⚖️合理" if delta > -15 else ("低估" if delta > -30 else "🧊深度低估")))
        return {
            "code": code, "price": px, "roe": roe, "g": g,
            "bvps": bvps, "cur_pb": cur_pb, "fair_pb": fair_pb,
            "fair_price": fair_price, "delta_pct": delta,
            "pb_pct": pct, "zone": zone, "r": r, "r_how": how, "src": src,
        }
    except Exception as _e:
        import traceback as _tb
        _tb.print_exc()
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
    args = [a for a in sys.argv[1:]]
    fetch = "--fetch" in args
    args = [a for a in args if a != "--fetch"]
    as_of = args[0] if args and "-" in args[0] else "2026-09-09"
    codes = [a for a in args if "-" not in a] or ["000651", "600941", "600900"]
    names = _names()
    for c in codes:
        v = fair_value(c, as_of, name=names.get(c, ""), allow_fetch=fetch)
        print(render(v) if v else f"{c}: 数据不足")
