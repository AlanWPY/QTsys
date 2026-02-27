"""新闻情感分析与影响判断引擎"""
import re
from dataclasses import dataclass, field
from logging_config import get_logger

logger = get_logger("qtsys.news.analyzer")


@dataclass
class SentimentResult:
    score: float = 0.0          # -1.0 ~ 1.0
    label: str = "中性"          # 利好/利空/中性
    impact_level: str = "低"     # 高/中/低
    sectors: list = field(default_factory=list)
    keywords_hit: list = field(default_factory=list)
    reason: str = ""


# ===== 否定词表 =====
NEGATION_WORDS = {"不", "不会", "没有", "未", "非", "无", "否认", "难以", "不再", "并非"}

# ===== 关键词词典 =====

POSITIVE_WORDS = {
    # 政策利好
    "利好": 0.6, "上涨": 0.5, "大涨": 0.7, "涨停": 0.8, "暴涨": 0.8,
    "突破": 0.4, "新高": 0.6, "反弹": 0.4, "回升": 0.3, "走强": 0.4,
    "增长": 0.4, "盈利": 0.5, "超预期": 0.6, "景气": 0.4, "繁荣": 0.5,
    "降息": 0.5, "降准": 0.5, "宽松": 0.4, "刺激": 0.4, "扶持": 0.4,
    "补贴": 0.4, "减税": 0.5, "利润增长": 0.6, "业绩预增": 0.7,
    "订单": 0.3, "签约": 0.3, "合作": 0.3, "并购": 0.3, "重组": 0.4,
    "回购": 0.4, "增持": 0.5, "举牌": 0.5, "北向资金流入": 0.5,
    "放量": 0.3, "资金流入": 0.4, "机构看好": 0.4, "评级上调": 0.5,
    # IPO与融资
    "IPO": 0.3, "上市": 0.3, "融资": 0.3, "定增": 0.3,
    # 科技创新
    "技术突破": 0.5, "自主可控": 0.4, "国产替代": 0.5, "量产": 0.4,
    # 宏观利好
    "经济复苏": 0.5, "就业改善": 0.4, "出口增长": 0.4, "消费回暖": 0.4,
    "政策支持": 0.4, "产业升级": 0.4, "数字化转型": 0.3,
}

NEGATIVE_WORDS = {
    "利空": -0.6, "下跌": -0.5, "大跌": -0.7, "跌停": -0.8, "暴跌": -0.8,
    "破位": -0.5, "新低": -0.6, "回调": -0.3, "走弱": -0.4, "下行": -0.3,
    "亏损": -0.5, "下滑": -0.4, "不及预期": -0.6, "萧条": -0.5,
    "加息": -0.4, "收紧": -0.4, "监管": -0.3, "处罚": -0.5, "罚款": -0.5,
    "退市": -0.8, "爆雷": -0.8, "违规": -0.5, "造假": -0.7,
    "减持": -0.5, "抛售": -0.6, "清仓": -0.6, "北向资金流出": -0.5,
    "缩量": -0.3, "资金流出": -0.4, "评级下调": -0.5, "风险": -0.3,
    "战争": -0.5, "制裁": -0.5, "贸易摩擦": -0.4, "通胀": -0.3,
    "债务": -0.3, "违约": -0.6, "破产": -0.7, "裁员": -0.4,
    # 监管与合规
    "立案调查": -0.7, "内幕交易": -0.6, "操纵市场": -0.6, "信披违规": -0.5,
    # 宏观风险
    "经济衰退": -0.6, "滞胀": -0.5, "资本外流": -0.5, "汇率贬值": -0.4,
    "产能过剩": -0.4, "库存积压": -0.3, "需求萎缩": -0.5,
}

# ===== 行业/板块关键词映射 =====

SECTOR_KEYWORDS = {
    "银行": ["银行", "信贷", "存款", "贷款", "LPR"],
    "房地产": ["房地产", "楼市", "房价", "地产", "住房", "土地"],
    "科技": ["芯片", "半导体", "AI", "人工智能", "算力", "大模型", "机器人"],
    "新能源": ["新能源", "光伏", "锂电", "储能", "风电", "氢能", "充电桩"],
    "汽车": ["汽车", "新能源车", "电动车", "智能驾驶", "自动驾驶"],
    "医药": ["医药", "医疗", "创新药", "疫苗", "生物医药", "中药"],
    "消费": ["消费", "白酒", "食品", "零售", "电商", "旅游"],
    "军工": ["军工", "国防", "航空", "航天", "导弹", "军事"],
    "有色金属": ["黄金", "白银", "铜", "铝", "锂", "稀土", "有色"],
    "石油化工": ["石油", "原油", "天然气", "化工", "煤炭"],
    "农业": ["农业", "粮食", "猪肉", "养殖", "种业"],
    "传媒": ["传媒", "游戏", "影视", "短视频", "直播"],
    "宏观经济": ["GDP", "CPI", "PPI", "PMI", "央行", "货币政策", "财政"],
    "半导体": ["晶圆", "封测", "EDA", "光刻", "制程", "芯片设计"],
    "信创": ["信创", "国产化", "操作系统", "数据库", "中间件", "办公软件"],
    "数据要素": ["数据要素", "数据交易", "数据确权", "数字经济", "数据资产"],
    "低空经济": ["低空经济", "eVTOL", "无人机", "飞行汽车", "通用航空"],
}

IMPACT_KEYWORDS = {
    "高": ["重磅", "突发", "紧急", "历史性", "首次", "全面", "大幅",
           "暴涨", "暴跌", "涨停", "跌停", "爆雷", "退市"],
    "中": ["超预期", "不及预期", "调整", "变化", "新政", "改革",
           "增长", "下滑", "回升", "回调"],
}


# ===== 核心分析函数 =====

def _is_negated(text: str, word: str) -> bool:
    """检查关键词前是否有否定词（窗口3个字符内）"""
    idx = text.find(word)
    if idx <= 0:
        return False
    prefix = text[max(0, idx - 3):idx]
    for neg in NEGATION_WORDS:
        if neg in prefix:
            return True
    return False


def analyze_sentiment(title: str, summary: str = "") -> SentimentResult:
    """基于关键词的情感分析 - 支持否定词处理"""
    text = (title + " " + summary).strip()
    if not text:
        return SentimentResult()

    score = 0.0
    hits = []

    # 正面词匹配（含否定词反转）
    for word, weight in POSITIVE_WORDS.items():
        if word in text:
            if _is_negated(text, word):
                score -= weight * 0.5  # 否定后反转为负面，但力度减半
                hits.append(f"否定:{word}")
            else:
                score += weight
                hits.append(word)

    # 负面词匹配（含否定词反转）
    for word, weight in NEGATIVE_WORDS.items():
        if word in text:
            if _is_negated(text, word):
                score -= weight * 0.5  # 否定后反转为正面
                hits.append(f"否定:{word}")
            else:
                score += weight
                hits.append(word)

    # 归一化到 [-1, 1]
    score = max(-1.0, min(1.0, score))

    # 判断标签
    if score >= 0.2:
        label = "利好"
    elif score <= -0.2:
        label = "利空"
    else:
        label = "中性"

    # 识别影响行业
    sectors = _detect_sectors(text)

    # 判断影响等级
    impact = _detect_impact(text, abs(score))

    # 生成原因
    reason = _build_reason(label, hits, sectors)

    return SentimentResult(
        score=round(score, 3),
        label=label,
        impact_level=impact,
        sectors=sectors,
        keywords_hit=hits[:10],
        reason=reason,
    )


def _detect_sectors(text: str) -> list[str]:
    """识别新闻涉及的行业板块"""
    found = []
    for sector, keywords in SECTOR_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                found.append(sector)
                break
    return found


def _detect_impact(text: str, abs_score: float) -> str:
    """判断影响等级"""
    for kw in IMPACT_KEYWORDS["高"]:
        if kw in text:
            return "高"
    if abs_score >= 0.5:
        return "高"
    for kw in IMPACT_KEYWORDS["中"]:
        if kw in text:
            return "中"
    if abs_score >= 0.25:
        return "中"
    return "低"


def _build_reason(label: str, hits: list, sectors: list) -> str:
    """生成分析原因说明"""
    parts = []
    if hits:
        parts.append(f"触发关键词: {', '.join(hits[:5])}")
    if sectors:
        parts.append(f"涉及板块: {', '.join(sectors[:4])}")
    if not parts:
        return "未检测到明显情感倾向"
    return f"{label} | " + "; ".join(parts)


# ===== LLM增强分析 =====

LLM_ANALYSIS_PROMPT = """你是一位资深金融分析师。请分析以下新闻对金融市场的影响。

新闻标题: {title}
新闻摘要: {summary}

请严格按以下JSON格式返回:
{{"sentiment": "利好/利空/中性", "score": 0.5, "impact_level": "高/中/低", "sectors": ["行业1"], "reason": "简要分析原因"}}

只返回JSON，不要其他内容。"""


async def analyze_with_llm(
    title: str, summary: str,
    api_key: str, base_url: str, model: str,
) -> dict:
    """使用LLM进行深度分析"""
    import aiohttp
    import json

    prompt = LLM_ANALYSIS_PROMPT.format(title=title, summary=summary[:300])
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3, "max_tokens": 500,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
                text = data["choices"][0]["message"]["content"]
                start = text.find("{")
                end = text.rfind("}") + 1
                if start >= 0 and end > start:
                    return json.loads(text[start:end])
    except Exception:
        logger.exception("LLM分析异常")
    return {}


# ===== 市场情绪汇总 =====

def aggregate_sentiment(results: list[dict]) -> dict:
    """汇总多条新闻的市场情绪"""
    if not results:
        return {"overall": "中性", "score": 0, "bullish": 0, "bearish": 0, "neutral": 0, "sectors": {}}

    bullish = sum(1 for r in results if r.get("label") == "利好")
    bearish = sum(1 for r in results if r.get("label") == "利空")
    neutral = len(results) - bullish - bearish
    avg_score = sum(r.get("score", 0) for r in results) / len(results)

    # 板块情绪统计
    sector_scores = {}
    for r in results:
        for s in r.get("sectors", []):
            if s not in sector_scores:
                sector_scores[s] = {"count": 0, "score_sum": 0}
            sector_scores[s]["count"] += 1
            sector_scores[s]["score_sum"] += r.get("score", 0)

    sectors = {}
    for s, v in sector_scores.items():
        avg = v["score_sum"] / v["count"]
        sectors[s] = {
            "count": v["count"],
            "avg_score": round(avg, 3),
            "label": "利好" if avg >= 0.2 else ("利空" if avg <= -0.2 else "中性"),
        }

    if avg_score >= 0.15:
        overall = "偏多"
    elif avg_score <= -0.15:
        overall = "偏空"
    else:
        overall = "中性"

    return {
        "overall": overall,
        "score": round(avg_score, 3),
        "bullish": bullish,
        "bearish": bearish,
        "neutral": neutral,
        "total": len(results),
        "sectors": sectors,
    }
