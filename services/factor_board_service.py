"""因子看板分析服务。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from types import SimpleNamespace
from typing import Callable, Optional
from uuid import uuid4

import pandas as pd

from config import CACHE_DIR, DB_PATH
from data.data_cache import DataCache, make_mysql_conn
from data.tushare_client import TushareClient
from database.db_config import load_db_config, save_db_config
from database.db_manager import DatabaseManager
from factor.factor_board_analyzer import FactorBoardAnalyzer
from logging_config import get_logger

logger = get_logger("qtsys.factor_board")
SETTINGS_DB = DB_PATH
DEFAULT_INDEX = "000905.SH"
SYSTEM_UNIVERSES = [
    {"code": "000905.SH", "name": "中证500", "benchmark_code": "000905.SH"},
    {"code": "000300.SH", "name": "沪深300", "benchmark_code": "000300.SH"},
    {"code": "000016.SH", "name": "上证50", "benchmark_code": "000016.SH"},
    {"code": "000852.SH", "name": "中证1000", "benchmark_code": "000852.SH"},
]
SYSTEM_UNIVERSE_MAP = {item["code"]: item for item in SYSTEM_UNIVERSES}


class AnalysisCancelledError(Exception):
    pass


def load_settings_snapshot() -> dict:
    if not os.path.exists(SETTINGS_DB):
        return {}
    conn = sqlite3.connect(SETTINGS_DB)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT tushare_token, mysql_host, mysql_port, mysql_user, mysql_password, mysql_database, use_mysql
            FROM settings WHERE id = 1
            """
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def get_factor_board_db_config() -> dict:
    snapshot = load_settings_snapshot()
    if snapshot.get("mysql_host") and snapshot.get("mysql_user"):
        return {
            "host": snapshot.get("mysql_host") or "localhost",
            "port": snapshot.get("mysql_port") or 3306,
            "user": snapshot.get("mysql_user") or "root",
            "password": snapshot.get("mysql_password") or "",
            "database": snapshot.get("mysql_database") or "qtsys",
        }
    return load_db_config()


def get_system_universes() -> list[dict]:
    return [dict(item) for item in SYSTEM_UNIVERSES]


def build_membership_by_date(index_weight: pd.DataFrame, trade_dates: list[str]) -> dict[str, set[str]]:
    if index_weight is None or index_weight.empty:
        return {}
    data = index_weight.copy()
    data["trade_date"] = data["trade_date"].astype(str).str.replace("-", "", regex=False).str[:8]
    data["con_code"] = data["con_code"].astype(str)

    snapshots = {
        trade_date: set(group["con_code"].tolist())
        for trade_date, group in data.groupby("trade_date")
    }
    snapshot_dates = sorted(snapshots)
    if not snapshot_dates:
        return {}

    result = {}
    current_members = snapshots[snapshot_dates[0]]
    pointer = 0
    for trade_date in sorted(trade_dates):
        while pointer + 1 < len(snapshot_dates) and snapshot_dates[pointer + 1] <= trade_date:
            pointer += 1
            current_members = snapshots[snapshot_dates[pointer]]
        result[trade_date] = set(current_members)
    return result


def _normalize_daily_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    result = df.copy()
    if "vol" in result.columns and "volume" not in result.columns:
        result = result.rename(columns={"vol": "volume"})
    if "amount" not in result.columns:
        result["amount"] = result.get("close", 0) * result.get("volume", 0)
    result["trade_date"] = pd.to_datetime(result["trade_date"])
    cols = ["ts_code", "trade_date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]
    return result[[col for col in cols if col in result.columns]]


def _merge_market_frames(*frames: Optional[pd.DataFrame]) -> pd.DataFrame:
    valid = []
    for frame in frames:
        normalized = _normalize_daily_frame(frame)
        if not normalized.empty:
            valid.append(normalized)
    if not valid:
        return pd.DataFrame()
    merged = pd.concat(valid, ignore_index=True)
    merged = merged.drop_duplicates(subset=["ts_code", "trade_date"]).sort_values(["ts_code", "trade_date"])
    return merged.reset_index(drop=True)


def _build_market_bundle_key(analysis_meta: dict, payload: dict) -> str:
    if analysis_meta.get("universe_type") == "system":
        raw = f"system:{analysis_meta.get('universe_code') or DEFAULT_INDEX}"
    else:
        custom_pool_id = payload.get("custom_pool_id") or analysis_meta.get("universe_code") or analysis_meta.get("universe_name")
        raw = f"custom:{custom_pool_id}"
    return raw


def _market_bundle_path(bundle_key: str) -> str:
    digest = hashlib.md5(bundle_key.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, f"factor_board_market_{digest}.pkl")


def _market_bundle_meta_path(bundle_key: str) -> str:
    digest = hashlib.md5(bundle_key.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, f"factor_board_market_{digest}.meta.json")


def _load_market_bundle(bundle_key: Optional[str]) -> pd.DataFrame:
    if not bundle_key:
        return pd.DataFrame()
    path = _market_bundle_path(bundle_key)
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        return _normalize_daily_frame(pd.read_pickle(path))
    except Exception:
        logger.warning("股票池聚合缓存已损坏，忽略: %s", path)
        return pd.DataFrame()


def _load_market_bundle_meta(bundle_key: Optional[str]) -> dict:
    if not bundle_key:
        return {}
    path = _market_bundle_meta_path(bundle_key)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        logger.warning("股票池缓存元数据已损坏，忽略: %s", path)
        return {}


def _build_market_bundle_meta(data: pd.DataFrame) -> dict:
    normalized = _normalize_daily_frame(data)
    if normalized.empty:
        return {"coverage": {}, "updated_at": pd.Timestamp.now().isoformat(timespec="seconds")}
    coverage = normalized.groupby("ts_code")["trade_date"].agg(["min", "max"]).reset_index()
    result = {}
    for row in coverage.itertuples(index=False):
        result[str(row.ts_code)] = {
            "min": pd.to_datetime(row.min).strftime("%Y%m%d"),
            "max": pd.to_datetime(row.max).strftime("%Y%m%d"),
        }
    return {"coverage": result, "updated_at": pd.Timestamp.now().isoformat(timespec="seconds")}


def _save_market_bundle(bundle_key: Optional[str], data: pd.DataFrame) -> None:
    normalized = _normalize_daily_frame(data)
    if not bundle_key or normalized.empty:
        return
    path = _market_bundle_path(bundle_key)
    normalized.to_pickle(path)
    meta_path = _market_bundle_meta_path(bundle_key)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(_build_market_bundle_meta(normalized), f, ensure_ascii=False)


def _slice_market_bundle(bundle_df: pd.DataFrame, codes: list[str], start_date: str, end_date: str) -> pd.DataFrame:
    normalized = _normalize_daily_frame(bundle_df)
    if normalized.empty or not codes:
        return pd.DataFrame()
    start_ts = pd.to_datetime(start_date)
    end_ts = pd.to_datetime(end_date)
    result = normalized[
        normalized["ts_code"].isin(codes)
        & (normalized["trade_date"] >= start_ts)
        & (normalized["trade_date"] <= end_ts)
    ].copy()
    return result.reset_index(drop=True)


def _split_cached_and_pending_codes(bundle_df: pd.DataFrame, bundle_meta: dict, codes: list[str], start_date: str, end_date: str) -> tuple[list[str], list[str]]:
    start_ts = pd.to_datetime(start_date)
    end_ts = pd.to_datetime(end_date)
    coverage = {}
    meta_coverage = bundle_meta.get("coverage") if isinstance(bundle_meta, dict) else None
    if isinstance(meta_coverage, dict) and meta_coverage:
        for code, row in meta_coverage.items():
            try:
                coverage[str(code)] = (
                    pd.to_datetime(str(row.get("min") or "")),
                    pd.to_datetime(str(row.get("max") or "")),
                )
            except Exception:
                continue
    else:
        normalized = _normalize_daily_frame(bundle_df)
        if normalized.empty:
            return [], list(codes)
        subset = normalized[normalized["ts_code"].isin(codes)].copy()
        if subset.empty:
            return [], list(codes)
        agg = subset.groupby("ts_code")["trade_date"].agg(["min", "max"])
        coverage = {str(code): (row["min"], row["max"]) for code, row in agg.iterrows()}
    cached_codes = []
    pending_codes = []
    for code in codes:
        if code not in coverage:
            pending_codes.append(code)
            continue
        min_date, max_date = coverage[code]
        if min_date <= start_ts and max_date >= end_ts:
            cached_codes.append(code)
        else:
            pending_codes.append(code)
    return cached_codes, pending_codes


def fetch_market_data(
    snapshot: dict,
    codes: list[str],
    start_date: str,
    end_date: str,
    bundle_key: Optional[str] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> tuple[pd.DataFrame, dict]:
    if not snapshot.get("tushare_token"):
        raise ValueError("请先在系统设置中配置 Tushare Token")

    cache_settings = SimpleNamespace(
        use_mysql=snapshot.get("use_mysql", 0),
        mysql_host=snapshot.get("mysql_host", ""),
        mysql_port=snapshot.get("mysql_port", 3306),
        mysql_user=snapshot.get("mysql_user", ""),
        mysql_password=snapshot.get("mysql_password", ""),
        mysql_database=snapshot.get("mysql_database", "qtsys"),
    )
    total = len(codes)
    bundle_df = _load_market_bundle(bundle_key)
    bundle_meta = _load_market_bundle_meta(bundle_key)
    cached_codes, pending_codes = _split_cached_and_pending_codes(bundle_df, bundle_meta, codes, start_date, end_date)
    cached_frame = _slice_market_bundle(bundle_df, cached_codes, start_date, end_date)

    if progress_callback:
        if not pending_codes:
            progress_callback({
                "phase": "market",
                "phase_label": "行情准备",
                "market_completed_codes": total,
                "message": f"股票池聚合缓存已命中 {total}/{total} 只股票，直接进入因子分析",
            })
        elif cached_codes:
            progress_callback({
                "phase": "market",
                "phase_label": "行情准备",
                "market_completed_codes": len(cached_codes),
                "message": (
                    f"股票池聚合缓存已命中 {len(cached_codes)}/{total} 只股票，"
                    f"开始增量补齐剩余 {len(pending_codes)} 只..."
                ),
            })
        else:
            progress_callback({
                "phase": "market",
                "phase_label": "行情准备",
                "market_completed_codes": 0,
                "message": f"开始准备 {total} 只股票的行情数据...",
            })

    frames = []
    if not cached_frame.empty:
        frames.append(cached_frame)

    if pending_codes:
        completed = 0
        fetched_count = 0
        failed_codes = []
        max_workers = min(max((os.cpu_count() or 4) // 2, 4), 8)

        def _load_one(code: str):
            worker_client = TushareClient(snapshot["tushare_token"])
            worker_cache = DataCache(worker_client, mysql_conn=make_mysql_conn(cache_settings))
            try:
                return code, _normalize_daily_frame(worker_cache.get_daily(code, start_date, end_date, adj="qfq"))
            finally:
                if worker_cache.mysql:
                    try:
                        worker_cache.mysql.close()
                    except Exception:
                        pass

        executor = ThreadPoolExecutor(max_workers=max_workers)
        futures = {}
        pending_iter = iter(pending_codes)
        try:
            for _ in range(min(max_workers, len(pending_codes))):
                code = next(pending_iter, None)
                if code is None:
                    break
                futures[executor.submit(_load_one, code)] = code

            while futures:
                if cancel_event and cancel_event.is_set():
                    for future in list(futures):
                        future.cancel()
                    raise AnalysisCancelledError("用户已停止当前分析任务")
                try:
                    for future in as_completed(list(futures), timeout=0.5):
                        code = futures.pop(future)
                        completed += 1
                        try:
                            _, df = future.result()
                            if not df.empty:
                                frames.append(df)
                                fetched_count += 1
                            if progress_callback:
                                progress_callback({
                                    "phase": "market",
                                    "phase_label": "行情准备",
                                    "market_completed_codes": len(cached_codes) + completed,
                                    "message": f"增量检查 {len(cached_codes) + completed}/{total}: {code}",
                                })
                        except Exception as exc:
                            failed_codes.append(code)
                            logger.exception("拉取行情失败: %s", code)
                            if progress_callback:
                                progress_callback({
                                    "phase": "market",
                                    "phase_label": "行情准备",
                                    "market_completed_codes": len(cached_codes) + completed,
                                    "message": f"获取数据失败 {len(cached_codes) + completed}/{total}: {code} ({exc})",
                                })
                        next_code = next(pending_iter, None)
                        if next_code is not None and not (cancel_event and cancel_event.is_set()):
                            futures[executor.submit(_load_one, next_code)] = next_code
                        break
                except FuturesTimeoutError:
                    continue
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
    else:
        fetched_count = 0
        failed_codes = []

    market_data = _merge_market_frames(*frames)
    if market_data.empty:
        return pd.DataFrame(), {
            "total_codes": total,
            "cached_codes": len(cached_codes),
            "updated_codes": fetched_count,
            "failed_codes": len(failed_codes),
            "bundle_hit": not pending_codes,
            "bundle_key": bundle_key or "",
        }

    updated_bundle = _merge_market_frames(bundle_df, market_data)
    _save_market_bundle(bundle_key, updated_bundle)
    return market_data, {
        "total_codes": total,
        "cached_codes": len(cached_codes),
        "updated_codes": fetched_count,
        "failed_codes": len(failed_codes),
        "bundle_hit": not pending_codes,
        "bundle_key": bundle_key or "",
    }


def resolve_analysis_window(payload: dict) -> tuple[str, str, int]:
    end_date = str(payload.get("end_date") or pd.Timestamp.today().strftime("%Y%m%d"))[:8]
    start_date = str(payload.get("start_date") or "")[:8]
    backtest_days = int(payload.get("backtest_days") or 365)
    if start_date and end_date:
        if start_date > end_date:
            raise ValueError("无法确定分析日期区间")
        backtest_days = max((pd.to_datetime(end_date) - pd.to_datetime(start_date)).days, 1)
        return start_date, end_date, backtest_days
    start_date = (pd.to_datetime(end_date) - pd.Timedelta(days=backtest_days + 40)).strftime("%Y%m%d")
    return start_date, end_date, backtest_days


def resolve_universe(
    aux_cache: DataCache,
    payload: dict,
    trade_dates: list[str],
) -> tuple[list[str], dict[str, set[str]], Optional[pd.DataFrame], dict]:
    universe_type = payload.get("universe_type") or "system"
    universe_code = str(payload.get("universe_code") or DEFAULT_INDEX)
    universe_name = str(payload.get("universe_name") or "")
    benchmark_code = payload.get("benchmark_code") or None

    if universe_type == "system":
        universe = SYSTEM_UNIVERSE_MAP.get(universe_code)
        if not universe:
            raise ValueError("股票池类型不正确")
        universe_name = universe["name"]
        benchmark_code = benchmark_code or universe["benchmark_code"]
        index_weight = aux_cache.get_index_weight(universe_code, trade_dates[0], trade_dates[-1])
        if index_weight is None or index_weight.empty:
            raise ValueError(f"股票池 {universe_name} 没有可用成分")
        membership_by_date = build_membership_by_date(index_weight, trade_dates)
        codes = sorted({code for members in membership_by_date.values() for code in members})
        return codes, membership_by_date, index_weight, {
            "universe_type": universe_type,
            "universe_code": universe_code,
            "universe_name": universe_name,
            "benchmark_code": benchmark_code,
        }

    stock_items = payload.get("stock_items") or []
    codes = sorted({str(item.get("ts_code") or item.get("code") or "").strip() for item in stock_items if item})
    codes = [code for code in codes if code]
    if not codes:
        raise ValueError("自定义股票池未包含有效股票")
    universe_name = universe_name or "自定义股票池"
    membership_by_date = {trade_date: set(codes) for trade_date in trade_dates}
    return codes, membership_by_date, None, {
        "universe_type": universe_type,
        "universe_code": universe_code or "custom",
        "universe_name": universe_name,
        "benchmark_code": benchmark_code or "",
    }


def _run_single_factor(
    factor_num: int,
    market_data: pd.DataFrame,
    start_date: str,
    end_date: str,
    backtest_days: int,
    membership_by_date: dict[str, set[str]],
    benchmark_data: Optional[pd.DataFrame],
    db_config: dict,
    analysis_batch: str,
    analysis_meta: dict,
):
    db = DatabaseManager(db_config)
    db.connect()
    try:
        analyzer = FactorBoardAnalyzer(db)
        result = analyzer.analyze_single_factor(
            factor_num,
            market_data,
            start_date,
            end_date,
            backtest_days=backtest_days,
            membership_by_date=membership_by_date,
            benchmark_data=benchmark_data,
        )
        if result:
            result.update(analysis_meta)
            db.save_factor_result(result, analysis_batch)
        return result
    finally:
        db.close()


def _to_compact_date(value) -> str:
    if value is None or value == "":
        return ""
    return pd.to_datetime(value).strftime("%Y%m%d")


def _can_reuse_latest_batch(latest_batch: Optional[dict], analysis_meta: dict, start_date: str, end_date: str, backtest_days: int) -> bool:
    if not latest_batch:
        return False
    if int(latest_batch.get("factor_count") or 0) < 191:
        return False
    latest_start = _to_compact_date(latest_batch.get("start_date"))
    latest_end = _to_compact_date(latest_batch.get("end_date"))
    latest_days = int(latest_batch.get("backtest_days") or 0)
    latest_type = str(latest_batch.get("universe_type") or "")
    latest_code = str(latest_batch.get("universe_code") or "")
    return (
        latest_start == start_date
        and latest_end == end_date
        and latest_days == int(backtest_days)
        and latest_type == str(analysis_meta.get("universe_type") or "")
        and latest_code == str(analysis_meta.get("universe_code") or "")
    )


def run_factor_board_analysis(payload: dict, status_callback: Callable[[dict], None], cancel_event: Optional[threading.Event] = None):
    if cancel_event and cancel_event.is_set():
        raise AnalysisCancelledError("用户已停止当前分析任务")
    snapshot = load_settings_snapshot()
    if not snapshot.get("tushare_token"):
        raise ValueError("请先在系统设置中配置 Tushare Token")

    db_config = get_factor_board_db_config()
    save_db_config(db_config)

    db = DatabaseManager(db_config)
    db.connect()
    db.init_tables()
    db.close()
    reuse_existing = payload.get("reuse_existing", True)

    start_date, end_date, backtest_days = resolve_analysis_window(payload)
    cache_settings = SimpleNamespace(
        use_mysql=snapshot.get("use_mysql", 0),
        mysql_host=snapshot.get("mysql_host", ""),
        mysql_port=snapshot.get("mysql_port", 3306),
        mysql_user=snapshot.get("mysql_user", ""),
        mysql_password=snapshot.get("mysql_password", ""),
        mysql_database=snapshot.get("mysql_database", "qtsys"),
    )
    aux_client = TushareClient(snapshot["tushare_token"])
    aux_cache = DataCache(aux_client, mysql_conn=make_mysql_conn(cache_settings))

    status_callback({
        "message": "正在准备分析数据...",
        "progress": 0,
        "start_date": start_date,
        "end_date": end_date,
        "phase": "prepare",
        "phase_label": "准备参数",
    })
    trade_dates = aux_cache.get_trade_cal(start_date, end_date)
    if not trade_dates:
        raise ValueError("无法获取交易日历，请检查 Tushare Token 是否有效")
    if cancel_event and cancel_event.is_set():
        raise AnalysisCancelledError("用户已停止当前分析任务")

    all_codes, membership_by_date, _, analysis_meta = resolve_universe(aux_cache, payload, trade_dates)
    if not all_codes:
        raise ValueError("未获取到可分析的股票池成分")

    market_bundle_key = _build_market_bundle_key(analysis_meta, payload)
    status_callback({
        "message": f"已加载股票池：{analysis_meta['universe_name']}",
        "phase": "universe",
        "phase_label": "确定股票池",
        "universe_type": analysis_meta["universe_type"],
        "universe_code": analysis_meta["universe_code"],
        "universe_name": analysis_meta["universe_name"],
        "backtest_days": backtest_days,
        "market_total_codes": len(all_codes),
        "market_bundle_key": market_bundle_key,
    })

    if reuse_existing:
        latest_db = DatabaseManager(db_config)
        latest_db.connect()
        try:
            latest_batch = latest_db.get_latest_batch_summary()
        finally:
            latest_db.close()
        if _can_reuse_latest_batch(latest_batch, analysis_meta, start_date, end_date, backtest_days):
            reused_batch = str(latest_batch.get("analysis_batch") or "")
            status_callback({
                "progress": 191,
                "total": 191,
                "current_factor": "",
                "analysis_batch": reused_batch,
                "message": f"\u68c0\u6d4b\u5230\u53ef\u590d\u7528\u6279\u6b21 {reused_batch}\uff0c\u5df2\u76f4\u63a5\u8fd4\u56de\u6700\u65b0\u5206\u6790\u7ed3\u679c",
                "start_date": start_date,
                "end_date": end_date,
                "backtest_days": backtest_days,
                "phase": "completed",
                "phase_label": "\u590d\u7528\u5386\u53f2\u7ed3\u679c",
                "batch_reused": True,
                "result_count": 191,
                "market_cached_codes": len(all_codes),
                "market_updated_codes": 0,
                "market_failed_codes": 0,
                "market_total_codes": len(all_codes),
                "market_bundle_hit": True,
            })
            return {
                "analysis_batch": reused_batch,
                "reused": True,
                "message": f"\u5df2\u590d\u7528\u5386\u53f2\u6279\u6b21 {reused_batch}",
            }

    market_data, market_stats = fetch_market_data(
        snapshot,
        all_codes,
        start_date,
        end_date,
        bundle_key=market_bundle_key,
        progress_callback=lambda patch: status_callback(patch if isinstance(patch, dict) else {"message": patch}),
        cancel_event=cancel_event,
    )
    if market_data.empty:
        raise ValueError("未获取到可分析的行情数据")

    status_callback({
        "message": (
            f"行情准备完成：缓存命中 {market_stats['cached_codes']} 只，"
            f"增量补齐 {market_stats['updated_codes']} 只，"
            f"失败 {market_stats['failed_codes']} 只"
        ),
        "phase": "market",
        "phase_label": "行情准备完成",
        "market_total_codes": market_stats["total_codes"],
        "market_completed_codes": market_stats["total_codes"],
        "market_cached_codes": market_stats["cached_codes"],
        "market_updated_codes": market_stats["updated_codes"],
        "market_failed_codes": market_stats["failed_codes"],
        "market_bundle_hit": market_stats["bundle_hit"],
    })

    benchmark_data = None
    if analysis_meta.get("benchmark_code"):
        status_callback({"message": "正在加载基准指数...", "phase": "benchmark", "phase_label": "加载基准"})
        benchmark_data = aux_cache.get_index_daily(analysis_meta["benchmark_code"], start_date, end_date)
        if benchmark_data is None or benchmark_data.empty:
            logger.warning("未获取到基准指数数据，将回退为等权基准")
            benchmark_data = None

    if aux_cache.mysql:
        try:
            aux_cache.mysql.close()
        except Exception:
            pass

    requested_batch = str(payload.get("analysis_batch") or "").strip()
    analysis_batch = requested_batch or uuid4().hex[:12]
    factor_numbers = [
        int(num)
        for num in (payload.get("factor_numbers") or list(range(1, 192)))
        if str(num).strip().isdigit()
    ]
    if not factor_numbers:
        raise ValueError("未提供可执行的因子列表")
    max_workers = min(max((os.cpu_count() or 4) // 2, 2), 4)
    completed = 0
    saved_results = 0
    failed_factors: list[str] = []
    if requested_batch:
        batch_db = DatabaseManager(db_config)
        batch_db.connect()
        try:
            saved_results = batch_db.get_batch_result_count(analysis_batch)
        finally:
            batch_db.close()

    status_callback({
        "message": f"开始批量分析 {len(factor_numbers)} 个因子...",
        "total": len(factor_numbers),
        "progress": 0,
        "analysis_batch": analysis_batch,
        "backtest_days": backtest_days,
        "phase": "factors",
        "phase_label": "因子回测中",
        "factor_total": len(factor_numbers),
        "result_count": 0,
    })
    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures = {}
    factor_iter = iter(factor_numbers)
    try:
        for _ in range(min(max_workers, len(factor_numbers))):
            factor_num = next(factor_iter, None)
            if factor_num is None:
                break
            futures[executor.submit(
                _run_single_factor,
                factor_num,
                market_data,
                start_date,
                end_date,
                backtest_days,
                membership_by_date,
                benchmark_data,
                db_config,
                analysis_batch,
                analysis_meta,
            )] = factor_num

        while futures:
            if cancel_event and cancel_event.is_set():
                for future in list(futures):
                    future.cancel()
                raise AnalysisCancelledError("用户已停止当前分析任务")
            try:
                for future in as_completed(list(futures), timeout=0.5):
                    factor_num = futures.pop(future)
                    completed += 1
                    exc = future.exception()
                    if exc:
                        failed_factors.append(f"Alpha#{factor_num}")
                        logger.exception("Alpha#%s 分析失败", factor_num)
                        status_callback({
                            "progress": completed,
                            "current_factor": f"Alpha#{factor_num}",
                            "message": f"Alpha#{factor_num} 分析失败: {exc}",
                            "factor_completed": completed,
                            "result_count": saved_results,
                            "failed_factors": failed_factors,
                        })
                    else:
                        result = future.result()
                        if result:
                            saved_results += 1
                        status_callback({
                            "progress": completed,
                            "current_factor": f"Alpha#{factor_num}",
                            "message": f"已完成 Alpha#{factor_num}",
                            "factor_completed": completed,
                            "result_count": saved_results,
                            "failed_factors": failed_factors,
                        })
                    next_factor = next(factor_iter, None)
                    if next_factor is not None and not (cancel_event and cancel_event.is_set()):
                        futures[executor.submit(
                            _run_single_factor,
                            next_factor,
                            market_data,
                            start_date,
                            end_date,
                            backtest_days,
                            membership_by_date,
                            benchmark_data,
                            db_config,
                            analysis_batch,
                            analysis_meta,
                        )] = next_factor
                    break
            except FuturesTimeoutError:
                continue
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    status_callback({
        "progress": len(factor_numbers),
        "current_factor": "",
        "message": "全部因子分析完成",
        "analysis_batch": analysis_batch,
        "start_date": start_date,
        "end_date": end_date,
        "backtest_days": backtest_days,
        "factor_completed": len(factor_numbers),
        "result_count": saved_results,
        "failed_factors": failed_factors,
        "phase": "completed",
        "phase_label": "分析完成",
    })
    return analysis_batch
