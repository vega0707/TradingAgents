#!/usr/bin/env python3
"""bt_dip_oos.py — dip_buy 样本外验证：前 2/3 历史定参，后 1/3 验证

防自我欺骗：样本内挑的 -12% 阈值必须经未参与定参的时段检验。
- 前半段(定参)：各阈值超额 → 选最优
- 后半段(验证)：固定前半最优阈值，算超额/胜率 vs 基线
用法: .venv/bin/python scripts/bt_dip_oos.py
"""
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tradingagents.ashare.material import fetch_daily_kline

POOL = ["601318", "600582", "300679", "000915", "601669", "601668", "601128",
        "601006", "600219", "600900", "000651", "600941", "601088",
        "000970", "600548", "600196", "600332", "000582",
        "603938", "601869", "600519", "000858", "600585", "600030"]
AS_OF = "2026-09-08"
DIPS = [0.85, 0.88, 0.90, 0.92, 0.95]
H = 20


def main() -> None:
    closes_by = {}
    for code in POOL:
        try:
            rows = [k for k in fetch_daily_kline(code, 520) if k["date"] <= AS_OF]
            if len(rows) >= 300:
                closes_by[code] = [r["close"] for r in rows]
        except Exception:
            pass
    print(f"样本 {len(closes_by)} 只\n")

    def ma60(closes):
        out = [None] * len(closes)
        for i in range(len(closes)):
            if i >= 59:
                out[i] = sum(closes[i - 59:i + 1]) / 60
        return out

    def segment_stats(closes, seg):
        """seg=(start,end) 索引段内评估 dip 策略（信号在段内触发，收益也在段内兑现）。"""
        out = {}
        for d in DIPS:
            rs, n = [], 0
            m60 = ma60(closes)
            for i in range(max(60, seg[0]), seg[1] - H):
                if m60[i] and closes[i] <= m60[i] * d:
                    n += 1
                    if i + H < len(closes):
                        rs.append(closes[i + H] / closes[i] - 1)
            if rs:
                bl = sum(closes[j + H] / closes[j] - 1 for j in range(seg[0], seg[1] - H, 5)) / max((seg[1] - seg[0] - H) // 5, 1)
                out[d] = (n, (sum(rs) / len(rs) - bl) * 100,
                          sum(1 for x in rs if x > 0) / len(rs) * 100)
        return out

    # 每只票前半段选最优阈值
    chosen = {}
    for c, cl in closes_by.items():
        n = len(cl)
        split = n * 2 // 3
        stats = segment_stats(cl, (60, split))
        if stats:
            best_d = max(stats, key=lambda d: stats[d][1])
            chosen[c] = best_d

    # 后半段：分别用"前半最优阈值"与"固定-12%"验证
    def verify(d_sel):
        exs, wins, ns = [], [], 0
        for c, cl in closes_by.items():
            n = len(cl)
            split = n * 2 // 3
            d = d_sel(c)
            st = segment_stats(cl, (split, n - 1))
            if st and d in st:
                n_, ex, win = st[d]
                exs.append(ex); wins.append(win); ns += n_
        if not exs:
            return None
        return (len(exs), sum(exs) / len(exs), sum(wins) / len(wins), ns)

    r1 = verify(lambda c: chosen.get(c, 0.88))
    r2 = verify(lambda c: 0.88)
    print("=== 样本外验证（后 1/3 时段）===")
    if r1:
        print(f"前半最优阈值: 平均超额 {r1[1]:+.2f}% 胜率 {r1[2]:.0f}% (样本 {r1[3]})")
    if r2:
        print(f"固定 -12% 阈值: 平均超额 {r2[1]:+.2f}% 胜率 {r2[2]:.0f}% (样本 {r2[3]})")
    if r1 and r2:
        verdict = "✅ 样本外仍为正超额" if r2[1] > 0 else "⚠️ 样本外超额消失/转负——参数可能过拟合"
        print(verdict)


if __name__ == "__main__":
    main()
