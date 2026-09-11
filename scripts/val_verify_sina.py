#!/usr/bin/env python3
"""val_verify_sina.py — 估值锚交叉验证：新浪财务源 vs akshare 快照

新浪(独立源,免费,未被限流)拉 ROE/每股净资产 → 算合理价/PB，
与 valuation.fair_value(akshare PIT 快照) 对比。口径差异注明：
- 新浪 ctrl=Y 年报 ROE（单期）；akshare 用历史平均 ROE
- BVPS 新浪用"调整前每股净资产"，akshare 用快照最新期
用法: .venv/bin/python scripts/val_verify_sina.py [代码...]
"""
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tradingagents.ashare.material import fetch_daily_kline
from tradingagents.ashare.valuation import REQ_RETURN, fair_value

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/126.0"}
DEFAULT = ["601318", "600941", "000651", "600900", "601668", "000970", "600366"]
AS_OF = "2026-09-09"


def sina_finance(code: str, year: int = 2025):
    """新浪财务指标页 → {roe, bvps}（最新报告期列）。"""
    url = (f"https://money.finance.sina.com.cn/corp/go.php/vFD_FinancialGuideLine/"
           f"stockid/{code}/ctrl/{year}/displaytype/4.phtml")
    raw = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15).read()
    text = re.sub(r"<[^>]+>", " ", raw.decode("gbk", "replace"))
    text = re.sub(r"\s+", " ", text)

    def grab(kw):
        i = text.find(kw)
        if i < 0:
            return None
        seg = text[i:i + 60]
        m = re.search(r"[\d.]+", seg.split(kw, 1)[1] if kw in seg else "")
        return float(m.group()) if m else None

    return {"roe": grab("净资产收益率(%)"), "bvps": grab("每股净资产_调整前(元)")}


def main() -> None:
    from tradingagents.ashare.valuation import asset_r
    import yaml
    d = yaml.safe_load(Path("/Users/vega/git/ai-hedge-fund/config/tickers.yaml").read_text(encoding="utf-8"))
    names = {t["code"]: t.get("name", "") for t in d["tickers"]}
    codes = [a for a in sys.argv[1:] if "-" not in a] or DEFAULT
    print(f"{'代码':<8}{'名称':<6}{'ROE%':<6}{'BVPS':<7}{'现价':<7}{'r%':<5}{'合理PB':<7}{'合理价':<8}{'vs现价'}")
    for code in codes:
        name = names.get(code, "")
        px = [k for k in fetch_daily_kline(code, 20) if k["date"] <= AS_OF][-1]["close"]
        try:
            s = sina_finance(code)
            if s["roe"] and s["bvps"]:
                roe = s["roe"] / 100
                r, how = asset_r(name, code, AS_OF)
                fair_pb = max(0.3, roe / r)
                fair = fair_pb * s["bvps"]
                pb = px / s["bvps"]
                print(f"{code:<8}{name:<7}{s['roe']:<6.1f}{s['bvps']:<7.1f}{px:<7.1f}"
                      f"{r*100:<5.0f}{fair_pb:<7.2f}{fair:<8.1f}{(px/fair-1)*100:+.0f}%")
        except Exception as e:  # noqa: BLE001
            print(f"{code:<8}{name:<7}失败 {str(e)[:50]}")


if __name__ == "__main__":
    main()
