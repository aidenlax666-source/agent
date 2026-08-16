from __future__ import annotations
"""DeepSeek API client - 多 key 轮询 + 重试 + 限流退避。"""

import json
import asyncio
import httpx
import itertools

from app.config import get_settings

settings = get_settings()


_api_keys: list[str] | None = None
_key_cycle: itertools.cycle | None = None
_keys_sig: tuple = ()


def _ensure_keys() -> None:
    """按需初始化/刷新 key 池（配置变化后自动重建，不再模块导入时冻结）。"""
    global _api_keys, _key_cycle, _keys_sig
    s = get_settings()
    sig = (s.deepseek_api_keys, s.deepseek_api_key)
    if _api_keys is None or sig != _keys_sig:
        keys = []
        if s.deepseek_api_keys and s.deepseek_api_keys.strip():
            keys.extend(k.strip() for k in s.deepseek_api_keys.split(",") if k.strip())
        if s.deepseek_api_key and s.deepseek_api_key.strip():
            keys.append(s.deepseek_api_key.strip())
        seen = set()
        result = []
        for k in keys:
            if k not in seen:
                seen.add(k)
                result.append(k)
        if not result:
            raise RuntimeError("未配置 DEEPSEEK_API_KEY / DEEPSEEK_API_KEYS")
        _api_keys = result
        _key_cycle = itertools.cycle(result)
        _keys_sig = sig


def _next_key() -> str:
    """轮询取下一个 key（负载均衡）。"""
    _ensure_keys()
    return next(_key_cycle)  # type: ignore[arg-type]


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
    """带重试的 API 调用：多 key 轮询 + 指数退避处理限流。

    永久性 4xx（400/401/403/404）直接失败不重试；429 读取 Retry-After 退避。
    """
    last_error = None

    for attempt in range(max_retries):
        key = _next_key()
        try:
            async with _get_client(key) as client:
                response = await client.post(
                    f"{settings.deepseek_base_url}/v1/chat/completions",
                    json=body,
                )

                # 429 限流 → 读 Retry-After 退避后换 key 重试
                if response.status_code == 429:
                    last_error = f"rate_limited({key[:8]}...)"
                    wait = 2 ** attempt  # 2, 4, 8, 16 秒
                    try:
                        ra = float(response.headers.get("Retry-After", ""))
                        if 0 < ra <= 60:
                            wait = max(wait, ra)
                    except (TypeError, ValueError):
                        pass
                    await asyncio.sleep(wait)
                    continue

                # 永久性错误：重试必然失败，直接抛错
                if response.status_code in (400, 401, 403, 404):
                    raise RuntimeError(
                        f"LLM API 返回 {response.status_code}: {response.text[:200]}")

                # 其他非 200 错误 → 换 key 重试
                if response.status_code != 200:
                    last_error = f"http_{response.status_code}"
                    await asyncio.sleep(1)
                    continue

                data = response.json()
                return data

        except RuntimeError:
            raise
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
    """发送 chat completion 并解析 JSON（容错：剥离围栏/前缀，正则提取首个 JSON 对象）。"""
    text = await chat_completion(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    text = text.strip()
    # 剥离 ```json ... ``` 围栏（含语言标注、前后缀文本）
    if "```" in text:
        import re as _re
        m = _re.search(r"```(?:json)?\s*(.*?)```", text, _re.S)
        if m:
            text = m.group(1).strip()
    if not text:
        raise RuntimeError("LLM 返回空内容，无法解析 JSON")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 模型偶发输出带前缀/后缀：提取首个 {...} 块
        import re as _re
        m = _re.search(r"\{.*\}", text, _re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        raise RuntimeError(f"LLM 返回的不是有效 JSON: {text[:200]}")
