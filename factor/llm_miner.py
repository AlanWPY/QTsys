"""LLM 因子挖掘。"""
import json
from typing import Optional

from services.llm_gateway import chat_complete_text


async def call_llm(
    api_key: str,
    base_url: str,
    model: str,
    prompt: str,
) -> Optional[str]:
    """调用 LLM 接口。"""
    try:
        result = await chat_complete_text(
            api_key=api_key,
            base_url=base_url,
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8,
            max_tokens=2000,
        )
        return result["content"]
    except Exception:
        return None


FACTOR_MINING_PROMPT = """你是一名量化因子研究专家。请根据以下可用数据和函数，生成 {count} 个有潜力的量化因子表达式。

可用变量：
- close: 收盘价序列
- high: 最高价序列
- low: 最低价序列
- vol: 成交量序列
- returns: 日收益率序列

可用函数：
- mean(series, n): n 日均值
- std(series, n): n 日标准差
- ts_max(series, n): n 日最大值
- ts_min(series, n): n 日最小值
- delta(series, n): n 日差分
- delay(series, n): 延迟 n 日
- rank(series): 横截面排序
- corr(a, b, n): n 日滚动相关系数
- abs(x), log(x), sqrt(x)

要求：
1. 每个因子只用一行 Python 表达式表示
2. 因子应有明确的经济学含义
3. 尽量组合多个变量和函数
4. 避免过于简单的因子（如单纯的 close）
{extra_hint}

请严格按以下 JSON 格式返回：
[
  {{"name": "因子名称", "expression": "因子表达式", "description": "因子说明"}},
  ...
]

只返回 JSON 数组，不要添加其他内容。"""


def parse_llm_response(text: str) -> list[dict]:
    """解析 LLM 返回的因子列表。"""
    if not text:
        return []
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        items = json.loads(text[start : end + 1])
        results = []
        for item in items:
            if isinstance(item, dict) and "expression" in item:
                results.append(
                    {
                        "name": item.get("name", "LLM 因子"),
                        "expression": item["expression"],
                        "description": item.get("description", ""),
                    }
                )
        return results
    except (json.JSONDecodeError, KeyError):
        return []
