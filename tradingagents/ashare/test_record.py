"""test_record.py — 记录层离线单测（不联网、不调 LLM）。

覆盖：schema 完整性 / mark-as_of 一致性 / 无未来字段 / 防覆盖 / force 覆盖。
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # noqa: E402

from tradingagents.ashare.material import Material
from tradingagents.ashare.records import build_record, write_record_local
from tradingagents.ashare.team import TeamRecord


def _mat(as_of="2026-09-04") -> Material:
    return Material(ticker="601318", name="中国平安", as_of=as_of, is_etf=False,
                    fundamentals="基本面\nPE 6.3", price_text="行情",
                    mark=58.24, fundamentals_ok=True)


def _rec() -> TeamRecord:
    views = [
        {"master": "graham", "signal": "bullish", "conviction": 70, "thesis": "x", "risks": "y"},
        {"master": "burry", "signal": "bearish", "conviction": 55, "thesis": "a", "risks": "b"},
    ]
    debate = [{"side": "bull", "round": 1, "text": "b1"},
              {"side": "bear", "round": 1, "text": "k1"}]
    return TeamRecord(views=views, debate=debate,
                      manager={"recommendation": "增持", "confidence": 60},
                      trader={"action": "加仓"}, failures=[])


def test_schema_complete_and_type_consistent():
    r = build_record(_mat(), _rec(), 200, 35.98, benchmark_mark=4567.0,
                     llm_tier={"quick": "q", "deep": "d"})
    assert r["version"] == 1
    assert r["ticker"] == "601318" and r["as_of"] == "2026-09-04"
    assert r["mark"] == 58.24
    assert r["decided_on"] >= r["as_of"]          # 决策日不早于数据日
    assert r["cost"] == 35.98 and r["shares"] == 200
    assert r["benchmark"] == "000300"
    assert r["benchmark_mark"] == 4567.0
    assert r["horizons"] == [5, 20]
    assert r["masters"] == [
        {"master": "graham", "signal": "bullish", "conviction": 70},
        {"master": "burry", "signal": "bearish", "conviction": 55},
    ]
    assert r["manager"] == {"recommendation": "增持", "confidence": 60}
    assert r["trader"] == {"action": "加仓"}
    assert r["is_etf"] is False and r["fundamentals_ok"] is True


def test_etf_flag_and_null_benchmark(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    etf = Material(ticker="512880", name="证券ETF", as_of="2026-09-04", is_etf=True,
                   fundamentals="ETF", price_text="行情", mark=1.105, fundamentals_ok=False)
    r = build_record(etf, _rec(), 300, 1.06, benchmark_mark=None, llm_tier={})
    assert r["is_etf"] is True and r["fundamentals_ok"] is False
    assert r["benchmark_mark"] is None


def test_local_write_atomic_and_overwrite_guard(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mat, rec = _mat(), _rec()
    r1 = build_record(mat, rec, None, None, None, {})
    p = write_record_local(r1)
    assert p.exists() and p.name == "record.json"
    assert json.loads(p.read_text(encoding="utf-8"))["mark"] == 58.24
    assert list(p.parent.glob("*.tmp")) == []   # 原子写后无残留临时文件

    # 防覆盖：不带 force 拒绝
    with pytest.raises(FileExistsError):
        write_record_local(r1)
    # force：覆盖成功且内容更新
    mat2 = _mat(as_of="2026-09-04")
    r2 = build_record(mat2, _rec(), 100, 50.0, None, {})
    r2["mark"] = 60.0
    write_record_local(r2, force=True)
    assert json.loads(p.read_text(encoding="utf-8"))["mark"] == 60.0


def test_no_future_dates_in_record():
    r = build_record(_mat("2026-09-04"), _rec(), None, None, None, {})
    assert date.fromisoformat(r["as_of"]) <= date.today()
    for key in ("decided_on", "created_at"):
        assert key in r
