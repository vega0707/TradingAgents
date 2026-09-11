"""store.py — 云端主仓：Cloudflare D1（SQLite 语义，HTTP 直写，全球可达）。

配置（.env，勿提交）：
  CF_API_TOKEN=...        D1 可用 token（cfat_ 前缀）
  CF_D1_ACCOUNT=...       Cloudflare 账号 id
  CF_D1_DB=...            D1 数据库 uuid（本仓创建: ashare-records）

写失败不致命：调用方把 record 落本地 pending，等网络恢复再 flush。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
  ticker        TEXT NOT NULL,
  name          TEXT NOT NULL DEFAULT '',
  as_of         TEXT NOT NULL,
  decided_on    TEXT NOT NULL,
  is_etf        INTEGER NOT NULL DEFAULT 0,
  fundamentals_ok INTEGER NOT NULL DEFAULT 0,
  mark          REAL,
  cost          REAL,
  shares        REAL,
  benchmark     TEXT,
  benchmark_mark REAL,
  horizons      TEXT NOT NULL DEFAULT '[5,20]',
  masters       TEXT NOT NULL DEFAULT '[]',
  manager       TEXT NOT NULL DEFAULT '{}',
  trader        TEXT NOT NULL DEFAULT '{}',
  failures      TEXT NOT NULL DEFAULT '[]',
  llm_tier      TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL,
  PRIMARY KEY (ticker, as_of)
)
"""


class CloudStoreUnavailable(RuntimeError):
    pass


class D1Store:
    """极薄封装：单条 upsert。HTTP 偶发失败抛 CloudStoreUnavailable。"""

    def __init__(self, token: str | None = None, account: str | None = None,
                 db_id: str | None = None, base: str = "https://api.cloudflare.com/client/v4"):
        self.token = token or os.environ.get("CF_API_TOKEN", "")
        self.account = account or os.environ.get("CF_D1_ACCOUNT", "")
        self.db_id = db_id or os.environ.get("CF_D1_DB", "")
        self.base = base
        if not (self.token and self.account and self.db_id):
            raise CloudStoreUnavailable("CF_D1_* 未配置")

    def _query(self, sql: str, params: list) -> dict:
        url = f"{self.base}/accounts/{self.account}/d1/database/{self.db_id}/query"
        body = json.dumps({"sql": sql, "params": params}).encode()
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise CloudStoreUnavailable(f"D1 HTTP {exc.code}: {exc.read()[:200]!r}") from exc
        except Exception as exc:
            raise CloudStoreUnavailable(str(exc)) from exc

    def init_schema(self) -> None:
        for stmt in _SCHEMA.split(";"):
            if stmt.strip():
                self._query(stmt, [])

    def upsert(self, r: dict) -> bool:
        """按 (ticker, as_of) upsert 一条决策记录；成功返回 True。"""
        sql = (
            "INSERT INTO records (ticker,name,as_of,decided_on,is_etf,fundamentals_ok,"
            "mark,cost,shares,benchmark,benchmark_mark,horizons,masters,manager,trader,"
            "failures,llm_tier,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(ticker, as_of) DO UPDATE SET mark=excluded.mark,"
            "decided_on=excluded.decided_on,manager=excluded.manager,trader=excluded.trader,"
            "masters=excluded.masters,failures=excluded.failures,created_at=excluded.created_at"
        )
        j = json.dumps
        self._query(sql, [
            r["ticker"], r.get("name", ""), r["as_of"], r["decided_on"],
            1 if r.get("is_etf") else 0, 1 if r.get("fundamentals_ok") else 0,
            r.get("mark"), r.get("cost"), r.get("shares"), r.get("benchmark"),
            r.get("benchmark_mark"), j(r.get("horizons", [5, 20])),
            j(r.get("masters", [])), j(r.get("manager", {})), j(r.get("trader", {})),
            j(r.get("failures", [])), j(r.get("llm_tier", {})), r["created_at"],
        ])
        return True

    def fetch(self, sql: str, params: list | None = None) -> list[dict]:
        resp = self._query(sql, params or [])
        rows = (resp.get("result") or [{}])[0].get("results", [])
        return rows
