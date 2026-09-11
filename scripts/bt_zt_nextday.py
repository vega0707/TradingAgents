#!/usr/bin/env python3
"""bt_zt_nextday.py — 回测：昨日涨停股今日（次日）表现

回答 A 股短线核心问题：追涨停/打板隔日有没有正期望。
- 涨停日 D 的涨停池 → D+1 开盘溢价/收盘 vs 涨停价
- 分连板数(1板/2板+)与大盘对比
数据：东财涨停池(akshare) + 腾讯批量次日行情。请求量≈14×3，限速执行。

用法: .venv/bin/python scripts/bt_zt_nextday.py [--days 14]
"""
import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

OUT = Path("ashare_out") / "_bt"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0"


def trade_days_before(end: str, n: int) -> list[str]:
    """end(YYYY-MM-DD 或 YYYYMMDD) 往前 n 个交易日（含 end）→ ['YYYYMMDD'...] 升序。"""
    import akshare as ak
    cal = ak.tool_trade_date_hist_sina()
    days = sorted(str(d)[:10].replace("-", "") for d in cal["trade_date"])
    end_fmt = end.replace("-", "")
    if end_fmt not in days:
        end_fmt = max(d for d in days if d <= end_fmt)
    idx = days.index(end_fmt)
    return days[idx - n + 1: idx + 1]


def zt_pool(day: str) -> list[dict]:
    """一天涨停池 → [{code,name,zt_price,lb(连板),ind}]。"""
    import akshare as ak
    df = ak.stock_zt_pool_em(date=day)
    if df is None or df.empty:
        return []
    return [{
        "code": str(r["代码"]), "name": str(r["名称"]),
        "zt_price": float(r["最新价"]), "lb": int(r["连板数"] or 0),
        "ind": str(r.get("所属行业", "")),
    } for _, r in df.iterrows()]


def tx_quote(codes: list[str]) -> dict[str, dict]:
    """腾讯批量 → {code: {open,last}}。60 只/请求分批。"""
    out: dict[str, dict] = {}
    for i in range(0, len(codes), 60):
        batch = codes[i:i + 60]
        syms = ",".join(("sh" if c[0] in "6" else "sz") + c for c in batch)
        url = f"https://qt.gtimg.cn/q={syms}"
        raw = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": _UA}), timeout=12).read().decode("gbk", "ignore")
        for line in raw.strip().split(";"):
            if "=" not in line:
                continue
            f = line.split("=")[1].strip('"').split("~")
            # f[2]=6位代码, f[3]=最新, f[5]=今开
            if len(f) > 5 and f[2] and f[3]:
                out[f[2]] = {"open": float(f[5]) if f[5] else None, "last": float(f[3])}
        if len(codes) > 60:
            time.sleep(1)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()

    days = trade_days_before("2026-09-07", args.days)  # 涨停日截止 9/4 之前已完整
    zt_days = days[:-1]  # 最后一天(9/5? 9/7)涨停的次日未完整，排除
    print(f"回测涨停日 {len(zt_days)} 个: {zt_days[0]} ~ {zt_days[-1]}")

    OUT.mkdir(parents=True, exist_ok=True)
    all_records = []
    for d in zt_days:
        cache = OUT / f"zt_{d}.json"
        if cache.exists():
            pool = json.loads(cache.read_text())
        else:
            pool = zt_pool(d)
            cache.write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")
            time.sleep(1.2)
        print(f"  {d}: {len(pool)} 只涨停")
        all_records.append((d, pool))
        if not pool:
            continue
        # 次日行情（交易日历下一日）
        nxt = days[days.index(d) + 1] if d in days else None
        if not nxt:
            continue
        q = tx_quote([p["code"] for p in pool])
        day_file = OUT / f"nx_{d}.json"
        day_file.write_text(json.dumps(q, ensure_ascii=False), encoding="utf-8")
        time.sleep(1.2)
    print("数据收集完成，样本:", sum(len(p) for _, p in all_records))


if __name__ == "__main__":
    main()
