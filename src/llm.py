# -*- coding: utf-8 -*-
"""LLM 客户端封装 —— DeepSeek（OpenAI 兼容协议）。

设计要点：
1. 统一 JSON 输出 + 解析容错（模型偶尔会包一层 ```json 代码块）
2. 失败自动重试 + 指数退避
3. 密钥只从环境变量读，绝不写死在代码里
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DEFAULT_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
DEFAULT_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

_client = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "未找到 DEEPSEEK_API_KEY。请在项目根目录创建 .env，写入：\n"
                "DEEPSEEK_API_KEY=sk-xxxx\n"
                "DEEPSEEK_BASE_URL=https://api.deepseek.com\n"
                "LLM_MODEL=deepseek-chat"
            )
        _client = OpenAI(api_key=key, base_url=DEFAULT_BASE_URL, timeout=90)
    return _client


def _strip_code_fence(text: str) -> str:
    """去掉模型可能加上的 ```json ... ``` 包裹。"""
    text = (text or "").strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    return m.group(1).strip() if m else text


def chat_json(system: str, user: str, *, model: str | None = None,
              temperature: float = 0.2, max_tokens: int = 3000,
              retries: int = 3) -> dict:
    """调用 LLM 并强制解析为 JSON dict。失败重试，最终失败抛异常。"""
    client = get_client()
    use_model = model or DEFAULT_MODEL
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = client.chat.completions.create(
                model=use_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content or ""
            data = json.loads(_strip_code_fence(raw))
            if not isinstance(data, dict):
                raise ValueError(f"期望 JSON 对象，实际得到 {type(data).__name__}")
            return data
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < retries:
                time.sleep(1.5 * attempt)
    raise RuntimeError(f"LLM 调用失败（已重试 {retries} 次）：{last_err}")


def last_usage() -> dict:
    """占位：如需统计 token 消耗可在此接入。"""
    return {}
