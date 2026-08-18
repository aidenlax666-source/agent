from __future__ import annotations
"""LLM 客户端：多提供商（DeepSeek/OpenAI/Anthropic/Ollama/自定义）+ 多 key 轮询 + 重试 + 限流退避。

所有提供商统一走 OpenAI 兼容 /chat/completions（Anthropic 用 /v1/messages 单独适配）。
"""

import json
import asyncio
import httpx
import itertools

from app.config import get_settings

settings = get_settings()


def _provider_config() -> dict:
    """返回当前提供商的 (base_url, api_key, default_model)。"""
    s = get_settings()
    p = (s.llm_provider or "deepseek").strip().lower()
    if p == "openai":
        return {"base_url": s.openai_base_url, "key": s.openai_api_key, "model": s.openai_model,
                "endpoint": "/chat/completions", "kind": "openai"}
    if p == "anthropic":
        return {"base_url": s.openai_base_url, "key": s.anthropic_api_key, "model": s.anthropic_model,
                "endpoint": "/messages", "kind": "anthropic"}
    if p == "ollama":
        return {"base_url": s.ollama_base_url, "key": "", "model": s.ollama_model,
                "endpoint": "/chat/completions", "kind": "openai"}
    if p == "custom":
        return {"base_url": s.custom_openai_base_url, "key": s.custom_openai_key, "model": s.custom_openai_model,
                "endpoint": "/chat/completions", "kind": "openai"}
    # deepseek（默认）
    return {"base_url": s.deepseek_base_url, "key": "", "model": s.ai_model,
            "endpoint": "/v1/chat/completions", "kind": "openai"}


_api_keys: list[str] | None = None
_key_cycle: itertools.cycle | None = None
_keys_sig: tuple = ()


def _ensure_keys() -> None:
    """按需初始化/刷新 key 池（配置变化后自动重建，不再模块导入时冻结）。"""
    global _api_keys, _key_cycle, _keys_sig
    s = get_settings()
    cfg = _provider_config()
    if cfg["kind"] == "anthropic":
        # Anthropic 单 key
        sig = ("anthropic", cfg["key"], cfg["base_url"])
        if _api_keys is None or sig != _keys_sig:
            _api_keys = [cfg["key"]] if cfg["key"] else []
            _key_cycle = itertools.cycle(_api_keys or ["no-key"])
            _keys_sig = sig
        if not _api_keys:
            raise RuntimeError("未配置 ANTHROPIC_API_KEY")
        return
    if cfg["kind"] != "openai" or cfg["key"]:
        # 非 deepseek 或自定义 key：单 key
        sig = (s.llm_provider, cfg.get("key", ""), cfg.get("base_url", ""))
        key = cfg.get("key") or s.deepseek_api_key
        if _api_keys is None or sig != _keys_sig:
            _api_keys = [key] if key else []
            _key_cycle = itertools.cycle(_api_keys or ["no-key"])
            _keys_sig = sig
        if not _api_keys:
            raise RuntimeError("未配置 LLM API KEY")
        return
    # DeepSeek：多 key 轮询
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


def _resolve_model(requested: str | None) -> tuple[str, str]:
    """把调用方请求的模型名映射到当前提供商的 (endpoint_url, model_name)。"""
    cfg = _provider_config()
    base = cfg["base_url"].rstrip("/")
    model = requested or cfg["model"]
    # 调用方传的是默认 deepseek 模型名时，替换为当前提供商默认模型
    if requested in ("deepseek-chat", "deepseek-reasoner") and cfg["kind"] != "openai":
        model = cfg["model"]
    if cfg["kind"] == "anthropic":
        return f"{base}{cfg['endpoint']}", model
    return f"{base}{cfg['endpoint']}", model


async def _call_with_retry(
    body: dict,
    url: str,
    max_retries: int = 4,
    anthropic: bool = False,
) -> dict:
    """带重试的 API 调用：多 key 轮询 + 指数退避处理限流。

    永久性 4xx（400/401/403/404）直接失败不重试；429 读取 Retry-After 退避。
    """
    last_error = None

    for attempt in range(max_retries):
        key = _next_key()
        try:
            async with _get_client(key) as client:
                response = await client.post(url, json=body)

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
    """发送 chat completion 请求，返回文本。带多 key 轮询 + 重试。

    支持提供商：deepseek（默认）/ openai / anthropic / ollama / custom，均统一入口。
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    cfg = _provider_config()
    url, resolved_model = _resolve_model(model)
    is_anthropic = cfg["kind"] == "anthropic"

    if is_anthropic:
        # Anthropic Messages API：system 单独传，max_tokens 必填
        body = {
            "model": resolved_model,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format and response_format.get("type") == "json_object":
            body["response_format"] = response_format
    else:
        body = {
            "model": resolved_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format and response_format.get("type") == "json_object":
            body["response_format"] = response_format

    data = await _call_with_retry(body, url, anthropic=is_anthropic)
    if is_anthropic:
        # Anthropic 响应: {content: [{type: "text", text: "..."}]}
        content = "".join(
            b.get("text", "") for b in (data.get("content") or []) if b.get("type") == "text"
        )
        return (content or "").strip() or ""
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
