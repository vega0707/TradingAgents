"""records.py — 决策的「可对账记录」：写本地 record.json + 推云端主仓(D1)。

铁律（handover v1 阶段1）：
- record 只含 as_of 当日及之前可得的信息，永不回填未来价格。
- 同 ticker + 同 as_of 的目录已有 record.json → 拒绝覆盖，须 --force。
- 云端写失败 → 落本地 ashare_out/_pending/，不阻塞当日产出。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime
from pathlib import Path

from tradingagents.ashare.material import Material
from tradingagents.ashare.store import CloudStoreUnavailable, D1Store
from tradingagents.ashare.team import TeamRecord

PENDING_DIR = Path("ashare_out/_pending")
HORIZONS = [5, 20]          # 交易日，固定常量（防挑 horizon 自欺）


def build_record(mat: Material, rec: TeamRecord, shares: float | None,
                 cost: float | None, benchmark_mark: float | None,
                 llm_tier: dict | None = None) -> dict:
    """从一次团队产出构造对账记录（字段口径与 handover schema 对齐）。"""
    return {
        "version": 1,
        "ticker": mat.ticker,
        "name": mat.name,
        "as_of": mat.as_of,
        "decided_on": date.today().isoformat(),
        "is_etf": mat.is_etf,
        "fundamentals_ok": mat.fundamentals_ok,
        "mark": mat.mark,
        "cost": cost,
        "shares": shares,
        "benchmark": "000300",
        "benchmark_mark": benchmark_mark,
        "horizons": HORIZONS,
        "masters": [
            {"master": v["master"], "signal": v["signal"], "conviction": v["conviction"]}
            for v in rec.views
        ],
        "manager": {"recommendation": rec.manager.get("recommendation"),
                    "confidence": rec.manager.get("confidence")},
        "trader": {"action": rec.trader.get("action")},
        "failures": rec.failures,
        "llm_tier": llm_tier or {},
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def _out_dir(ticker: str, as_of: str) -> Path:
    return Path("ashare_out") / f"{ticker}-{as_of}"


def record_exists(ticker: str, as_of: str) -> bool:
    return (_out_dir(ticker, as_of) / "record.json").exists()


def write_record_local(record: dict, force: bool = False) -> Path:
    """原子写 ashare_out/<ticker>-<as_of>/record.json；已存在且非 force 则拒绝。"""
    out = _out_dir(record["ticker"], record["as_of"])
    target = out / "record.json"
    if target.exists() and not force:
        raise FileExistsError(
            f"{target} 已存在：同一 ticker+as_of 的决策非确定性，重复跑会污染样本。"
            "确需重跑请加 --force（原记录将覆盖）。")
    out.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=1)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return target


def push_cloud(record: dict) -> tuple[str, bool]:
    """推云端主仓；返回 (状态说明, 成功与否)。失败落 pending 文件。"""
    try:
        store = D1Store()
        store.init_schema()
        store.upsert(record)
        return "cloud:ok", True
    except CloudStoreUnavailable as exc:
        PENDING_DIR.mkdir(parents=True, exist_ok=True)
        pending = PENDING_DIR / f"{record['ticker']}-{record['as_of']}.json"
        pending.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        return f"cloud:unavailable → 已存 pending {pending}（{str(exc)[:80]}）", False


def flush_pending() -> int:
    """重试推送 pending 目录里的积压记录。返回成功条数。"""
    store = D1Store()
    store.init_schema()
    done = 0
    for p in sorted(PENDING_DIR.glob("*.json")):
        rec = json.loads(p.read_text(encoding="utf-8"))
        store.upsert(rec)
        p.unlink()
        done += 1
    return done
