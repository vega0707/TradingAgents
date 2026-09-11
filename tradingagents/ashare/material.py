"""material.py — A 股单票的「材料包」：让每个角色看到同一份事实，谁也不许编。

数据来源（全部免费、本机已验证可达）：
- 基本面：本仓 vendor 的 point-in-time 快照（data.AkshareDataClient +
  snapshot.build_snapshot，含披露日 PIT 过滤），源出 ai-hedge-fund hedge_fund.data，
  已独立解耦（见 data/__init__.py 与 closed_loop_handover.md）。
- 行情/技术面：新浪日 K JSON（https://quotes.sina.cn/cn/api/json_v2.php/...）
  直连，只取 as_of 当日及之前的收盘（防未来泄漏）。
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

_SINA_KLINE_URL = (
    "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
)
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126 Safari/537.36")

# 磁盘缓存：行情当日有效（价格日变，当日首次取价即定格）；基本面按报告期窗口
# （财报季报才变，21 天内同 ticker 不重复拉财务——请求量降 95%，抗源限流）。
_CACHE_DIR = Path("ashare_out") / "_cache"
SNAPSHOT_TTL_DAYS = 21


def _cache_load(kind: str, key: str, ttl_days: int = 1) -> object | None:
    """ttl 内命中返回缓存，过期/缺失返回 None。kind 防 key 冲突。"""
    p = _CACHE_DIR / f"{kind}-{key}.json"
    if not p.is_file():
        return None
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
        saved = date.fromisoformat(blob["day"])
    except (OSError, ValueError, KeyError):
        return None
    if (date.today() - saved).days >= ttl_days:
        return None
    return blob.get("data")


def _cache_save(kind: str, key: str, data: object, ttl_days: int = 1) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = _CACHE_DIR / f"{kind}-{key}.json"
        p.write_text(
            json.dumps({"day": date.today().isoformat(), "data": data},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass  # 缓存失败不阻断主流程（下次会重新拉取）

# 上证 6 开头、深证 0/3 开头；ETF：5 开头沪市、1 开头深市。
def _prefix(ticker: str) -> str:
    if ticker.startswith(("5", "6", "9")):
        return "sh" + ticker
    return "sz" + ticker


def is_etf(ticker: str) -> bool:
    return len(ticker) == 6 and ticker[0] in "15"


def _http_json(url: str, timeout: int = 10, retries: int = 3) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Referer": "https://finance.sina.com.cn"})
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001 — 新浪偶发断连/频控，退避重试
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"http fetch failed after {retries} tries: {url} :: {last_err}")


def fetch_daily_kline(ticker: str, days: int = 260) -> list[dict]:
    """新浪日 K（前复权?不——新浪 getKLineData 为不复权原始价，够用）。

    当日磁盘缓存：同一天同一 ticker 只请求一次（盘后价不变，盘中以当日首次为准）。
    """
    return _kline_cached(_prefix(ticker), days)


def fetch_symbol_kline(symbol: str, days: int = 400) -> list[dict]:
    """按新浪符号拉日K（带前缀，如指数 'sh000300'）。当日缓存同上。"""
    return _kline_cached(symbol, days)


def _kline_cached(symbol: str, days: int) -> list[dict]:
    cached = _cache_load("kline", f"{symbol}-{days}")
    if cached is not None:
        return [dict(r) for r in cached]
    url = f"{_SINA_KLINE_URL}?symbol={symbol}&scale=240&ma=no&datalen={days}"
    rows = _http_json(url)
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"sina kline empty for {symbol}")
    out = [
        {"date": r["day"], "open": float(r["open"]), "close": float(r["close"]),
         "high": float(r["high"]), "low": float(r["low"]), "volume": float(r["volume"])}
        for r in rows
    ]
    _cache_save("kline", f"{symbol}-{days}", out)
    return out


def fetch_symbol_close(symbol: str, as_of: str) -> float | None:
    """任意新浪符号（如指数 'sh000300'）截至 as_of 的收盘；取不到返回 None。

    供 record 的 benchmark_mark 使用——只取 as_of 当日及之前的 bar。
    """
    try:
        rows = fetch_symbol_kline(symbol, 300)  # 带当日缓存
    except Exception:
        return None
    for r in reversed(rows):
        if r["date"] <= as_of:
            try:
                return float(r["close"])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def _pct(cur: float, prev: float | None) -> str:
    return "n/a" if not prev else f"{(cur / prev - 1) * 100:+.1f}%"


def render_price_section(ticker: str, as_of: str) -> tuple[str, dict]:
    """截至 as_of 收盘的行情/技术面中文摘要 + {mark, date} 供交易员/卡片用。

    只统计 close.date <= as_of 的样本；as_of 早于数据或交易日当日盘中，
    取 <= as_of 的最后一根（周末/节假日自动回退上一交易日）。
    """
    rows = [r for r in fetch_daily_kline(ticker) if r["date"] <= as_of]
    if not rows:
        raise RuntimeError(f"no kline <= {as_of} for {ticker}")
    closes = [r["close"] for r in rows]
    mark = closes[-1]
    d_last = rows[-1]["date"]
    n = len(closes)

    def ref(k: int) -> float | None:
        return closes[-k - 1] if n > k else None

    win60 = closes[-60:] if n >= 60 else closes
    hi60, lo60 = max(win60), min(win60)
    ma20 = sum(closes[-20:]) / min(20, n)
    ma60 = sum(closes[-60:]) / min(60, n)
    pos60 = (mark - lo60) / (hi60 - lo60) * 100 if hi60 > lo60 else 50.0

    lines = [
        f"行情（截至 {d_last} 收盘，日K 不复权）：",
        f"  最新收盘 {mark:.2f} ｜ 距60日高 {hi60:.2f} 回撤 {_pct(mark, hi60)}"
        f" ｜ 距60日低 {lo60:.2f} 上方 {_pct(mark, lo60)}",
        f"  近5日 {_pct(mark, ref(4))} ｜ 近20日 {_pct(mark, ref(19))}"
        f" ｜ 近60日 {_pct(mark, ref(59))}",
        f"  MA20 {ma20:.2f} ｜ MA60 {ma60:.2f} ｜ 现价{'站上' if mark >= ma20 else '跌破'}MA20"
        f"、{'站上' if mark >= ma60 else '跌破'}MA60",
        f"  60日区间位置 {pos60:.0f}%（0=区间底 100=区间顶）",
    ]
    return "\n".join(lines), {"mark": mark, "date": d_last}


@dataclass
class Material:
    ticker: str
    name: str
    as_of: str          # 请求日期（YYYY-MM-DD），分析截至该日收盘
    is_etf: bool
    fundamentals: str   # 中文快照文本；不可用时说明原因
    price_text: str
    mark: float | None
    fundamentals_ok: bool
    prev_note: str = ""  # 上次判断上下文（复查防日度噪声翻转），空=首析

    def render(self) -> str:
        head = f"标的：{self.name} {self.ticker} ｜ 分析截至 {self.as_of}（收盘）\n"
        body = head + "\n" + self.price_text + "\n\n" + self.fundamentals
        if self.prev_note:
            body += "\n\n" + self.prev_note
        return body


def build_material(ticker: str, as_of: str, name: str = "") -> Material:
    """ticker 形如 601318；name 可传中文名（没有则用 'T'+code 占位）。"""
    etf = is_etf(ticker)

    # 行情先行：拿不到行情直接失败（卡片的现价是硬事实）。
    price_text, meta = render_price_section(ticker, as_of)

    fundamentals, ok = "", False
    if not etf:
        # 当日缓存：同一 ticker 同一天重复 build（重跑/对账）不再全量拉 akshare
        # 快照缓存按 ticker（跨 as_of 共享——render 故意 date-free，同一报告期
        # 任何 as_of 文本一致）；21 天窗口内不重复拉财务（财报季报才变）
        cached = _cache_load("snapshot", ticker, ttl_days=SNAPSHOT_TTL_DAYS)
        if cached is not None:
            return Material(
                ticker=ticker, name=name or f"T{ticker}", as_of=as_of, is_etf=etf,
                fundamentals=cached["fundamentals"], price_text=price_text,
                mark=meta["mark"], fundamentals_ok=bool(cached["ok"]),
            )
        try:
            from tradingagents.ashare.data import AkshareDataClient
            from tradingagents.ashare.snapshot import build_snapshot as bs
            snap = bs(ticker, as_of, AkshareDataClient())
            fundamentals = snap.render()
            ok = True
        except Exception as exc:  # InsufficientData / 网络 / 源拒绝
            fundamentals = (
                f"基本面快照不可用（{type(exc).__name__}: {str(exc)[:200]}）。"
                "若凭现有信息无法按你的方法判断，请输出 abstain。"
            )
        if ok:  # 只缓存成功快照；失败可重试，不被坏缓存挡住
            _cache_save("snapshot", ticker,
                        {"ok": True, "fundamentals": fundamentals},
                        ttl_days=SNAPSHOT_TTL_DAYS)
    else:
        fundamentals = (
            f"{ticker} 为 ETF/指数基金，无个股基本面（财务报表/估值不适用）。"
            "请勿按个股基本面方法（ROE、市盈率、账面价值…）评价，也勿编造其成分股数据；"
            "如无观点可 abstain。价格与趋势信息见上方行情段。"
        )

    return Material(
        ticker=ticker, name=name or f"T{ticker}", as_of=as_of, is_etf=etf,
        fundamentals=fundamentals, price_text=price_text, mark=meta["mark"],
        fundamentals_ok=ok,
    )


def latest_trade_date(as_of: str | None = None) -> str:
    """最近一个自然日（调用方在非交易日会由 kline 自动回退），保持简单。"""
    d = date.fromisoformat(as_of) if as_of else date.today()
    return d.isoformat()
