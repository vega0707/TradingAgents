#!/usr/bin/env python3
"""macro.py — 宏观大势判断（三层抽丝剥笋）

起因（用户 2026-09-17）：美联储三年来首次加息当天，早盘简报一个字没提；
用户追问"我们是要关注宏观的，你能不能抽丝剥笋判断大势"。

推理链（每层回答一个问题，层层向下）：
  ① 全球流动性 —— 外部约束有多紧？
       美联储方向（加息/降息/维持，从隔夜快讯判定）
       美元指数、离岸人民币（资金流向与汇率压力）
       黄金、原油（避险与通胀）
  ② 国内基本面 —— 盈利周期在什么位置？
       PPI（工业品价格 → 企业盈利弹性）
       CPI（通胀 → 货币政策空间）
       PMI（制造业景气）
       GDP（总需求）
  ③ 市场结构 —— 现在贵不贵、在趋势哪一边？
       沪深300 与 MA200 的关系（趋势）
       沪深300 价格历史分位（估值位置）

合成：三层加权 → 大势评级（进攻 / 中性 / 防守）+ 关键矛盾 + 仓位含义。
设计原则：数据缺失就标注并跳过该维度，绝不臆造；结论必须能追溯到具体数字。

数据源：东财 datacenter（宏观指标）+ 东财行情（美元/汇率/商品，多域名轮询）
        + 新浪（指数 K 线，走本地缓存）。当日缓存，避免重复请求。

用法: .venv/bin/python -m tradingagents.ashare.macro [--refresh]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

CACHE = Path("ashare_out") / "_cache" / "macro.json"
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
QUOTE_UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
QUOTE_HOSTS = ("push2delay.eastmoney.com", "82.push2.eastmoney.com", "push2.eastmoney.com")
MACRO_HOSTS = ("datacenter-web.eastmoney.com", "datacenter.eastmoney.com")

# 行情 secid：美元指数 / 离岸人民币 / 纽约金 / NYMEX原油
SECIDS = {"UDI": "美元指数", "USDCNH": "离岸人民币", "GC00Y": "纽约金", "CL00Y": "NYMEX原油"}
SECID_LIST = "100.UDI,133.USDCNH,101.GC00Y,102.CL00Y"
MACRO_REPORTS = {"PMI": "RPT_ECONOMY_PMI", "CPI": "RPT_ECONOMY_CPI",
                 "PPI": "RPT_ECONOMY_PPI", "GDP": "RPT_ECONOMY_GDP"}


def _get(url: str, headers: dict, tries: int = 2) -> dict:
    last: Exception | None = None
    for i in range(tries):
        try:
            return json.loads(urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=18).read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i < tries - 1:
                time.sleep(2)
    raise last  # type: ignore[misc]


def quotes() -> dict[str, dict]:
    """隔夜/实时海外行情（美元、人民币、金、油）。"""
    for host in QUOTE_HOSTS:
        try:
            d = _get(f"https://{host}/api/qt/ulist.np/get?secids={SECID_LIST}"
                     f"&fltt=2&invt=2&fields=f12,f2,f3", QUOTE_UA, tries=1)
            rows = (d.get("data") or {}).get("diff") or []
            out = {}
            for r in rows:
                code = str(r.get("f12"))
                if code in SECIDS:
                    out[code] = {"name": SECIDS[code], "last": r.get("f2"), "pct": r.get("f3")}
            if out:
                return out
        except Exception:  # noqa: BLE001 — 换下一个行情节点
            continue
    return {}


def macro_series() -> dict[str, dict]:
    """最新一期宏观指标（PMI/CPI/PPI/GDP）。"""
    out: dict[str, dict] = {}
    for label, rn in MACRO_REPORTS.items():
        for host in MACRO_HOSTS:
            try:
                d = _get(f"https://{host}/api/data/v1/get?reportName={rn}&columns=ALL"
                         f"&pageSize=2&sortColumns=REPORT_DATE&sortTypes=-1", UA, tries=1)
                rows = (d.get("result") or {}).get("data") or []
                if rows:
                    out[label] = rows[0]
                    break
            except Exception:  # noqa: BLE001
                continue
    return out


def index_state() -> dict:
    """沪深300 趋势与位置（新浪 K 线，本地缓存）。"""
    try:
        from tradingagents.ashare.material import fetch_symbol_kline
        rows = [k for k in fetch_symbol_kline("sh000300", 1300)
                if k["date"] <= date.today().isoformat()]
        cl = [r["close"] for r in rows]
        if len(cl) < 200:
            return {}
        last = cl[-1]
        ma200 = sum(cl[-200:]) / 200
        win = cl[-1200:] if len(cl) >= 1200 else cl
        lo, hi = min(win), max(win)
        pct = (last - lo) / (hi - lo) * 100 if hi > lo else 50.0
        return {"last": last, "ma200": ma200, "above_ma200": last >= ma200,
                "dev_pct": (last / ma200 - 1) * 100, "price_pct": pct,
                "d20": (last / cl[-21] - 1) * 100 if len(cl) > 21 else None}
    except Exception:
        return {}


def fed_bias() -> str:
    """从隔夜快讯判定美联储方向（加息/降息/维持）。

    只做粗判：关键词计数偏哪边。精确判断交给早盘简报的 LLM（它已能看到
    完整快讯 + 海外硬数据）。
    """
    try:
        from tradingagents.ashare.daily_brief import fetch_feed
        items = fetch_feed(6)
        txt = " ".join(items)
        up = txt.count("加息")
        down = txt.count("降息")
        # 阈值放宽到 2 次：2026-09-17 实测当天 160 条快讯里"加息"只出现 2 次
        # （滚动窗口里大部分相关报道已滚出），按 >=3 会误判成'分歧'。
        if up >= 2 and up > down * 2:
            return f"偏鹰（快讯提'加息' {up} 次 / '降息' {down} 次）"
        if down >= 2 and down > up * 2:
            return f"偏鸽（快讯提'降息' {down} 次 / '加息' {up} 次）"
        if up or down:
            return f"分歧（加息 {up} / 降息 {down}）"
        return "无明确信号"
    except Exception:
        return "未知"


def collect(refresh: bool = False) -> dict:
    """汇总全部宏观数据（当日缓存）。"""
    if CACHE.is_file() and not refresh:
        try:
            blob = json.loads(CACHE.read_text(encoding="utf-8"))
            if blob.get("day") == date.today().isoformat():
                return blob["data"]
        except Exception:
            pass
    data = {"day": date.today().isoformat(), "quotes": quotes(),
            "macro": macro_series(), "index": index_state(), "fed": fed_bias()}
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return data


# ---------------------------------------------------------------------------
# 三层评分：每层 -2..+2，正=有利，负=不利
# ---------------------------------------------------------------------------

def score_liquidity(d: dict) -> tuple[int, list[str]]:
    """① 全球流动性：美联储方向 + 美元 + 人民币。"""
    s, notes = 0, []
    fed = d.get("fed", "")
    if "偏鹰" in fed:
        s -= 2
        notes.append(f"美联储{fed} → 全球流动性收紧")
    elif "偏鸽" in fed:
        s += 2
        notes.append(f"美联储{fed} → 流动性宽松")
    else:
        notes.append(f"美联储：{fed}")
    q = d.get("quotes") or {}
    cnh = q.get("USDCNH") or {}
    if isinstance(cnh.get("pct"), (int, float)):
        # 人民币计价：USDCNH 上涨=人民币贬值=压力
        if cnh["pct"] > 0.3:
            s -= 1
            notes.append(f"离岸人民币贬值 {cnh['pct']:+.2f}%（资金外流压力）")
        elif cnh["pct"] < -0.3:
            s += 1
            notes.append(f"离岸人民币升值 {cnh['pct']:+.2f}%")
    udi = q.get("UDI") or {}
    if isinstance(udi.get("pct"), (int, float)) and udi["pct"] > 0.5:
        s -= 1
        notes.append(f"美元指数走强 {udi['pct']:+.2f}%")
    return max(-2, min(2, s)), notes


def score_fundamentals(d: dict) -> tuple[int, list[str]]:
    """② 国内基本面：PPI / CPI / PMI。"""
    s, notes = 0, []
    m = d.get("macro") or {}
    ppi = (m.get("PPI") or {}).get("BASE_SAME")
    if isinstance(ppi, (int, float)):
        if ppi > 0:
            s += 1
            notes.append(f"PPI 同比 {ppi:+.1f}%（工业品涨价 → 企业盈利有弹性）")
        else:
            s -= 1
            notes.append(f"PPI 同比 {ppi:+.1f}%（工业品通缩 → 压制盈利）")
    cpi = (m.get("CPI") or {}).get("NATIONAL_SAME")
    if isinstance(cpi, (int, float)):
        if cpi > 3:
            s -= 1
            notes.append(f"CPI 同比 {cpi:+.1f}%（通胀约束货币政策）")
        else:
            notes.append(f"CPI 同比 {cpi:+.1f}%（通胀温和）")
    pmi_row = m.get("PMI") or {}
    pmi = pmi_row.get("MAKE_INDEX")   # 制造业 PMI 水平值（50 = 荣枯线）；MAKE_SAME 是同比变化，别用错
    if isinstance(pmi, (int, float)):
        if pmi >= 50:
            s += 1
            notes.append(f"制造业 PMI {pmi}（扩张区间，{pmi_row.get('TIME', '')}）")
        else:
            s -= 1
            notes.append(f"制造业 PMI {pmi}（低于荣枯线 50，景气偏弱）")
    return max(-2, min(2, s)), notes


def score_structure(d: dict) -> tuple[int, list[str]]:
    """③ 市场结构：趋势 + 位置。"""
    s, notes = 0, []
    idx = d.get("index") or {}
    if idx:
        if idx.get("above_ma200"):
            s += 1
            notes.append(f"沪深300 {idx['last']:.0f} 在 MA200 上方 {idx['dev_pct']:+.1f}%")
        else:
            s -= 1
            notes.append(f"沪深300 {idx['last']:.0f} 在 MA200 下方 {idx['dev_pct']:+.1f}%（趋势压制）")
        pct = idx.get("price_pct")
        if isinstance(pct, (int, float)):
            if pct < 30:
                s += 1
                notes.append(f"指数处近 5 年 {pct:.0f}% 分位（低估区间）")
            elif pct > 70:
                s -= 1
                notes.append(f"指数处近 5 年 {pct:.0f}% 分位（偏高）")
            else:
                notes.append(f"指数处近 5 年 {pct:.0f}% 分位（中性）")
    return max(-2, min(2, s)), notes


def judge(d: dict) -> dict:
    l, ln = score_liquidity(d)
    f, fn = score_fundamentals(d)
    st, sn = score_structure(d)
    total = l + f + st
    if total >= 3:
        stance, pos = "🟢 进攻", "可满仓（分批建仓）"
    elif total >= 1:
        stance, pos = "🟡 偏多", "7-8 成仓"
    elif total >= -1:
        stance, pos = "⚪ 中性", "半仓，等信号"
    elif total >= -3:
        stance, pos = "🟠 偏空", "3-4 成仓，防守为主"
    else:
        stance, pos = "🔴 防守", "低仓/空仓，保本优先"
    return {"total": total, "stance": stance, "position": pos,
            "layers": [("① 全球流动性", l, ln), ("② 国内基本面", f, fn), ("③ 市场结构", st, sn)]}


def render(d: dict, j: dict) -> str:
    q = d.get("quotes") or {}
    m = d.get("macro") or {}
    qs = " | ".join(f"{v['name']} {v['last']} ({v['pct']:+.2f}%)"
                    for v in q.values() if isinstance(v.get("pct"), (int, float)))
    lines = [f"# 宏观大势判断 · {d.get('day')}", "",
             f"**结论：{j['stance']}（总分 {j['total']:+d}）→ {j['position']}**", "",
             f"美联储：{d.get('fed')}", f"海外行情：{qs or 'n/a'}",
             f"宏观：PPI {(m.get('PPI') or {}).get('BASE_SAME', 'n/a')}%"
             f" / CPI {(m.get('CPI') or {}).get('NATIONAL_SAME', 'n/a')}%"
             f" / 制造业PMI {(m.get('PMI') or {}).get('MAKE_INDEX', 'n/a')}"
             f"（{(m.get('PMI') or {}).get('TIME', 'n/a')}）", ""]
    for name, sc, notes in j["layers"]:
        lines.append(f"## {name}（{sc:+d}）")
        lines += [f"- {n}" for n in notes] or ["- 数据缺失"]
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="忽略当日缓存重新拉取")
    args = ap.parse_args()
    d = collect(refresh=args.refresh)
    j = judge(d)
    print(render(d, j))


if __name__ == "__main__":
    main()
