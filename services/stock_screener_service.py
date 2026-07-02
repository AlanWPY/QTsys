"""股票选股服务 - 基本面+技术面双维度打分选股（v2 修复版）"""
from __future__ import annotations
import asyncio, math, os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Optional
import numpy as np
import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession
from data.data_cache import DataCache, make_mysql_conn
from data.tushare_client import TushareClient
from factor.factor_engine import FactorEngine
from services.settings_service import get_or_create_settings
from logging_config import get_logger

logger = get_logger("qtsys.screener")

SYSTEM_POOLS = [
    {"code": "000300.SH", "name": "沪深300", "type": "index"},
    {"code": "000905.SH", "name": "中证500",  "type": "index"},
    {"code": "000016.SH", "name": "上证50",   "type": "index"},
    {"code": "000852.SH", "name": "中证1000", "type": "index"},
    {"code": "all_a",     "name": "全体A股",  "type": "all"},
]
INDUSTRY_POOLS = [
    "银行","非银金融","计算机","通信","电子","医药生物","食品饮料","家用电器",
    "汽车","房地产","建筑材料","建筑装饰","机械设备","电力设备","化工",
    "农林牧渔","钢铁","有色金属","煤炭","石油石化","交通运输","商贸零售",
    "社会服务","传媒","国防军工","公用事业","轻工制造","纺织服饰","综合",
]


def _safe(v, default=0.0):
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except Exception:
        return default


def _safen(v):
    """返回 float 或 None（不存在/NaN 时返回 None）"""
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except Exception:
        return None


def get_stock_pool_options():
    return {
        "system_pools": SYSTEM_POOLS,
        "industry_pools": [{"code": f"industry:{s}", "name": s, "type": "industry"} for s in INDUSTRY_POOLS],
    }


def _fetch_pool_codes(client, cache, pool_code, as_of_date):
    if pool_code == "all_a":
        df = client.get_stock_basic_full()
        return df["ts_code"].dropna().unique().tolist() if df is not None and not df.empty else []
    if pool_code.startswith("industry:"):
        industry = pool_code[9:]
        df = client.get_stock_basic_full()
        if df is None or df.empty:
            return []
        return df[df["industry"] == industry]["ts_code"].dropna().unique().tolist()
    try:
        end   = as_of_date
        start = (pd.to_datetime(end) - timedelta(days=45)).strftime("%Y%m%d")
        df = cache.get_index_weight(pool_code, start, end)
        if df is not None and not df.empty and "con_code" in df.columns:
            codes = df["con_code"].dropna().unique().tolist()
            if codes:
                return codes
        df2 = client.get_index_weight(pool_code, end, end)
        if df2 is not None and not df2.empty and "con_code" in df2.columns:
            codes = df2["con_code"].dropna().unique().tolist()
            if codes:
                return codes
        df3 = client.get_index_weight(pool_code, start, end)
        if df3 is not None and not df3.empty and "con_code" in df3.columns:
            return df3["con_code"].dropna().unique().tolist()
    except Exception as e:
        logger.warning(f"获取指数成分失败 {pool_code}: {e}")
    return []


def _piotroski_score(ts_code, token):
    """
    改进版 Piotroski F-Score，9 个维度，每项通过得 1 分，映射到 0-100。
    每线程独立创建 TushareClient，避免多线程 token 竞争。
    字段名修正：assets_turn（非 asset_turnover），ocf_to_or 用于质量检验。
    """
    score = 0
    details = {}
    try:
        client = TushareClient(token)
        fina = client.get_fina_indicator(ts_code)
        if fina is None or fina.empty:
            return {"score": 0, "details": {}, "error": "无财务数据"}
        fina = fina.sort_values("end_date").dropna(subset=["end_date"]).reset_index(drop=True)
        if len(fina) < 2:
            return {"score": 0, "details": {}, "error": "财务数据不足"}

        latest = fina.iloc[-1]
        prev   = fina.iloc[-2]

        def gv(row, field):
            return _safen(row.get(field))

        # F1 盈利能力
        roa_now  = gv(latest, "roa")
        roa_prev = gv(prev,   "roa")
        ocf_or   = gv(latest, "ocf_to_or")       # 经营现金流/营业收入(%)
        npm      = gv(latest, "netprofit_margin") # 净利润率(%)

        p1 = int(roa_now is not None and roa_now > 0)
        p2 = int(ocf_or  is not None and ocf_or  > 0)
        p3 = int(roa_now is not None and roa_prev is not None and roa_now > roa_prev)
        p4 = int(ocf_or  is not None and npm      is not None and ocf_or  > npm)
        details.update({"ROA正值": p1, "经营现金流正值": p2, "ROA同比提升": p3, "现金流高于净利率": p4})
        score += p1 + p2 + p3 + p4

        # F2 杠杆/流动性
        debt_now  = gv(latest, "debt_to_assets")
        debt_prev = gv(prev,   "debt_to_assets")
        curr_now  = gv(latest, "current_ratio")
        curr_prev = gv(prev,   "current_ratio")

        p5 = int(debt_now is not None and debt_prev is not None and debt_now < debt_prev)
        p6 = int(curr_now is not None and curr_prev is not None and curr_now > curr_prev)
        details.update({"资产负债率下降": p5, "流动比率提升": p6})
        score += p5 + p6

        # F3 运营效率
        gm_now  = gv(latest, "grossprofit_margin")
        gm_prev = gv(prev,   "grossprofit_margin")
        at_now  = gv(latest, "assets_turn")   # 正确字段名
        at_prev = gv(prev,   "assets_turn")
        roe_now = gv(latest, "roe")

        p7 = int(gm_now is not None and gm_prev is not None and gm_now > gm_prev)
        p8 = int(at_now is not None and at_prev is not None and at_now > at_prev)
        p9 = int(roe_now is not None and roe_now > 8)
        details.update({"毛利率提升": p7, "资产周转率提升": p8, "ROE>8%": p9})
        score += p7 + p8 + p9

        return {
            "score":            round(score / 9 * 100, 1),
            "raw_score":        score,
            "details":          details,
            "roe":              round(roe_now,   2) if roe_now  is not None else None,
            "roa":              round(roa_now,   2) if roa_now  is not None else None,
            "debt_to_assets":   round(debt_now,  2) if debt_now is not None else None,
            "grossprofit_margin": round(gm_now,  2) if gm_now  is not None else None,
            "current_ratio":    round(curr_now,  2) if curr_now is not None else None,
        }
    except Exception as e:
        logger.warning(f"基本面评分失败 {ts_code}: {e}")
        return {"score": 0, "details": {}, "error": str(e)}


def _compute_factor_value(ts_code, factor, cache, start_date, end_date):
    try:
        engine = FactorEngine(cache)
        expression = factor.get("expression", "")
        if not expression:
            return None
        series = engine.compute_factor_values(expression, ts_code, start_date, end_date)
        if series is None or series.empty:
            return None
        last = series.dropna().iloc[-1] if not series.dropna().empty else None
        return float(last) if last is not None and math.isfinite(float(last)) else None
    except Exception:
        return None


def _rank_normalize(values):
    if not values:
        return {}
    codes = list(values.keys())
    vals  = np.array([values[c] for c in codes], dtype=float)
    ranks = pd.Series(vals).rank(pct=True).values
    return {codes[i]: round(float(ranks[i]) * 100, 2) for i in range(len(codes))}


async def run_stock_screener(payload, db):
    settings = await get_or_create_settings(db)
    token = (settings.tushare_token or "").strip()
    if not token:
        return {"error": "请先在设置中配置 Tushare Token"}

    client = TushareClient(token)
    from types import SimpleNamespace
    cs = SimpleNamespace(
        use_mysql=getattr(settings, "use_mysql", 0),
        mysql_host=getattr(settings, "mysql_host", ""),
        mysql_port=getattr(settings, "mysql_port", 3306),
        mysql_user=getattr(settings, "mysql_user", ""),
        mysql_password=getattr(settings, "mysql_password", ""),
        mysql_database=getattr(settings, "mysql_database", "qtsys"),
    )
    cache = DataCache(client, mysql_conn=make_mysql_conn(cs))

    pool_code   = str(payload.get("pool_code") or "000300.SH")
    fund_weight = max(0.0, min(float(payload.get("fund_weight", 0.5)), 1.0))
    tech_weight = max(0.0, min(float(payload.get("tech_weight", 0.5)), 1.0))
    total_w = fund_weight + tech_weight
    if total_w > 0:
        fund_weight /= total_w
        tech_weight /= total_w
    else:
        fund_weight = tech_weight = 0.5

    tech_factors = payload.get("tech_factors") or []
    top_n    = max(5, min(int(payload.get("top_n", 50)), 200))
    end_date = str(payload.get("end_date") or datetime.now().strftime("%Y%m%d"))[:8]
    start_date = (pd.to_datetime(end_date) - timedelta(days=365)).strftime("%Y%m%d")

    # 1. 股票池
    codes = await asyncio.to_thread(_fetch_pool_codes, client, cache, pool_code, end_date)
    if not codes:
        return {"error": f"无法获取股票池 {pool_code} 的成分股"}
    codes = list(dict.fromkeys(codes))[:500]

    # 2. 股票基本信息
    stock_info = {}
    try:
        basic = client.get_stock_basic_full()
        if basic is not None and not basic.empty:
            for _, row in basic[basic["ts_code"].isin(codes)].iterrows():
                stock_info[row["ts_code"]] = {
                    "name":     str(row.get("name") or ""),
                    "industry": str(row.get("industry") or ""),
                    "area":     str(row.get("area") or ""),
                }
    except Exception as e:
        logger.warning(f"获取基本信息失败: {e}")

    # 3. 批量市场估值（PE/PB/市值），自动回退到最近有效交易日
    market_data = {}
    try:
        basic_df = await asyncio.to_thread(
            client.get_daily_basic_batch, end_date,
            "ts_code,pe,pb,total_mv,turnover_rate"
        )
        if basic_df is not None and not basic_df.empty:
            code_set = set(codes)
            for _, row in basic_df[basic_df["ts_code"].isin(code_set)].iterrows():
                market_data[row["ts_code"]] = {
                    "pe":           _safen(row.get("pe")),
                    "pb":           _safen(row.get("pb")),
                    "total_mv":     _safen(row.get("total_mv")),
                    "turnover_rate":_safen(row.get("turnover_rate")),
                }
            logger.info(f"市场数据获取成功: {len(market_data)} 条")
        else:
            logger.warning("批量市场数据返回空")
    except Exception as e:
        logger.warning(f"批量市场数据失败: {e}")

    # 4. 基本面评分（并行，每线程独立客户端）
    fund_scores = {}
    if fund_weight > 0:
        def _score_one(code):
            return code, _piotroski_score(code, token)

        with ThreadPoolExecutor(max_workers=min(max(os.cpu_count() or 4, 6), 12)) as ex:
            futs = {ex.submit(_score_one, c): c for c in codes}
            for fut in as_completed(futs):
                code, result = fut.result()
                fund_scores[code] = result

    # 5. 技术面评分（并行）
    tech_scores = {}
    tech_factor_values = {c: {} for c in codes}

    if tech_weight > 0 and tech_factors:
        for factor in tech_factors:
            fname     = factor.get("name") or factor.get("expression", "?")
            direction = factor.get("direction", "high")
            raw_values = {}

            def _factor_one(code, _f=factor):
                return code, _compute_factor_value(code, _f, cache, start_date, end_date)

            with ThreadPoolExecutor(max_workers=min(max(os.cpu_count() or 4, 6), 12)) as ex:
                futs = {ex.submit(_factor_one, c): c for c in codes}
                for fut in as_completed(futs):
                    code, v = fut.result()
                    if v is not None:
                        raw_values[code] = v

            ranked = _rank_normalize(raw_values)
            if direction == "low":
                ranked = {k: 100 - v for k, v in ranked.items()}
            for code, v in ranked.items():
                tech_factor_values[code][fname] = round(v, 1)

        for code in codes:
            fv = tech_factor_values.get(code, {})
            tech_scores[code] = round(float(np.mean(list(fv.values()))), 2) if fv else 0.0

    # 6. 综合打分
    results = []
    for code in codes:
        fs     = fund_scores.get(code, {})
        fund_s = _safe(fs.get("score"), 0.0) if fund_weight > 0 else None
        tech_s = tech_scores.get(code, 0.0) if tech_weight > 0 and tech_factors else None

        if fund_weight > 0 and tech_weight > 0 and tech_factors:
            total = round(_safe(fund_s) * fund_weight + _safe(tech_s) * tech_weight, 2)
        elif fund_weight > 0:
            total = _safe(fund_s)
        elif tech_weight > 0 and tech_factors:
            total = _safe(tech_s)
        else:
            total = 0.0

        info = stock_info.get(code, )
        mkt  = market_data.get(code, {})

        results.append({
            "ts_code":     code,
            "name":        info.get("name", ""),
            "industry":    info.get("industry", ""),
            "area":        info.get("area", ""),
            "total_score": round(float(total), 2),
            "fund_score":  round(float(fund_s), 2) if fund_s is not None else None,
            "tech_score":  round(float(tech_s), 2) if tech_s is not None else None,
            "pe":          (round(mkt["pe"],   2) if mkt.get("pe")       is not None else None),
            "pb":          (round(mkt["pb"],   2) if mkt.get("pb")       is not None else None),
            "total_mv":    (round(mkt["total_mv"] / 10000, 2) if mkt.get("total_mv") is not None else None),
            "roe":              fs.get("roe"),
            "roa":              fs.get("roa"),
            "debt_to_assets":   fs.get("debt_to_assets"),
            "piotroski_details": fs.get("details", {}),
            "tech_factor_values": tech_factor_values.get(code, {}),
        })

    results.sort(key=lambda x: x["total_score"], reverse=True)
    results = results[:top_n]
    for i, r in enumerate(results):
        r["rank"] = i + 1

    if cache.mysql:
        try: cache.mysql.close()
        except Exception: pass

    return {
        "results":     results,
        "total_codes": len(codes),
        "pool_code":   pool_code,
        "fund_weight": round(fund_weight * 100, 1),
        "tech_weight": round(tech_weight * 100, 1),
        "end_date":    end_date,
        "top_n":       top_n,
    }
