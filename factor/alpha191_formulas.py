"""Alpha191 \u56e0\u5b50\u8bf4\u660e\u751f\u6210\u5668"""

from factor.alpha191_templates import ALPHA191_FORMULAS


def _contains_any(text: str, words: list[str]) -> bool:
    return any(word in text for word in words)


def _build_tags(formula: str) -> list[str]:
    lower = formula.lower()
    tags: list[str] = []
    if _contains_any(lower, ["correlation", "covariance"]):
        tags.append("\u91cf\u4ef7\u5173\u7cfb")
    if _contains_any(lower, ["rank(", "ts_rank"]):
        tags.append("\u6a2a\u622a\u9762\u6392\u5e8f")
    if _contains_any(lower, ["delta", "delay", "returns"]):
        tags.append("\u52a8\u91cf\u53cd\u8f6c")
    if _contains_any(lower, ["std", "variance"]):
        tags.append("\u6ce2\u52a8\u7387")
    if _contains_any(lower, ["volume", "adv"]):
        tags.append("\u6d41\u52a8\u6027")
    if _contains_any(lower, ["vwap"]):
        tags.append("\u6210\u4ea4\u5747\u4ef7\u504f\u79bb")
    if _contains_any(lower, ["open", "high", "low", "close"]):
        tags.append("K\u7ebf\u7ed3\u6784")
    if _contains_any(lower, ["mean", "sma", "decaylinear"]):
        tags.append("\u8d8b\u52bf\u5e73\u6ed1")
    return tags or ["\u590d\u5408\u4fe1\u53f7"]


def _build_component_text(formula: str) -> str:
    lower = formula.lower()
    parts: list[str] = []
    if "open" in lower:
        parts.append("\u5f00\u76d8\u4ef7")
    if "close" in lower:
        parts.append("\u6536\u76d8\u4ef7")
    if _contains_any(lower, ["high", "low"]):
        parts.append("\u9ad8\u4f4e\u4ef7\u533a\u95f4")
    if "vwap" in lower:
        parts.append("\u6210\u4ea4\u5747\u4ef7")
    if _contains_any(lower, ["volume", "adv"]):
        parts.append("\u6210\u4ea4\u91cf\u4e0e\u6362\u624b")
    if "returns" in lower:
        parts.append("\u6536\u76ca\u7387\u53d8\u5316")
    deduped = list(dict.fromkeys(parts))
    return "\u3001".join(deduped) if deduped else "\u4ef7\u683c\u4e0e\u6210\u4ea4\u6570\u636e"


def _build_signal_family(formula: str) -> str:
    lower = formula.lower()
    if _contains_any(lower, ["correlation"]) and _contains_any(lower, ["volume", "adv"]):
        return "\u91cf\u4ef7\u8054\u52a8\u578b"
    if _contains_any(lower, ["vwap"]) and _contains_any(lower, ["close", "open"]):
        return "\u4ef7\u683c\u504f\u79bb\u4fee\u590d\u578b"
    if _contains_any(lower, ["std", "variance"]) and _contains_any(lower, ["delta", "returns"]):
        return "\u6ce2\u52a8\u653e\u5927\u578b\u52a8\u91cf\u4fe1\u53f7"
    if _contains_any(lower, ["mean", "sma", "decaylinear"]) and _contains_any(lower, ["close", "returns"]):
        return "\u8d8b\u52bf\u5e73\u6ed1\u786e\u8ba4\u578b"
    if _contains_any(lower, ["open"]) and _contains_any(lower, ["delay(close, 1)", "delay(open, 1)"]):
        return "\u8df3\u7a7a\u7f3a\u53e3\u53cd\u5e94\u578b"
    if _contains_any(lower, ["high", "low", "close"]):
        return "\u4ef7\u683c\u533a\u95f4\u7ed3\u6784\u578b"
    if _contains_any(lower, ["rank", "ts_rank"]):
        return "\u76f8\u5bf9\u5f3a\u5f31\u6392\u5e8f\u578b"
    return "\u590d\u5408\u5b9a\u4ef7\u504f\u5dee\u4fe1\u53f7"


def _build_mechanism(formula: str) -> str:
    lower = formula.lower()
    clauses: list[str] = []
    if _contains_any(lower, ["delta", "delay"]):
        clauses.append("\u901a\u8fc7\u6bd4\u8f83\u5f53\u524d\u503c\u4e0e\u5386\u53f2\u6ede\u540e\u503c\uff0c\u6355\u6349\u77ed\u671f\u4ef7\u683c\u6216\u6210\u4ea4\u884c\u4e3a\u7684\u52a0\u901f\u5ea6\u53d8\u5316")
    if _contains_any(lower, ["rank(", "ts_rank"]):
        clauses.append("\u4f7f\u7528\u6392\u5e8f\u7b97\u5b50\u524a\u5f31\u6781\u7aef\u503c\u5f71\u54cd\uff0c\u66f4\u5f3a\u8c03\u622a\u9762\u4e0a\u7684\u76f8\u5bf9\u5f3a\u5f31\u800c\u4e0d\u662f\u7edd\u5bf9\u6570\u503c")
    if _contains_any(lower, ["correlation", "covariance"]):
        clauses.append("\u901a\u8fc7\u76f8\u5173\u6027\u6216\u534f\u65b9\u5dee\u8861\u91cf\u4ef7\u683c\u4e0e\u6210\u4ea4\u91cf\u662f\u5426\u540c\u6b65\u6269\u5f20\uff0c\u4ece\u800c\u8bc6\u522b\u8d44\u91d1\u63a8\u52a8\u7684\u8d8b\u52bf\u5f3a\u5ea6")
    if _contains_any(lower, ["std", "variance"]):
        clauses.append("\u628a\u6ce2\u52a8\u7387\u7eb3\u5165\u4fe1\u53f7\u540e\uff0c\u56e0\u5b50\u4f1a\u5bf9\u98ce\u9669\u6269\u5f20\u3001\u62e5\u6324\u4ea4\u6613\u6216\u8106\u5f31\u8d8b\u52bf\u66f4\u654f\u611f")
    if "vwap" in lower:
        clauses.append("\u5f15\u5165\u6210\u4ea4\u5747\u4ef7\u540e\uff0c\u56e0\u5b50\u80fd\u8861\u91cf\u6536\u76d8\u4ef7\u683c\u76f8\u5bf9\u771f\u5b9e\u6210\u4ea4\u91cd\u5fc3\u7684\u504f\u79bb\u7a0b\u5ea6")
    if _contains_any(lower, ["high", "low"]):
        clauses.append("\u7ed3\u5408\u65e5\u5185\u9ad8\u4f4e\u4ef7\u533a\u95f4\uff0c\u53ef\u5224\u65ad\u591a\u7a7a\u529b\u91cf\u5728\u4ea4\u6613\u65e5\u5185\u90e8\u7684\u62c9\u952f\u7ed3\u679c")
    if _contains_any(lower, ["mean", "sma", "decaylinear"]):
        clauses.append("\u5e73\u6ed1\u6216\u8870\u51cf\u52a0\u6743\u5904\u7406\u80fd\u964d\u4f4e\u5355\u65e5\u566a\u58f0\uff0c\u63d0\u5347\u5bf9\u6301\u7eed\u6027\u8d8b\u52bf\u7684\u8bc6\u522b\u80fd\u529b")
    if not clauses:
        clauses.append("\u8be5\u56e0\u5b50\u901a\u8fc7\u591a\u4e2a\u57fa\u7840\u7b97\u5b50\u7684\u7ec4\u5408\u6765\u8bc6\u522b\u5b9a\u4ef7\u504f\u5dee\uff0c\u5e76\u5c06\u5176\u6620\u5c04\u4e3a\u53ef\u6392\u5e8f\u7684\u622a\u9762\u4fe1\u53f7")
    return "\uff1b".join(clauses[:3]) + "\u3002"


def _build_use_case(formula: str) -> str:
    lower = formula.lower()
    if _contains_any(lower, ["volume", "adv", "correlation"]):
        return "\u8fd9\u7c7b\u56e0\u5b50\u66f4\u9002\u5408\u5728\u6210\u4ea4\u6d3b\u8dc3\u3001\u8d44\u91d1\u9a71\u52a8\u660e\u663e\u7684\u5e02\u573a\u73af\u5883\u4e2d\u4f7f\u7528\uff0c\u901a\u5e38\u7528\u4e8e\u8bc6\u522b\u88ab\u589e\u91cf\u8d44\u91d1\u5f3a\u5316\u7684\u5f3a\u52bf\u80a1\u6216\u5f31\u52bf\u80a1\u3002"
    if _contains_any(lower, ["vwap"]):
        return "\u8fd9\u7c7b\u56e0\u5b50\u9002\u5408\u7528\u4e8e\u6355\u6349\u4ef7\u683c\u5411\u6210\u4ea4\u91cd\u5fc3\u56de\u5f52\uff0c\u6216\u5224\u65ad\u5c3e\u76d8\u4ef7\u683c\u662f\u5426\u88ab\u5f02\u5e38\u4ea4\u6613\u884c\u4e3a\u63a8\u79bb\u5408\u7406\u533a\u95f4\u3002"
    if _contains_any(lower, ["std", "variance"]):
        return "\u8fd9\u7c7b\u56e0\u5b50\u9002\u5408\u914d\u5408\u98ce\u9669\u7ea6\u675f\u4f7f\u7528\uff0c\u5e2e\u52a9\u6295\u8d44\u8005\u533a\u5206\u9ad8\u6536\u76ca\u673a\u4f1a\u4e0e\u9ad8\u6ce2\u52a8\u9677\u9631\u3002"
    if _contains_any(lower, ["open"]) and _contains_any(lower, ["delay"]):
        return "\u8fd9\u7c7b\u56e0\u5b50\u5e38\u7528\u4e8e\u89c2\u5bdf\u9694\u591c\u4fe1\u606f\u5982\u4f55\u5728\u6b21\u65e5\u5f00\u76d8\u4f53\u73b0\uff0c\u5e76\u8bc4\u4f30\u8df3\u7a7a\u662f\u5426\u4f1a\u5728\u540e\u7eed\u4ea4\u6613\u4e2d\u5ef6\u7eed\u6216\u56de\u8865\u3002"
    return "\u5b9e\u52a1\u4e2d\u5efa\u8bae\u7ed3\u5408\u5206\u4f4d\u6536\u76ca\u3001IC/IR\u3001\u6362\u624b\u7387\u548c\u884c\u4e1a\u66b4\u9732\u5171\u540c\u9a8c\u8bc1\uff0c\u5224\u65ad\u8be5\u4fe1\u53f7\u662f\u5426\u5177\u6709\u7a33\u5b9a\u7684\u53ef\u4ea4\u6613\u6027\u3002"


def _build_description(formula: str) -> str:
    tags = "\u3001".join(_build_tags(formula)[:4])
    family = _build_signal_family(formula)
    components = _build_component_text(formula)
    return (
        f"\u8be5\u56e0\u5b50\u76f4\u63a5\u57fa\u4e8e{components}\u6784\u9020\uff0c\u6838\u5fc3\u5c5e\u4e8e{family}\u4fe1\u53f7\u3002"
        f"\u516c\u5f0f\u4e2d\u540c\u65f6\u4f7f\u7528\u4e86{tags}\u7b49\u7b97\u5b50\uff0c\u7528\u4e8e\u523b\u753b\u4ef7\u683c\u3001\u6210\u4ea4\u4e0e\u76f8\u5bf9\u5f3a\u5f31\u4e4b\u95f4\u7684\u8054\u52a8\u7279\u5f81\u3002"
    )


def _build_economic_meaning(formula: str) -> str:
    components = _build_component_text(formula)
    family = _build_signal_family(formula)
    return (
        f"\u4ece\u7ecf\u6d4e\u5b66\u542b\u4e49\u770b\uff0c\u8be5\u56e0\u5b50\u56f4\u7ed5{components}\u63d0\u53d6\u4fe1\u606f\uff0c\u672c\u8d28\u4e0a\u662f\u5728\u5bfb\u627e{family}\u6240\u4ee3\u8868\u7684\u9519\u8bef\u5b9a\u4ef7\u6216\u98ce\u9669\u8865\u507f\u3002"
        f"{_build_mechanism(formula)}"
        f"{_build_use_case(formula)}"
    )


ALPHA191_INFO = {
    number: {
        "formula": formula,
        "description": _build_description(formula),
        "economic_meaning": _build_economic_meaning(formula),
        "tags": _build_tags(formula),
        "source": "Alpha191 \u539f\u59cb\u516c\u5f0f",
    }
    for number, formula in ALPHA191_FORMULAS.items()
}
