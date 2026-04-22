"""AI strategy assistant service."""
from __future__ import annotations

import ast
import json
import re
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import NewsArticle, Settings
from services.llm_gateway import chat_complete_text
STRATEGY_AI_SKILLS = [
    {
        "key": "template_guard",
        "name": "模板约束",
        "description": "强制生成符合 QTsys 回测引擎的 initialize/handle_data 模板。",
    },
    {
        "key": "market_news_brief",
        "name": "市场快照",
        "description": "可结合系统内最近市场新闻，辅助生成更贴近当前环境的策略。",
    },
    {
        "key": "quant_design",
        "name": "量化设计",
        "description": "围绕信号、仓位、风控和调仓逻辑输出可执行策略代码。",
    },
    {
        "key": "risk_review",
        "name": "风险复核",
        "description": "对生成代码做本地校验，失败时自动触发一次修复。",
    },
]

ALLOWED_CONTEXT_APIS = [
    "context.universe",
    "context.positions",
    "context.cash",
    "context.portfolio_value",
    "context.get_history(ts_code, count, field)",
    "context.get_price(ts_code)",
    "context.order(ts_code, amount)",
    "context.order_value(ts_code, value)",
    "context.order_target_percent(ts_code, pct)",
    "context.log(message)",
]

FORBIDDEN_ITEMS = [
    "os",
    "sys",
    "subprocess",
    "pathlib",
    "socket",
    "urllib",
    "requests",
    "eval",
    "exec",
    "__import__",
]

DEFAULT_REPLY = "已根据你的要求生成可直接保存并用于回测的策略草稿。"
DEFAULT_NAME = "AI 策略草稿"
DEFAULT_DESCRIPTION = "AI 生成的量化交易策略"


def _strip_markdown_fence(text: str) -> str:
    stripped = (text or "").strip()
    if not stripped:
        return ""
    match = re.match(r"^```[a-zA-Z0-9_-]*\s*([\s\S]*?)\s*```$", stripped)
    if match:
        return match.group(1).strip()
    return stripped


def _candidate_json_blocks(text: str) -> list[str]:
    cleaned = _strip_markdown_fence(text)
    if not cleaned:
        return []

    candidates = [cleaned]
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", cleaned):
        start = match.start()
        try:
            _, end = decoder.raw_decode(cleaned[start:])
            candidates.append(cleaned[start : start + end])
        except json.JSONDecodeError:
            continue

    if "```" in text:
        for fenced in re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", text, flags=re.I):
            candidates.append(fenced.strip())
    return candidates


def _extract_json_object(text: str) -> dict[str, Any]:
    last_error: Optional[Exception] = None
    for candidate in _candidate_json_blocks(text):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(parsed, dict):
            return parsed
        last_error = ValueError("JSON 根对象不是对象类型")
    if last_error is not None:
        raise ValueError(str(last_error)) from last_error
    raise ValueError("AI 返回内容中未找到可解析的 JSON 对象")


def _normalize_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _normalize_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, list):
        chunks: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content") or ""
                if text:
                    chunks.append(str(text))
            elif item is not None:
                chunks.append(str(item))
        value = "\n".join(part for part in chunks if part).strip()
    text = str(value).strip()
    return text or default


def _extract_python_code(text: str) -> str:
    cleaned = _normalize_text(text)
    if not cleaned:
        return ""

    fenced_blocks = re.findall(r"```(?:python|py)?\s*([\s\S]*?)\s*```", cleaned, flags=re.I)
    for candidate in fenced_blocks + [cleaned]:
        snippet = candidate.strip()
        if "def initialize" in snippet and "def handle_data" in snippet:
            return snippet if snippet.endswith("\n") else snippet + "\n"
    return ""


def _extract_strategy_from_freeform(payload: dict[str, Any], raw_text: str) -> dict[str, Any]:
    strategy = payload.get("strategy") if isinstance(payload.get("strategy"), dict) else {}
    code = _normalize_text(
        strategy.get("code")
        or strategy.get("strategy_code")
        or strategy.get("python_code")
        or payload.get("code")
        or payload.get("strategy_code")
        or payload.get("python_code")
    )
    if not code:
        code = _extract_python_code(raw_text)
    if code and not code.endswith("\n"):
        code += "\n"

    return {
        "assistant_reply": _normalize_text(payload.get("assistant_reply") or payload.get("message"), DEFAULT_REPLY),
        "strategy": {
            "name": _normalize_text(strategy.get("name") or payload.get("name"), DEFAULT_NAME),
            "description": _normalize_text(strategy.get("description") or payload.get("description"), DEFAULT_DESCRIPTION),
            "code": code,
            "logic_points": _normalize_list(strategy.get("logic_points") or payload.get("logic_points")),
            "risk_points": _normalize_list(strategy.get("risk_points") or payload.get("risk_points")),
            "market_view": _normalize_text(strategy.get("market_view") or payload.get("market_view")),
            "analysis_notes": _normalize_list(strategy.get("analysis_notes") or payload.get("analysis_notes")),
            "tags": _normalize_list(strategy.get("tags") or payload.get("tags")),
        },
    }


def _normalize_strategy_payload(payload: dict[str, Any]) -> dict[str, Any]:
    strategy = payload.get("strategy") or {}
    if not isinstance(strategy, dict):
        strategy = {}

    code = _normalize_text(
        strategy.get("code")
        or strategy.get("strategy_code")
        or strategy.get("python_code")
        or payload.get("code")
        or payload.get("strategy_code")
        or payload.get("python_code")
    )
    if code and not code.endswith("\n"):
        code += "\n"

    return {
        "assistant_reply": _normalize_text(payload.get("assistant_reply"), DEFAULT_REPLY),
        "strategy": {
            "name": _normalize_text(strategy.get("name"), DEFAULT_NAME),
            "description": _normalize_text(strategy.get("description"), DEFAULT_DESCRIPTION),
            "code": code,
            "logic_points": _normalize_list(strategy.get("logic_points")),
            "risk_points": _normalize_list(strategy.get("risk_points")),
            "market_view": _normalize_text(strategy.get("market_view")),
            "analysis_notes": _normalize_list(strategy.get("analysis_notes")),
            "tags": _normalize_list(strategy.get("tags")),
        },
    }


def validate_strategy_code(code: str) -> Optional[str]:
    if not code.strip():
        return "策略代码为空"
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"策略代码语法错误: {exc.msg} (line {exc.lineno})"

    function_defs = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    if "handle_data" not in function_defs:
        return "策略必须定义 handle_data(context) 函数"
    if "initialize" in function_defs and len(function_defs["initialize"].args.args) != 1:
        return "initialize 必须只接收一个 context 参数"
    if len(function_defs["handle_data"].args.args) != 1:
        return "handle_data 必须只接收一个 context 参数"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in FORBIDDEN_ITEMS:
                    return f"禁止导入模块: {root}"
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in FORBIDDEN_ITEMS:
                return f"禁止导入模块: {root}"
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec", "__import__"}:
                return f"禁止使用危险函数: {node.func.id}"
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr not in {"__init__", "__name__"}:
                return f"禁止访问双下划线属性: {node.attr}"

    return None


async def _call_openai_compatible(
    api_key: str,
    base_url: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.2,
    max_tokens: int = 2400,
) -> str:
    result = await chat_complete_text(
        api_key=api_key,
        base_url=base_url,
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return result["content"]


async def _load_market_context(db: AsyncSession, limit: int = 6) -> dict[str, Any]:
    result = await db.execute(
        select(NewsArticle).order_by(NewsArticle.created_at.desc()).limit(limit)
    )
    articles = list(result.scalars().all())
    news_items = []
    for article in articles:
        summary = (article.summary or article.content or "").strip().replace("\n", " ")
        if len(summary) > 120:
            summary = summary[:117] + "..."
        news_items.append(
            {
                "title": article.title or "",
                "source": article.source or "",
                "publish_time": article.publish_time or "",
                "summary": summary,
                "sentiment_label": article.sentiment_label or "",
            }
        )
    return {
        "news_count": len(news_items),
        "news_items": news_items,
        "skills": STRATEGY_AI_SKILLS,
    }


def _render_market_context(context: dict[str, Any]) -> str:
    items = context.get("news_items") or []
    if not items:
        return "当前没有可用的市场新闻缓存，请在无新闻上下文下生成策略。"
    lines = []
    for item in items:
        title = " | ".join(
            piece
            for piece in [
                item.get("publish_time") or "时间未知",
                item.get("source") or "未知来源",
                item.get("title") or "",
            ]
            if piece
        )
        lines.append(
            f"- {title}\n"
            f"  摘要：{item.get('summary') or '无摘要'}\n"
            f"  情绪：{item.get('sentiment_label') or '未标注'}"
        )
    return "\n".join(lines)


def _build_system_prompt(
    include_market_context: bool,
    market_context_text: str,
    current_strategy: Optional[dict[str, Any]] = None,
) -> str:
    current_block = ""
    if current_strategy:
        current_block = (
            "\n当前编辑中的策略如下，如果用户要求优化、重构或解释，请基于它继续：\n"
            f"策略名称：{_normalize_text(current_strategy.get('name'), '未命名策略')}\n"
            f"策略描述：{_normalize_text(current_strategy.get('description'), '无描述')}\n"
            f"策略代码：\n{_normalize_text(current_strategy.get('code'), '无')}\n"
        )

    market_block = ""
    if include_market_context:
        market_block = (
            "\n你还具备“市场快照”能力，可参考系统内最近市场新闻：\n"
            f"{market_context_text}\n"
        )

    return f"""
你是 QTsys 内置的 AI 量化策略助手，目标是交付“可直接保存、可直接回测”的策略代码。

你必须严格遵守以下约束：
1. 只返回一个 JSON 对象，不要输出 Markdown，不要输出 ``` 代码块。
2. JSON 结构必须为：
{{
  "assistant_reply": "给用户的简洁说明",
  "strategy": {{
    "name": "策略名称",
    "description": "一到两句简介",
    "code": "完整 Python 策略代码",
    "logic_points": ["核心逻辑1", "核心逻辑2"],
    "risk_points": ["风险提示1", "风险提示2"],
    "market_view": "当前市场环境适配说明",
    "analysis_notes": ["实现说明1", "实现说明2"],
    "tags": ["趋势", "均线"]
  }}
}}
3. 代码中必须定义 `initialize(context)` 和 `handle_data(context)`。
4. 主交易逻辑必须写在 `handle_data(context)` 中。
5. 只允许使用这些上下文 API：{", ".join(ALLOWED_CONTEXT_APIS)}。
6. 默认优先使用纯 Python 实现，不要依赖 `numpy as np` 或 `pandas as pd`；只有在确实必要时才使用它们。禁止使用这些模块或能力：{", ".join(FORBIDDEN_ITEMS)}。
7. 不要访问任何未说明的系统对象，不要读写文件，不要联网。
8. 股票池必须直接使用 `context.universe`，严禁在代码里硬编码大段股票列表。
9. 每只股票都要处理历史数据不足的情况。
10. 仓位控制必须清晰，避免无限加仓；单股仓位若用户有约束，必须落实到代码里。
11. 输出保持紧凑，策略代码尽量控制在 140 行以内，避免冗长注释和大段说明。
12. 如果你无法稳定输出完整 JSON，也必须优先保证 `strategy.code` 字段里是完整可运行的 Python 代码，不能留空。

设计标准：
- 信号、买卖规则、仓位规则、风控规则、调仓规则要完整。
- 优先给出稳健、可解释、可维护的策略，不要只给空洞框架。
- 如果用户要求优化当前策略，应基于当前策略改进，而不是脱离上下文重写。
{market_block}
{current_block}
""".strip()


def _conversation_text(messages: list[dict[str, str]]) -> str:
    parts = []
    for item in messages:
        role = item.get("role", "user")
        content = _normalize_text(item.get("content"))
        if content:
            parts.append(f"[{role}] {content}")
    return "\n".join(parts)


async def _repair_invalid_json_once(
    api_key: str,
    base_url: str,
    model: str,
    raw_output: str,
    messages: list[dict[str, str]],
) -> dict[str, Any]:
    repair_messages = [
        {
            "role": "system",
            "content": (
                "你是 JSON 修复助手。"
                "请输出一个完整、严格可解析的 JSON 对象。"
                "如果原始输出被截断或损坏，请依据原始用户需求重新生成一个更紧凑的完整版本。"
                "不要输出 Markdown，不要输出解释。"
                "策略代码必须使用 context.universe，不能硬编码大段股票列表。"
            ),
        },
        {
            "role": "user",
            "content": (
                "原始用户对话如下：\n"
                f"{_conversation_text(messages)}\n\n"
                "下面是模型上一次的原始输出，可能被 Markdown 包裹、截断或损坏：\n"
                f"{raw_output[:5000]}"
            ),
        },
    ]
    repaired_raw = await _call_openai_compatible(
        api_key=api_key,
        base_url=base_url,
        model=model,
        messages=repair_messages,
        temperature=0.1,
        max_tokens=2600,
    )
    return _normalize_strategy_payload(_extract_json_object(repaired_raw))


async def _repair_strategy_once(
    api_key: str,
    base_url: str,
    model: str,
    invalid_payload: dict[str, Any],
    validation_error: str,
) -> dict[str, Any]:
    repair_messages = [
        {
            "role": "system",
            "content": (
                "你是 QTsys 策略修复助手。"
                "请只修复 JSON 中 strategy.code 的兼容性问题，保留整体策略思路。"
                "返回相同结构的 JSON 对象，不要 Markdown，不要解释。"
                "修复后代码必须使用 context.universe，且必须包含 initialize(context) 与 handle_data(context)。"
            ),
        },
        {
            "role": "user",
            "content": (
                "以下策略未通过 QTsys 模板校验，请修复。\n"
                f"校验错误：{validation_error}\n"
                f"原始 JSON：{json.dumps(invalid_payload, ensure_ascii=False)}"
            ),
        },
    ]
    repaired_raw = await _call_openai_compatible(
        api_key=api_key,
        base_url=base_url,
        model=model,
        messages=repair_messages,
        temperature=0.1,
        max_tokens=2400,
    )
    return _normalize_strategy_payload(_extract_json_object(repaired_raw))


async def generate_strategy_with_ai(
    db: AsyncSession,
    *,
    messages: list[dict[str, str]],
    current_strategy: Optional[dict[str, Any]] = None,
    include_market_context: bool = True,
) -> dict[str, Any]:
    settings_result = await db.execute(select(Settings).where(Settings.id == 1))
    settings = settings_result.scalar_one_or_none()
    if not settings or not settings.llm_api_key or not settings.llm_base_url:
        raise ValueError("请先在设置页面配置 LLM API Key、接口地址和模型名称。")

    market_context = await _load_market_context(db) if include_market_context else {
        "news_count": 0,
        "news_items": [],
        "skills": STRATEGY_AI_SKILLS,
    }
    system_prompt = _build_system_prompt(
        include_market_context=include_market_context,
        market_context_text=_render_market_context(market_context),
        current_strategy=current_strategy,
    )

    llm_messages = [{"role": "system", "content": system_prompt}]
    for item in messages:
        role = item.get("role", "user")
        if role not in {"user", "assistant"}:
            role = "user"
        content = _normalize_text(item.get("content"))
        if content:
            llm_messages.append({"role": role, "content": content})

    if len(llm_messages) == 1:
        llm_messages.append(
            {
                "role": "user",
                "content": "请基于当前市场环境，生成一个适合本系统回测的股票量化策略。",
            }
        )

    model_name = settings.llm_model or "gpt-4o-mini"
    raw = await _call_openai_compatible(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=model_name,
        messages=llm_messages,
        temperature=0.2,
        max_tokens=2600,
    )

    try:
        normalized = _normalize_strategy_payload(_extract_json_object(raw))
    except Exception:
        normalized = await _repair_invalid_json_once(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=model_name,
            raw_output=raw,
            messages=llm_messages,
        )

    if not normalized["strategy"]["code"]:
        fallback_payload = _extract_strategy_from_freeform({}, raw)
        if fallback_payload["strategy"]["code"]:
            normalized = fallback_payload

    validation_error = validate_strategy_code(normalized["strategy"]["code"])
    if validation_error:
        normalized = await _repair_strategy_once(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=model_name,
            invalid_payload=normalized,
            validation_error=validation_error,
        )
        validation_error = validate_strategy_code(normalized["strategy"]["code"])

    if validation_error and not normalized["strategy"]["code"]:
        fallback_payload = _extract_strategy_from_freeform(normalized, raw)
        if fallback_payload["strategy"]["code"]:
            normalized = fallback_payload
            validation_error = validate_strategy_code(normalized["strategy"]["code"])

    if validation_error:
        raise ValueError(f"AI 生成的策略未通过模板校验：{validation_error}")

    return {
        **normalized,
        "context_summary": market_context,
        "validation": {"passed": True, "error": ""},
    }
