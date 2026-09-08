#!/usr/bin/env python3
"""bt_dip_scan.py — dip_buy 深跌乖离策略广度回测 + 参数网格寻优

测试池 36 只（持仓+观察池+历史候选+行业大票）。每只 1 次日K(520根)，
本地计算全部参数组合，请求量≈新拉数(复用缓存)。

指标：相对"全期随机基线"的超额与胜率。寻优维度：
  阈值  dip ∈ {0.85,0.88,0.90,0.92,0.95}
  持有  h ∈ {5,10,20}
用法: .venv/bin/python scripts/bt_dip_scan.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tradingagents.ashare.material import fetch_daily_kline

POOL = ["601318", "600582", "300679", "000915", "601669", "601668", "601128",
        "601006", "600219", "600900", "000651", "600941", "601088",   # 持仓
        "000970", "600548", "600196", "600332", "000582",             # 观察池
        "002012", "603011", "002951", "001360", "603466",             # 历史候选
        "603938", "002971", "601869", "600487", "300879",             # 事件股
        "600519", "000858", "600585", "600030", "600887", "601166", "300760"]
NAMES = {"601318": "平安", "600582": "天地", "300679": "电连", "000915": "华特",
         "601669": "电建", "601668": "建筑", "601128": "常熟", "601006": "大秦",
         "600219": "南山", "600900": "长电", "000651": "格力", "600941": "移动",
         "601088": "神华", "000970": "中科三环", "600548": "深高速", "600196": "复星",
         "600332": "白云山", "000582": "北湾", "002012": "凯恩", "603011": "合锻",
         "002951": "金时", "001360": "南矿", "603466": "风语筑", "603938": "三孚",
         "002971": "和远", "601869": "长飞", "600487": "亨通", "300879": "大叶",
         "600519": "茅台", "000858": "五粮液", "600585": "海螺", "600030": "中信",
         "600887": "伊利", "601166": "兴业", "300760": "迈瑞"}

AS_OF = "2026-09-08"
DIPS = [0.85, 0.88, 0.90, 0.92, 0.95]
HORIZONS = [5, 10, 20]


def ma_series(closes, w):
    out = [None] * len(closes)
    for i in range(len(closes)):
        if i >= w - 1:
            out[i] = sum(closes[i - w + 1:i + 1]) / w
    return out


def dip_stats(closes, ma60, dip, h):
    n = len(closes)
    sig = [i for i in range(60, n - h) if ma60[i] and closes[i] <= ma60[i] * dip]
    if len(sig) < 8:
        return None
    rs = [closes[i + h] / closes[i] - 1 for i in sig if i + h < n]
    bl = sum(closes[j + h] / closes[j] - 1 for j in range(0, n - h, 5)) / max((n - h) // 5, 1)
    avg = sum(rs) / len(rs)
    win = sum(1 for x in rs if x > 0) / len(rs) * 100
    return len(sig), (avg - bl) * 100, win, avg * 100


def main() -> None:
    rows_by_code = {}
    for code in POOL:
        try:
            rows = [k for k in fetch_daily_kline(code, 520) if k["date"] <= AS_OF]
            if len(rows) >= 200:
                rows_by_code[code] = [r["close"] for r in rows]
        except Exception:
            pass
    print(f"数据就绪 {len(rows_by_code)}/{len(POOL)} 只\n")

    # 每票最佳格子
    best = {}
    agg = {d: {h: [] for h in HORIZONS} for d in DIPS}
    for code, closes in rows_by_code.items():
        ma60 = ma_series(closes, 60)
        name = NAMES.get(code, code)
        best_for_code = None
        for d in DIPS:
            for h in HORIZONS:
                r = dip_stats(closes, ma60, d, h)
                if r:
                    n, ex, win, _ = r
                    agg[d][h].append((ex, win, n))
                    if best_for_code is None or ex > best_for_code[2]:
                        best_for_code = (d, h, ex, win)
        if best_for_code:
            best[code] = (name, *best_for_code)

    print("=== 每票最优参数 ===")
    for code, (name, d, h, ex, win) in sorted(best.items(), key=lambda x: -x[1][3]):
        print(f"{name:<8}{code} dip={d} 持{h}日 超额{ex:+.1f}% 胜{win:.0f}%")

    print("\n=== 全池聚合（超额均值 / 胜率均值 / 样本数）===")
    for d in DIPS:
        line = [f"dip{d}: "]
        for h in HORIZONS:
            xs = agg[d][h]
            if xs:
                ex = sum(x[0] for x in xs) / len(xs)
                win = sum(x[1] for x in xs) / len(xs)
                n = sum(x[2] for x in xs)
                line.append(f"持{h}日 超额{ex:+.1f}% 胜{win:.0f}% (n={n})")
        print(" ".join(line))


if __name__ == "__main__":
    main()
