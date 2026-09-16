#!/usr/bin/env python3
"""proxy.py — 经腾讯云出口转发 akshare 请求（SOCKS5 over ssh）

背景（2026-09-16 实测）：
  公司网络出口访问东财 push2 等节点长期不通 —— akshare 连续四天
  RemoteDisconnected；而腾讯云服务器走公有云网络可正常访问，同一批接口在
  腾讯云上全部秒级成功（单票信息 0.3s / 涨停池 0.1s / 财务摘要 0.6s）。
  本地经 `ssh -D` 隧道（出口=腾讯云）调用 akshare 也能成功（涨停池 0.3s）。

日常链路（早盘简报 / 持仓分析 / 四层漏斗）已全部改为**东财直连**，不需要
本模块。它服务于"必须用 akshare 的场景"——东财没有直连实现的接口，例如
涨停池（`ak.stock_zt_pool_em`，用于 bt_zt_nextday.py 的短线回测）。

用法::

    from tradingagents.ashare.proxy import ensure_tx_tunnel, tx_env
    ensure_tx_tunnel()                 # 已通则复用，否则新建（幂等）
    os.environ.update(tx_env())        # requests/akshare 之后都走隧道
    import akshare as ak               # 此时请求经腾讯云出口

环境变量：TX_SSH_KEY（默认 /Users/vega/git/steel/servers/tx.pem）、
TX_HOST（默认 ubuntu@124.221.95.205）、TX_SOCKS_PORT（默认 11080，避开常用 1080）。
"""
from __future__ import annotations

import os
import socket
import subprocess
import time
from contextlib import contextmanager

DEFAULT_KEY = "/Users/vega/git/steel/servers/tx.pem"
DEFAULT_HOST = "ubuntu@124.221.95.205"
DEFAULT_PORT = 11080


def _key() -> str:
    return os.environ.get("TX_SSH_KEY", DEFAULT_KEY)


def _host() -> str:
    return os.environ.get("TX_HOST", DEFAULT_HOST)


def _port() -> int:
    return int(os.environ.get("TX_SOCKS_PORT", DEFAULT_PORT))


def port_open(port: int | None = None, host: str = "127.0.0.1", timeout: float = 1.5) -> bool:
    """本地隧道端口是否已在监听。"""
    p = port or _port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, p)) == 0


def ensure_tx_tunnel(rebuild: bool = False, wait: float = 8.0) -> bool:
    """确保到腾讯云的 SOCKS5 隧道可用（幂等）。返回是否可用。

    已监听且 rebuild=False 时直接复用——不会重复建连。
    不抛异常：调用方按返回值决定是否降级（返回 False 时 akshare 大概率不可用）。
    """
    if port_open() and not rebuild:
        return True
    if rebuild:
        subprocess.run(["pkill", "-f", f"ssh -D {_port()} "], capture_output=True)
        time.sleep(1)
    key = _key()
    if not os.path.isfile(key):
        return False
    cmd = [
        "ssh", "-D", str(_port()), "-N", "-f", "-i", key,
        "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
        "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=15", _host(),
    ]
    subprocess.run(cmd, capture_output=True)
    deadline = time.time() + wait
    while time.time() < deadline:
        if port_open():
            return True
        time.sleep(0.5)
    return False


def tx_env() -> dict[str, str]:
    """给 requests/akshare 用的代理环境变量（socks5h：DNS 也在远端解析）。

    注意：**不要**全局设置——urllib 读到 socks5h:// 会直接抛错（标准库不支持
    socks），而本仓大量行情请求走 urllib 直连新浪/腾讯。只给 akshare 调用用，
    见 tx_proxy()。
    """
    url = f"socks5h://127.0.0.1:{_port()}"
    return {"HTTPS_PROXY": url, "HTTP_PROXY": url, "ALL_PROXY": url}


@contextmanager
def tx_proxy():
    """with 块内让 requests/akshare 走腾讯云隧道，退出后恢复原环境。

    用法::

        with tx_proxy():
            df = ak.stock_zt_pool_em(date=day)   # 只有这里走隧道

    urllib 的直连请求（新浪/腾讯行情）不受影响。
    """
    ensure_tx_tunnel()
    keys = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.update(tx_env())
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def without_proxy_env() -> dict[str, str]:
    """清空代理（切回直连，含大写与小写变体）。"""
    return {k: "" for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
                            "https_proxy", "http_proxy", "all_proxy")}


if __name__ == "__main__":   # 冒烟：python -m tradingagents.ashare.proxy
    ok = ensure_tx_tunnel()
    print("隧道可用:" if ok else "隧道不可用", f"port={_port()}")
    if ok:
        os.environ.update(tx_env())
        try:
            import akshare as ak
            t0 = time.time()
            d = ak.stock_zt_pool_em(date=time.strftime("%Y%m%d"))
            print(f"经隧道取当日涨停池: {len(d)} 行 ({time.time() - t0:.1f}s)")
        except Exception as exc:  # noqa: BLE001
            print("akshare 调用失败:", type(exc).__name__, str(exc)[:80])
