"""文本规范化与乱码修复工具。"""
from __future__ import annotations

import re


MOJIBAKE_HINT_RE = re.compile(r"[ÃÂÅÆÇÐÑØÙÚÛÜÝÞßà-áâãäåæçè-éêëì-ïðñò-öø-üýþÿ]")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
CODE_RE = re.compile(r"\b\d{6}\.(?:SH|SZ)\b")
COUNT_RE = re.compile(r"\((\d+)只\)")

SYSTEM_UNIVERSE_LABELS = {
    "000905.SH": "中证500",
    "000300.SH": "沪深300",
    "000016.SH": "上证50",
    "000852.SH": "中证1000",
}


def _score_text(value: str) -> int:
    if not value:
        return 0
    cjk_count = len(CJK_RE.findall(value))
    mojibake_count = len(MOJIBAKE_HINT_RE.findall(value))
    replacement_count = value.count("�")
    question_count = value.count("?")
    return cjk_count * 3 - mojibake_count * 2 - replacement_count * 4 - question_count


def repair_text(value: str) -> str:
    """尝试修复常见 UTF-8 / Latin-1 / CP1252 误解码导致的中文乱码。"""
    if not isinstance(value, str) or not value:
        return value

    best = value
    best_score = _score_text(value)

    for encoding in ("latin1", "cp1252"):
        try:
            candidate = value.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        candidate_score = _score_text(candidate)
        if candidate_score > best_score:
            best = candidate
            best_score = candidate_score

    return best


def normalize_universe_label(value: str) -> str:
    if not isinstance(value, str) or not value:
        return value

    repaired = repair_text(value)
    if "?" not in repaired:
        return repaired

    code_match = CODE_RE.search(repaired)
    count_match = COUNT_RE.search(repaired)
    count_suffix = f" ({count_match.group(1)}只)" if count_match else ""

    if code_match:
        code = code_match.group(0)
        label = SYSTEM_UNIVERSE_LABELS.get(code)
        if label:
            return f"{label} {code}{count_suffix}"
        return f"{code}{count_suffix}".strip()

    if count_suffix:
        return f"自定义股票池{count_suffix}"
    return repaired


def normalize_text_payload(payload):
    """递归修复字典、列表中的字符串字段。"""
    if isinstance(payload, str):
        return repair_text(payload)
    if isinstance(payload, list):
        return [normalize_text_payload(item) for item in payload]
    if isinstance(payload, dict):
        return {key: normalize_text_payload(value) for key, value in payload.items()}
    return payload
