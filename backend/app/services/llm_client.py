from __future__ import annotations
"""DeepSeek API client - 多 key 轮询 + 重试 + 限流退避。"""

import json
import asyncio
import httpx
import itertools

from app.config import get_settings

settings = get_settings()


def _get_api_keys() -> list[str]:
    """解析所有可用的 API key（支持多个，逗号分隔）。"""
    keys = []
    # 优先读多 key 配置
    if settings.deepseek_api_keys and settings.deepseek_api_keys.strip():
        keys.extend(k.strip() for k in settings.deepseek_api_keys.split(",") if k.strip())
    # 兼容单个 key 配置
    if settings.deepseek_api_key and settings.deepseek_api_key.strip():
        keys.append(settings.deepseek_api_key.strip())
    # 去重，保持顺序
    seen = set()
    result = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            result.append(k)
    return result or ["sk-placeholder"]


_api_keys = _get_api_keys()
_key_cycle = itertools.cycle(_api_keys)


def _next_key() -> str:
    """轮询取下一个 key（负载均衡）。"""
    return next(_key_cycle)


def _get_client(key: str) -> httpx.AsyncClient:
    """为指定 key 创建 HTTP 客户端。"""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(120.0, connect=30.0),
        follow_redirects=True,
        trust_env=False,  # 忽略系统代理
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )


async def _call_with_retry(
    body: dict,
    max_retries: int = 4,
) -> dict:
    """带重试的 API 调用：多 key 轮询 + 指数退避处理限流。"""
    last_error = None

    for attempt in range(max_retries):
        key = _next_key()
        try:
            async with _get_client(key) as client:
                response = await client.post(
                    f"{settings.deepseek_base_url}/v1/chat/completions",
                    json=body,
                )

                # 429 限流 → 指数退避后换 key 重试
                if response.status_code == 429:
                    last_error = f"rate_limited({key[:8]}...)"
                    wait = 2 ** attempt  # 2, 4, 8, 16 秒
                    await asyncio.sleep(wait)
                    continue

                # 其他非 200 错误 → 换 key 重试
                if response.status_code != 200:
                    last_error = f"http_{response.status_code}"
                    await asyncio.sleep(1)
                    continue

                data = response.json()
                return data

        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as e:
            last_error = f"network:{type(e).__name__}"
            await asyncio.sleep(2 ** attempt)
            continue
        except Exception as e:
            last_error = f"error:{type(e).__name__}"
            await asyncio.sleep(1)
            continue

    raise RuntimeError(f"LLM API 调用失败（重试{max_retries}次后）: {last_error}")


async def chat_completion(
    system_prompt: str,
    user_prompt: str,
    model: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    response_format: dict | None = None,
) -> str:
    """发送 chat completion 请求，返回文本。带多 key 轮询 + 重试。"""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    body = {
        "model": model or settings.ai_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    if response_format and response_format.get("type") == "json_object":
        body["response_format"] = response_format

    data = await _call_with_retry(body)
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"LLM 返回异常（无 choices）: {str(data)[:200]}")
    content = choices[0].get("message", {}).get("content")
    return (content or "").strip() or ""


async def chat_completion_json(
    system_prompt: str,
    user_prompt: str,
    model: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
) -> dict:
    """发送 chat completion 并解析 JSON。"""
    text = await chat_completion(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:]) if len(lines) > 1 else text
    if text.endswith("```"):
        text = text[:-3].strip()
    return json.loads(text)
