#!/usr/bin/env python3
"""A股抄底候选粗筛（新浪全市场排序接口，3 次请求，本机可达无频控）

第 1 层：当日深跌 + 低 PB + 大市值（新浪 getHQNodeData，一次拉全排序）
第 2 层：对 top N 用腾讯日 K 补算 60 日跌幅与均线位置（每只 1 次请求）
用法: .venv/bin/python scripts/scan_dip.py [--top N] [--check60 N]
"""
import argparse
import json
import time
import urllib.request

SINA_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeData")
TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _get(url: str, timeout: int = 12) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Referer": "https://finance.sina.com.cn"})
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"fetch failed: {url} :: {last_err}")


def fetch_sina(page: int, num: int = 100) -> list[dict]:
    """当日跌幅榜（asc=1 跌幅最深在前）。"""
    url = (f"{SINA_URL}?page={page}&num={num}&sort=changepercent&asc=1"
           f"&node=hs_a&symbol=&_s_r_a=init")
    d = _get(url)
    return d if isinstance(d, list) else []


def drop60(code: str) -> float | None:
    """腾讯日K 60 日跌幅 %（60个交易日前收盘 → 最新收盘）。"""
    symbol = ("sh" if code[0] in "6" else "sz") + code
    url = f"{TENCENT_KLINE}?param={symbol},day,,,70,qfq"
    try:
        d = _get(url)
        rows = (d.get("data", {}).get(symbol, {}) or {}).get("qfqday") or \
               (d.get("data", {}).get(symbol, {}) or {}).get("day")
        if not rows or len(rows) < 2:
            return None
        closes = [float(r[2]) for r in rows]
        if len(closes) >= 61:
            base = closes[-61]
        else:
            base = closes[0]
        latest = closes[-1]
        if base <= 0:
            return None
        return (latest / base - 1) * 100
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--check60", type=int, default=10, help="对前 N 只补算 60日跌幅")
    ap.add_argument("--min-mktcap", type=float, default=60.0, help="总市值下限(亿)")
    ap.add_argument("--max-pb", type=float, default=2.0)
    ap.add_argument("--min-drop-today", type=float, default=-4.0, help="当日跌幅需低于此")
    args = ap.parse_args()

    raw = []
    for pno in range(1, 4):  # 当日跌幅榜前 300
        page = fetch_sina(pno)
        raw.extend(page)
        if len(page) < 100:
            break
        time.sleep(1.2)
    print(f"新浪当日跌幅榜拉取 {len(raw)} 只（3 次请求）")

    cands = []
    for x in raw:
        code, name = str(x.get("code", "")), str(x.get("name", ""))
        trade = x.get("trade")
        chg = x.get("changepercent")
        pb = x.get("pb")
        if not code or not name or not trade or chg is None:
            continue
        if code.startswith(("688", "689", "92", "4", "8", "900")):
            continue
        if any(k in name.upper() for k in ("ST", "退")) or name[:1] in ("N", "C"):
            continue
        mktcap = (x.get("mktcap") or 0) / 1e4  # 万 -> 亿
        if mktcap < args.min_mktcap:
            continue
        if pb is None or not (0.3 < pb < args.max_pb):
            continue
        if not (args.min_drop_today - 20 < chg < args.min_drop_today):
            continue
        cands.append({
            "code": code, "name": name, "price": float(trade),
            "chg_today": round(chg, 2), "pb": round(pb, 2),
            "pe": round(x["per"], 1) if (x.get("per") or 0) > 0 else None,
            "mktcap_yi": round(mktcap, 1),
            "turnover": round(x.get("turnoverratio") or 0, 2),
        })
    cands.sort(key=lambda c: c["chg_today"])

    # 第 2 层：前 N 只补 60日跌幅（腾讯，逐只 1 请求）
    for c in cands[: args.check60]:
        d60 = drop60(c["code"])
        c["drop60"] = round(d60, 1) if d60 is not None else None
        time.sleep(0.8)

    # 综合排序：60日深跌优先（有则按它，无则按当日）
    def keyf(c):
        return (c.get("drop60") if c.get("drop60") is not None else c["chg_today"])
    cands.sort(key=keyf)
    cands = cands[: args.top]

    print(f"\n抄底候选 {len(cands)} 只（当日深跌 + PB<{args.max_pb} + 市值>{args.min_mktcap}亿）：\n")
    for i, c in enumerate(cands, 1):
        d60 = f"60日{c['drop60']:+.0f}%" if c.get("drop60") is not None else "60日?"
        pe = c["pe"] if c["pe"] else "亏"
        print(f"{i:>2}. {c['code']} {c['name']:<7} 价{c['price']:<8} 今{c['chg_today']:+.1f}% "
              f"{d60} PB{c['pb']:.2f} PE({pe}) 市值{c['mktcap_yi']}亿 换手{c['turnover']}%")
    if cands:
        with open("/tmp/ashare-dip-candidates.json", "w", encoding="utf-8") as f:
            json.dump(cands, f, ensure_ascii=False, indent=2)
        print(f"\n候选已存 /tmp/ashare-dip-candidates.json")


if __name__ == "__main__":
    main()
