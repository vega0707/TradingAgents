"""run_holdings.py — 18 只持仓批跑（模拟 run_daily_tx.sh 的逐只循环）。

  逐只调 tradingagents.ashare.run 子进程（隔离：单只崩溃不影响其余），
  产物/记录/D1 推送与单票一致。持仓清单优先读 ai-hedge-fund 的
  config/tickers.yaml（云端权威来源），结构 {code,name,shares,cost}。

用法:
  python -m tradingagents.ashare.run_holdings [--tickers config/tickers.yaml]
      [--rounds 1] [--force] [--no-cloud] [--no-scoreboard]
      [--skip etf] [--only 601318,512880]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import yaml


def load_holdings(path: str) -> list[dict]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return [
        {"code": t["code"], "name": t.get("name", ""),
         "shares": float(t.get("shares") or 0), "cost": t.get("cost") or None}
        for t in data.get("tickers", [])
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default="config/tickers.yaml")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--only", default=None, help="逗号分隔子集")
    ap.add_argument("--skip-etf", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-cloud", action="store_true")
    ap.add_argument("--no-scoreboard", action="store_true")
    args = ap.parse_args()

    holdings = load_holdings(args.tickers)
    if args.only:
        keep = set(args.only.split(","))
        holdings = [h for h in holdings if h["code"] in keep]
    if args.skip_etf:
        holdings = [h for h in holdings if not h["code"][0] in "15"]

    print(f"批跑 {len(holdings)} 只，rounds={args.rounds}")
    t0 = time.time()
    results: list[dict] = []
    for i, h in enumerate(holdings, 1):
        code, name, shares, cost = h["code"], h["name"], h["shares"], h["cost"]
        print(f"\n[{i}/{len(holdings)}] {name} {code}", flush=True)
        cmd = [sys.executable, "-m", "tradingagents.ashare.run",
               "--ticker", code, "--name", name]
        if shares:
            cmd += ["--shares", str(int(shares))]
        if cost:
            cmd += ["--cost", str(cost)]
        cmd += ["--rounds", str(args.rounds)]
        if args.force:
            cmd.append("--force")
        if args.no_cloud:
            cmd.append("--no-cloud")
        if args.no_scoreboard:
            cmd.append("--no-scoreboard")
        r = subprocess.run(cmd, capture_output=True, text=True)
        ok = r.returncode == 0 and ("已保存" in r.stdout or "record.json" in r.stdout)
        results.append({"code": code, "name": name, "ok": ok,
                        "tail": (r.stdout + r.stderr)[-200:]})
        if not ok:
            print(f"  ✗ 失败：{(r.stdout + r.stderr)[-300:]}", flush=True)

    done = sum(1 for x in results if x["ok"])
    print(f"\n批跑完成：{done}/{len(results)} 成功，耗时 {time.time()-t0:.0f}s")
    if done < len(results):
        for x in results:
            if not x["ok"]:
                print(" 失败:", x["code"], x["name"], x["tail"][-150:])
    sys.exit(0 if done == len(results) else 1)


if __name__ == "__main__":
    main()
