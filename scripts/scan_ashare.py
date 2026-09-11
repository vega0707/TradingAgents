#!/usr/bin/env python3
"""A股全市场机会扫描（除科创板）— 东财实时行情粗筛 → 候选清单

用法: .venv/bin/python scripts/scan_ashare.py [--top N] [--min-price 3] [--max-price 200]

原理:
1. 东财 clist 接口一次拉全市场（排除科创板 688 / 北交所 8/4 开头可选），按活跃度排序
2. 量化粗筛: 涨跌幅 / 换手率 / 成交额 等客观指标（LLM 不参与，快）
3. 输出候选清单（几十只）→ 供 TradingAgents 深度分析

fs 板块编码:
  m:0+t:6   深主板   m:0+t:80 深创业板  m:1+t:2  沪主板   m:1+t:23 科创板
  排除科创 = 不用 m:1+t:23；排除北交所 = 默认不含 m:0+t:81+s:2048
"""
import argparse
import json
import time
import urllib.request

EM_URL = "https://push2.eastmoney.com/api/qt/clist/get"
# 沪深主板 + 创业板（排除科创板 688 / 北交所）
FS = "m:0+t:6,m:0+t:80,m:1+t:2"
FIELDS = "f2,f3,f5,f6,f8,f10,f12,f14,f15,f16,f17,f18"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
}


def fetch_page(page: int, page_size: int = 100, sort_field: str = "f3", order: int = 0) -> dict:
    """拉一页。sort_field=f3(涨幅)/f8(换手)/f6(成交额)；order 0=降序 1=升序。带重试。"""
    url = (f"{EM_URL}?pn={page}&pz={page_size}&po={order}&np=1&fltt=2&invt=2"
           f"&fid={sort_field}&fs={FS}&fields={FIELDS}")
    req = urllib.request.Request(url, headers=HEADERS)
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"fetch page {page} failed: {last_err}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=60, help="候选数量")
    ap.add_argument("--min-price", type=float, default=3.0)
    ap.add_argument("--max-price", type=float, default=300.0)
    ap.add_argument("--min-amount", type=float, default=1.0, help="最小成交额(亿)")
    ap.add_argument("--sort", default="f3", choices=["f3", "f8", "f6"], help="排序字段")
    ap.add_argument("--asc", action="store_true", help="升序(找超跌)")
    args = ap.parse_args()

    # 拉涨幅榜前几页拼候选池，再按价格/成交额过滤
    page_size = 100
    raw: list[dict] = []
    for pno in range(1, 3):  # 拉 2 页 = 200 只活跃股
        data = fetch_page(pno, page_size, args.sort, 0 if not args.asc else 1)
        diff = (data.get("data") or {}).get("diff") or []
        raw.extend(diff)
        if len(diff) < page_size:
            break
        time.sleep(3)

    # 过滤: 去科创(688/689)、去 ST/退市、价格区间、成交额下限
    cands = []
    for s in raw:
        code = str(s.get("f12", ""))
        name = str(s.get("f14", ""))
        price = s.get("f2")  # 可能为 None(停牌)
        if not code or not name or price is None:
            continue
        if code.startswith(("688", "689", "4", "8")):  # 科创/北交所
            continue
        if "ST" in name.upper() or "退" in name or "N" == name[0]:
            continue
        if not (args.min_price <= price <= args.max_price):
            continue
        amount_yi = (s.get("f6") or 0) / 1e8
        if amount_yi < args.min_amount:
            continue
        cands.append({
            "code": code, "name": name,
            "price": price,
            "pct": s.get("f3"),          # 涨跌幅 %
            "volume": s.get("f5"),
            "amount_yi": round(amount_yi, 2),
            "turnover": s.get("f8"),     # 换手率 %
            "pe": s.get("f10"),
        })

    # 去重 + 截断
    seen, uniq = set(), []
    for c in cands:
        if c["code"] not in seen:
            seen.add(c["code"]); uniq.append(c)
    cands = uniq[: args.top]

    print(f"扫描完成: 全市场活跃池 {len(raw)} 只 → 过滤后候选 {len(cands)} 只")
    for i, c in enumerate(cands, 1):
        print(f"{i:>3}. {c['code']} {c['name']:<8} 价{c['price']:<8} 涨{c['pct']}% 换手{c['turnover']}% 额{c['amount_yi']}亿")
    if cands:
        with open("/tmp/ashare-candidates.json", "w", encoding="utf-8") as f:
            json.dump(cands, f, ensure_ascii=False, indent=2)
        print(f"\n候选已存 /tmp/ashare-candidates.json")


if __name__ == "__main__":
    main()
