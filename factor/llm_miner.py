"""LLM因子挖掘 - 通过大模型生成因子表达式"""
import json
from typing import Optional


async def call_llm(
    api_key: str,
    base_url: str,
    model: str,
    prompt: str,
) -> Optional[str]:
    """调用LLM API (兼容OpenAI格式)"""
    import aiohttp

    url = base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.8,
        "max_tokens": 2000,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, headers=headers,
                json=payload, timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data["choices"][0]["message"]["content"]
    except Exception:
        return None


FACTOR_MINING_PROMPT = """你是一个量化因子研究专家。请根据以下可用的数据和函数，生成{count}个有潜力的量化因子表达式。

可用变量:
- close: 收盘价序列
- high: 最高价序列
- low: 最低价序列
- vol: 成交量序列
- returns: 日收益率序列

可用函数:
- mean(series, n): n日均值
- std(series, n): n日标准差
- ts_max(series, n): n日最大值
- ts_min(series, n): n日最小值
- delta(series, n): n日差分
- delay(series, n): 延迟n日
- rank(series): 百分位排名
- corr(a, b, n): n日滚动相关系数
- abs(x), log(x), sqrt(x)

要求:
1. 每个因子用一行Python表达式表示
2. 因子应有明确的经济学含义
3. 尽量组合多个变量和函数
4. 避免过于简单的因子(如单纯的close)
{extra_hint}

请严格按以下JSON格式返回:
[
  {{"name": "因子名称", "expression": "因子表达式", "description": "因子含义说明"}},
  ...
]

只返回JSON数组，不要其他内容。"""


def parse_llm_response(text: str) -> list[dict]:
    """解析LLM返回的因子列表"""
    if not text:
        return []
    # 尝试提取JSON部分
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        items = json.loads(text[start:end + 1])
        results = []
        for item in items:
            if isinstance(item, dict) and "expression" in item:
                results.append({
                    "name": item.get("name", "LLM因子"),
                    "expression": item["expression"],
                    "description": item.get("description", ""),
                })
        return results
    except (json.JSONDecodeError, KeyError):
        return []
