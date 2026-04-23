"""统一的 LLM 调用网关。"""
from __future__ import annotations

import json
from typing import Any, Iterable
from urllib.parse import urlparse

import aiohttp


NEWCLI_PREFERRED_FALLBACKS = [
    "gpt-5-medium",
    "gpt-5",
    "gpt-5-low",
    "gpt-5-minimal",
]

MODEL_ALIASES = {
    "claude-sonnet-4-5": [
        "claude-sonnet-4-5-20250929",
        "claude-sonnet-4-20250514",
        "claude-3-7-sonnet-20250219",
        *NEWCLI_PREFERRED_FALLBACKS,
    ],
    "claude-sonnet-4": [
        "claude-sonnet-4-20250514",
        "claude-3-7-sonnet-20250219",
        *NEWCLI_PREFERRED_FALLBACKS,
    ],
}


def normalize_base_url(base_url: str) -> str:
    trimmed = (base_url or "").strip().rstrip("/")
    if not trimmed:
        return trimmed
    parsed = urlparse(trimmed)
    if parsed.netloc == "code.newcli.com":
        normalized_path = parsed.path.rstrip("/")
        if normalized_path.endswith("/v1"):
            return f"{parsed.scheme}://{parsed.netloc}{normalized_path}"
        if normalized_path:
            return f"{parsed.scheme}://{parsed.netloc}{normalized_path}/v1"
        return f"{parsed.scheme}://{parsed.netloc}/v1"
    if trimmed.endswith("/chat/completions"):
        return trimmed[: -len("/chat/completions")]
    if trimmed.endswith("/v1"):
        return trimmed
    if parsed.path.endswith("/v1"):
        return trimmed
    return trimmed + "/v1"


def build_chat_completions_url(base_url: str) -> str:
    normalized = normalize_base_url(base_url)
    if normalized.endswith("/chat/completions"):
        return normalized
    return normalized.rstrip("/") + "/chat/completions"


def build_models_url(base_url: str) -> str:
    normalized = normalize_base_url(base_url)
    if normalized.endswith("/models"):
        return normalized
    return normalized.rstrip("/") + "/models"


def is_newcli_claude_base(base_url: str) -> bool:
    parsed = urlparse((base_url or "").strip())
    if parsed.netloc != "code.newcli.com":
        return False
    path = parsed.path.rstrip("/")
    return path == "/claude" or path.startswith("/claude/")


async def list_models(api_key: str, base_url: str) -> list[str]:
    url = build_models_url(base_url)
    headers = {"Authorization": f"Bearer {api_key}"}
    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return []
                payload = await resp.json()
                data = payload.get("data") or []
                return [item.get("id", "").strip() for item in data if item.get("id")]
    except Exception:
        return []


def _dedupe(items: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = (item or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _coerce_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content") or ""
                if text:
                    chunks.append(str(text))
            elif item is not None:
                chunks.append(str(item))
        return "\n".join(part for part in chunks if part).strip()
    if isinstance(content, dict):
        text = content.get("text") or content.get("content") or ""
        return str(text).strip()
    return str(content or "").strip()


def _extract_system_and_messages(messages: list[dict[str, str]]) -> tuple[str, list[dict[str, str]]]:
    system_parts: list[str] = []
    converted: list[dict[str, str]] = []
    for item in messages:
        role = (item.get("role") or "user").strip()
        content = _coerce_message_content(item.get("content"))
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
            continue
        if role not in {"user", "assistant"}:
            role = "user"
        converted.append({"role": role, "content": content})
    if not converted:
        converted.append({"role": "user", "content": "Hello"})
    return "\n\n".join(system_parts).strip(), converted


def resolve_model_candidates(model: str, available_models: list[str] | None = None) -> list[str]:
    requested = (model or "").strip()
    available = available_models or []
    candidates = [requested]
    candidates.extend(MODEL_ALIASES.get(requested, []))
    if requested and available:
        prefix_hits = [item for item in available if item == requested or item.startswith(requested + "-")]
        contains_hits = [item for item in available if requested in item]
        candidates.extend(prefix_hits)
        candidates.extend(contains_hits)
    if any(item.startswith("gpt-5") for item in available):
        for item in NEWCLI_PREFERRED_FALLBACKS:
            if item in available:
                candidates.append(item)
    candidates.extend(available)
    return _dedupe(candidates)


async def chat_complete_text(
    *,
    api_key: str,
    base_url: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.3,
    max_tokens: int = 2000,
) -> dict[str, Any]:
    normalized_base = normalize_base_url(base_url)
    use_anthropic_messages = is_newcli_claude_base(base_url)
    url = normalized_base.rstrip("/") + "/messages" if use_anthropic_messages else build_chat_completions_url(normalized_base)
    available_models = [] if use_anthropic_messages else await list_models(api_key, normalized_base)
    model_candidates = resolve_model_candidates(model, available_models)
    headers = (
        {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        if use_anthropic_messages
        else {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
    )
    timeout = aiohttp.ClientTimeout(total=120)
    last_error = "未知错误"

    def payload_variants(candidate_model: str) -> list[dict[str, Any]]:
        if use_anthropic_messages:
            system_text, anthropic_messages = _extract_system_and_messages(messages)
            base_payload = {
                "model": candidate_model,
                "messages": anthropic_messages,
                "max_tokens": max_tokens,
            }
            variants = [
                {**base_payload, "system": system_text} if system_text else base_payload,
                {**base_payload, "system": system_text, "temperature": temperature} if system_text else {**base_payload, "temperature": temperature},
            ]
        else:
            base_payload = {
                "model": candidate_model,
                "messages": messages,
            }
            variants = [
                {**base_payload, "temperature": temperature, "max_tokens": max_tokens},
                {**base_payload, "max_tokens": max_tokens},
                {**base_payload, "max_completion_tokens": max_tokens},
                base_payload,
            ]
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in variants:
            marker = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if marker in seen:
                continue
            seen.add(marker)
            deduped.append(item)
        return deduped

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for candidate_model in model_candidates:
            for payload in payload_variants(candidate_model):
                try:
                    async with session.post(url, headers=headers, json=payload) as resp:
                        body = await resp.text()
                        if resp.status != 200:
                            last_error = f"HTTP {resp.status} - {body[:400]}"
                            continue
                        data = json.loads(body)
                        if use_anthropic_messages:
                            blocks = data.get("content") or []
                            content = _coerce_message_content([item.get("text", "") for item in blocks if item.get("type") == "text"])
                        else:
                            choices = data.get("choices") or []
                            if not choices:
                                last_error = "返回结果中没有 choices"
                                continue
                            message = choices[0].get("message") or {}
                            content = _coerce_message_content(message.get("content"))
                        if not content:
                            last_error = "返回结果中没有可解析文本"
                            continue
                        return {
                            "content": content,
                            "model": data.get("model") or candidate_model,
                            "base_url": normalized_base,
                            "available_models": available_models,
                        }
                except Exception as exc:
                    last_error = str(exc)
                    continue

    raise RuntimeError(
        f"LLM 调用失败：已尝试 {url}，模型候选 {model_candidates[:6]}，最后错误：{last_error}"
    )
