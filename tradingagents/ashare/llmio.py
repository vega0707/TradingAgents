"""llmio.py — 本流水线统一 LLM 出入口（复用 TradingAgents 客户端/网关）。

角色分两档模型（沿用 TA 设定：分析师/辩手/交易员=quick，研究员=deep）：
  quick -> TRADINGAGENTS_QUICK_THINK_LLM（.env，luna）
  deep  -> TRADINGAGENTS_DEEP_THINK_LLM（.env，terra）
JSON 解析带 fence 剥离与一次纯文本重试，仍失败则抛 LLMCallError（外层决定
abstain 还是整体失败——分析师可弃权，交易员/研究员失败必须整票重试）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients.factory import create_llm_client

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_clients: dict[str, object] = {}
# 单次调用超时/重试（云端上游偶发 300s+ 卡死，必须兜底，宁缺毋滥）
_LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "240"))
_LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "1"))


def _client(tier: str):
    model = DEFAULT_CONFIG["quick_think_llm"] if tier == "quick" else DEFAULT_CONFIG["deep_think_llm"]
    with _lock:
        if model not in _clients:
            _clients[model] = create_llm_client(
                DEFAULT_CONFIG["llm_provider"], model,
                base_url=DEFAULT_CONFIG["backend_url"],
                timeout=_LLM_TIMEOUT, max_retries=_LLM_MAX_RETRIES,
            ).get_llm()
        return _clients[model]


class LLMCallError(RuntimeError):
    pass


def complete(tier: str, system: str, user: str, max_tokens: int = 2600) -> str:
    """一次调用，返回文本；传输/网关失败抛 LLMCallError。"""
    try:
        resp = _client(tier).invoke([("system", system), ("human", user)])
        # 兼容：标准 AIMessage 取 content；异常返回对象取纯文本
        if hasattr(resp, "content") and isinstance(resp.content, str):
            return resp.content
        return str(resp)
    except Exception as exc:
        raise LLMCallError(f"LLM {tier} call failed: {exc}") from exc


def complete_json(tier: str, system: str, user: str, max_tokens: int = 2600) -> dict:
    """要求模型只输出 JSON；解析失败重试一次（补一句“只输出 JSON”）。”

    仍失败 -> LLMCallError。
    """
    last_err: Exception | None = None
    for attempt in (system, system + "\n再次提醒：只输出一个 JSON 对象，不要任何解释或 Markdown 代码块。"):
        text = complete(tier, attempt, user, max_tokens)
        try:
            return _extract_json(text)
        except Exception as exc:
            last_err = exc
    raise LLMCallError(f"unparseable JSON after retry: {last_err}")


def _extract_json(text: str) -> dict:
    """取文本中第一个完整 JSON 对象；容忍围栏、前后缀、多余尾部内容。

    部分模型（如 gemma 系）输出单引号/python 风格 dict → json 解析失败时用
    ast.literal_eval 兜底（本流程字段只有中文串/数字/枚举，安全）。
    """
    import ast
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in response")
    decoder = json.JSONDecoder()
    try:
        data, _ = decoder.raw_decode(text[start:])
    except Exception:
        # 单引号兜底：按花括号深度截取第一个对象
        depth, end = 0, None
        in_str = False
        for i, ch in enumerate(text[start:]):
            if ch in "\"'":
                in_str = not in_str
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = start + i + 1
                    break
        if end is None:
            raise ValueError("unbalanced JSON object")
        data = ast.literal_eval(text[start:end])
    if not isinstance(data, dict):
        raise ValueError("JSON root is not an object")
    return data
