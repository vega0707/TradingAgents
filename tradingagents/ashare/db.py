#!/usr/bin/env python3
"""db.py — 本地行情落库（SQLite market.db）

减少外部依赖：拉到数据即本地落一份。表 kline(code,date,OHLCV)，
从 _cache/*.json 一次性迁移 + 之后增量 upsert。
用法: .venv/bin/python -m tradingagents.ashare.db migrate   # 迁移缓存
      .venv/bin/python -m tradingagents.ashare.db stats     # 统计
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "data" / "market.db"
CACHE = Path(__file__).resolve().parents[2] / "ashare_out" / "_cache"

SCHEMA = """
CREATE TABLE IF NOT EXISTS kline(
  code TEXT NOT NULL,
  date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, volume REAL,
  PRIMARY KEY(code, date));
CREATE INDEX IF NOT EXISTS idx_kline_code ON kline(code, date);
CREATE TABLE IF NOT EXISTS signal_case(
  code TEXT NOT NULL, name TEXT,
  prev_date TEXT, prev_action TEXT, cur_date TEXT, cur_action TEXT,
  days INTEGER, fundamental_changed INTEGER, kind TEXT DEFAULT 'flip',
  created TEXT DEFAULT (datetime('now','localtime')),
  PRIMARY KEY(code, cur_date, prev_date));
"""


def connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB))
    conn.executescript(SCHEMA)
    return conn


def upsert_kline(conn: sqlite3.Connection, code: str, rows: list[dict]) -> int:
    data = [(code, r["date"], r.get("open"), r.get("close"), r.get("high"),
             r.get("low"), r.get("volume")) for r in rows]
    conn.executemany(
        "INSERT OR REPLACE INTO kline(code,date,open,close,high,low,volume) "
        "VALUES(?,?,?,?,?,?,?)", data)
    conn.commit()
    return len(data)


def migrate_from_cache() -> None:
    conn = connect()
    total = 0
    files = 0
    for f in sorted(CACHE.glob("kline-*.json")):
        # kline-{sh600519}-{days}.json → code 去前缀
        stem = f.stem[len("kline-"):]
        code = stem.split("-")[0]
        if code[:2] in ("sh", "sz"):
            code = code[2:]
        try:
            blob = json.loads(f.read_text(encoding="utf-8"))
            rows = blob.get("data") or []
            if rows:
                n = upsert_kline(conn, code, rows)
                total += n
                files += 1
        except Exception as e:  # noqa: BLE001
            print(f"  {f.name} 失败: {e}")
    conn.close()
    print(f"迁移完成: {files} 文件 → {total} 根K线 (db={DB})")


def record_signal_case(code: str, name: str, prev_date: str, prev_action: str,
                       cur_date: str, cur_action: str, days: int,
                       fundamental_changed: bool = False) -> None:
    """反转入库：自我迭代案例库（recheck 检测到反转时调用）。"""
    try:
        conn = connect()
        conn.execute(
            "INSERT OR REPLACE INTO signal_case(code,name,prev_date,prev_action,"
            "cur_date,cur_action,days,fundamental_changed) VALUES(?,?,?,?,?,?,?,?)",
            (code, name, prev_date, prev_action, cur_date, cur_action, days,
             1 if fundamental_changed else 0))
        conn.commit()
        conn.close()
    except Exception:
        pass


def flip_history(code: str, asof: str, window_days: int = 90) -> int:
    """该票在 asof 之前 window_days 内的信号反转次数（run.py 一致性注入用）。"""
    try:
        conn = connect()
        n = conn.execute(
            "SELECT COUNT(*) FROM signal_case WHERE code=? AND cur_date < ? "
            "AND cur_date >= date(?, ?)",
            (code, asof, asof, f"-{window_days} day")).fetchone()[0]
        conn.close()
        return n
    except Exception:
        return 0


def stats() -> None:
    conn = connect()
    n = conn.execute("SELECT COUNT(*) FROM kline").fetchone()[0]
    codes = conn.execute("SELECT COUNT(DISTINCT code) FROM kline").fetchone()[0]
    span = conn.execute("SELECT MIN(date), MAX(date) FROM kline").fetchone()
    print(f"market.db: {codes} 只 / {n} 根K线 / 范围 {span[0]} ~ {span[1]}")
    conn.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "migrate":
        migrate_from_cache()
    else:
        stats()
